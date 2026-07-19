"""Remote streamable-HTTP MCP client -- the consumer side of the opt-in remote
tier (`deploy --remote-mcp`).

Where mcp_stdio.ServerPool spawns each @chkp server as a local stdio child,
this connects to the per-server Azure Container Apps that `deploy --remote-mcp`
stood up, over their streamable-HTTP MCP endpoints (`https://<app>/mcp`), with
an Entra bearer token for the gateway app-registration audience (Easy Auth
requires it). It exposes the IDENTICAL interface -- `list_tools()` / `call()`
with the same `<target>___<tool>` namespacing and the same one-server-failure-
never-kills-the-pool degradation -- so agent.py drives either transport
interchangeably (CHKP_MCP_TRANSPORT selects).

This is what makes the remote tier a real SECOND consumer path: the same tools
are now reachable by ANY MCP client that can present an Entra token for the
audience (Foundry portal agents, Copilot Studio, Claude Desktop), not only this
agent's in-process stdio children.

Design decisions:
  - The `mcp` package is imported lazily (optional extra), same as mcp_stdio.
  - Tool listing/calling/namespacing helpers are shared with mcp_stdio so the
    two transports can never drift in how they present tools to the model.
  - A single bearer is acquired for the shared audience (all endpoints belong to
    one app registration); token acquisition is injectable for tests.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import AsyncExitStack
from typing import Callable

from .config import remote_scope, target_name
from .mcp_stdio import (
    NamespacedTool,
    ToolCallResult,
    _object_schema,
    _result_text,
    split_namespaced,
)
from .config import TOOL_DESCRIPTION_MAX_CHARS, tool_namespace

START_TIMEOUT_S = 120.0


def parse_endpoints(value) -> list[dict]:
    """Parse the CHKP_REMOTE_MCP catalog (JSON list of {"server","url"}) into a
    validated list. Tolerant: None/''/garbage -> []; entries missing server or
    url are dropped. Order preserved."""
    if not value:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        if isinstance(item, dict) and item.get("server") and item.get("url"):
            out.append({"server": str(item["server"]), "url": str(item["url"])})
    return out


def scope_for(audience: str | None) -> str | None:
    """The Entra scope requested for the bearer, from the persisted audience
    (`api://<client-id>` -> `api://<client-id>/.default`). Accepts an audience
    that already carries the /.default suffix. None/'' -> None."""
    aud = (audience or "").strip()
    if not aud:
        return None
    if aud.endswith("/.default"):
        return aud
    # remote_scope expects the bare client id or api://<id>; normalize both.
    if aud.startswith("api://"):
        return f"{aud.rstrip('/')}/.default"
    return remote_scope(aud)


def auth_headers(token: str | None) -> dict[str, str]:
    """The request headers for a bearer token (empty when no token -- the pool
    still tries, and a 401 degrades that one endpoint, not the whole pool)."""
    return {"Authorization": f"Bearer {token}"} if token else {}


def _default_token_provider(scope: str) -> str | None:
    """Acquire an Entra token for `scope` via DefaultAzureCredential (az login
    locally; the per-agent identity inside the hosted container). Lazy import so
    importing this module never requires azure-identity."""
    from azure.identity import DefaultAzureCredential

    return DefaultAzureCredential().get_token(scope).token


def _mcp_imports():
    """Lazy import of the streamable-HTTP client (optional 'mcp' extra)."""
    try:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client
    except ImportError as exc:
        raise RuntimeError(
            "the remote MCP client needs the optional 'mcp' package -- install it:\n"
            '  pip install "chkpmcpaz[mcp]"'
        ) from exc
    return ClientSession, streamablehttp_client


def _log(line: str) -> None:
    print(line, flush=True)


class RemoteServerPool:
    """Async context manager holding one streamable-HTTP MCP session per remote
    @chkp Container App. Same surface as mcp_stdio.ServerPool: enter, then
    `list_tools()` / `call()`; endpoints that fail to connect are logged and
    skipped so a single down app never breaks the rest."""

    def __init__(self, endpoints: list[dict], *, audience: str | None = None,
                 token: str | None = None,
                 token_provider: Callable[[str], str | None] | None = None):
        self.endpoints = list(endpoints or [])
        # Mirror ServerPool.servers so the agent loop's "N @chkp servers" line
        # works against either transport.
        self.servers = [ep["server"] for ep in self.endpoints]
        self.audience = audience
        self._token = token
        self._token_provider = token_provider or _default_token_provider
        self._stack: AsyncExitStack | None = None
        self._sessions: dict[str, tuple[str, object]] = {}  # target -> (server, session)

    def _bearer(self) -> str | None:
        """The bearer to present: an explicit token wins; else acquire one for
        the audience's scope. A token-acquisition failure is non-fatal (the pool
        proceeds token-less and each endpoint's 401 degrades just that one)."""
        if self._token:
            return self._token
        scope = scope_for(self.audience)
        if not scope:
            return None
        try:
            return self._token_provider(scope)
        except Exception as exc:  # noqa: BLE001 -- token miss must not kill the pool
            _log(f"  could not acquire an Entra token for the remote MCP audience "
                 f"({type(exc).__name__}) -- endpoints requiring auth will 401.")
            return None

    async def __aenter__(self) -> "RemoteServerPool":
        ClientSession, streamablehttp_client = _mcp_imports()
        headers = auth_headers(self._bearer())
        self._stack = AsyncExitStack()
        await self._stack.__aenter__()
        try:
            await self._connect(ClientSession, streamablehttp_client, headers)
        except BaseException:
            await self.__aexit__(None, None, None)
            raise
        return self

    async def _connect(self, ClientSession, streamablehttp_client, headers):
        for ep in self.endpoints:
            server, url = ep["server"], ep["url"]
            child = AsyncExitStack()
            try:
                async with asyncio.timeout(START_TIMEOUT_S):
                    # streamablehttp_client yields (read, write, get_session_id).
                    transport = await child.enter_async_context(
                        streamablehttp_client(url, headers=headers))
                    read, write = transport[0], transport[1]
                    session = await child.enter_async_context(
                        ClientSession(read, write))
                    await session.initialize()
            except BaseException as exc:  # noqa: BLE001 -- one endpoint never kills the pool
                reason = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
                if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
                    reason = f"connect/initialize timed out after {START_TIMEOUT_S:.0f}s"
                _log(f"✗ {server} unreachable at {url}: {reason[:140]}")
                try:
                    await child.aclose()
                except Exception:
                    pass
                if isinstance(exc, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)):
                    raise
                continue
            self._sessions[target_name(server)] = (server, session)
            self._stack.push_async_exit(child.pop_all())

    async def __aexit__(self, exc_type, exc, tb):
        if self._stack is None:
            return False
        try:
            await self._stack.__aexit__(exc_type, exc, tb)
        except Exception as teardown:  # session close races are non-actionable
            _log(f"(remote pool teardown: {type(teardown).__name__})")
        finally:
            self._stack = None
            self._sessions.clear()
        return False

    async def list_tools(self) -> list[NamespacedTool]:
        """Paginated tools/list per remote session, merged and namespaced --
        identical presentation to the stdio pool."""
        merged: list[NamespacedTool] = []
        for target, (server, session) in self._sessions.items():
            try:
                cursor = None
                while True:
                    res = await session.list_tools(cursor=cursor)
                    for tool in res.tools:
                        desc = (tool.description or tool.name).strip() or tool.name
                        merged.append(NamespacedTool(
                            namespaced=tool_namespace(server, tool.name),
                            server=server,
                            description=desc[:TOOL_DESCRIPTION_MAX_CHARS],
                            input_schema=_object_schema(
                                getattr(tool, "inputSchema", None)),
                        ))
                    cursor = res.nextCursor
                    if not cursor:
                        break
            except Exception as exc:  # noqa: BLE001
                _log(f"✗ {server} tools/list failed: {str(exc)[:140]}")
        return merged

    async def call(self, namespaced: str, args: dict) -> ToolCallResult:
        """tools/call by namespaced name -- catches every exception and reports
        it back to the model instead of crashing the loop (mirror of stdio)."""
        try:
            target, tool = split_namespaced(namespaced)
            entry = self._sessions.get(target)
            if entry is None:
                raise RuntimeError(f"no remote server for target {target!r}")
            _, session = entry
            res = await session.call_tool(tool, args or {})
            return {"text": _result_text(res),
                    "error": bool(getattr(res, "isError", False))}
        except Exception as exc:  # noqa: BLE001
            return {"text": f"tool call failed: {exc}", "error": True}
