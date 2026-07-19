"""Stdio child-process MCP client -- the Azure replacement for the AWS
AgentCore Gateway tier.

On AWS every @chkp server is its own hosted container aggregated behind one
gateway; here the agent process itself spawns each server as a stdio child
(`npx -y @chkp/<server>-mcp@<pin>`) -- locally for `chat --runtime local`,
inside the Foundry Hosted Agent container for the hosted runtime. The same
pinned packages, the same per-server credential env vars (from Key Vault
instead of Secrets Manager), the same `TELEMETRY_DISABLED=true`, and the same
tool namespacing `<server-no-hyphens>___<tool>` -- only the transport differs.

Design decisions:
  - One child failing to start must NOT kill the pool: log a one-line
    `✗ <server> failed to start: <reason>` and continue with the rest --
    exactly how a FAILED gateway target degraded on AWS.
  - `call()` catches EVERY exception and returns an error ToolCallResult so a
    tool/transport failure is fed back to the model instead of crashing the
    agentic loop (mirror of the AWS `_call_one`).
  - The `mcp` package is imported lazily so this module (and everything that
    imports it) works without the optional extra installed; the error message
    says exactly what to install.
  - Unlike the AWS gateway, stdio DOES relay `elicitation/create`, so the
    quantum-gaia agent-side answerer (chkpmcpaz.gaia) actually fires here.
  - Credential VALUES pass only through the child's process environment;
    nothing here logs or prints them.
"""

from __future__ import annotations

import asyncio
import os
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Mapping, TypedDict

from .config import (
    SERVERS,
    TOOL_DESCRIPTION_MAX_CHARS,
    ServerSpec,
    split_namespaced,
    target_name,
    tool_namespace,
)

# A cold `npx -y` download of a pinned package can take a while on first run
# (the image does not pre-bake the 15 packages, matching the AWS entrypoint);
# a child that has not finished `initialize` by then is declared failed.
START_TIMEOUT_S = 120.0


@dataclass
class NamespacedTool:
    """One tool, merged into the flat namespaced catalog the agent sees."""
    namespaced: str      # 'quantummanagement___show_hosts'
    server: str          # 'quantum-management'
    description: str     # truncated to TOOL_DESCRIPTION_MAX_CHARS
    input_schema: dict   # normalized to an object schema


class ToolCallResult(TypedDict):
    text: str            # full text; agent.py truncates before feeding back
    error: bool


def npx_command(spec: ServerSpec) -> list[str]:
    """The exact child-process command line: version-pinned package plus the
    server's extra args (e.g. documentation's `--region US`). Stdio transport
    is the @chkp default, so no transport flags are needed."""
    return ["npx", "-y", spec.pinned, *spec.args]


def build_child_env(base: Mapping[str, str], creds: dict[str, str] | None) -> dict[str, str]:
    """Child env = parent env + the server's Key Vault secret body (if any) +
    TELEMETRY_DISABLED=true (same knob the AWS server image baked in)."""
    env = {str(k): str(v) for k, v in dict(base).items()}
    for k, v in (creds or {}).items():
        env[str(k)] = str(v)
    env["TELEMETRY_DISABLED"] = "true"
    return env


def _mcp_imports():
    """Lazy import so `mcp` stays an optional dependency of the package."""
    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
    except ImportError as exc:
        raise RuntimeError(
            "the stdio MCP client needs the optional 'mcp' package -- install it:\n"
            '  pip install "chkpmcpaz[mcp]"'
        ) from exc
    return ClientSession, StdioServerParameters, stdio_client


def _object_schema(schema) -> dict:
    """The Anthropic API wants input_schema to be an object schema. MCP tool
    schemas already are; normalize the minimum so a sloppy one never 400s."""
    if not isinstance(schema, dict) or schema.get("type") != "object":
        return {"type": "object", "properties": {}}
    schema.setdefault("properties", {})
    return schema


def _result_text(result) -> str:
    """Flatten an mcp CallToolResult into text for a tool_result block."""
    parts = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        parts.append(text if text is not None else str(block))
    return "\n".join(parts) if parts else "(no content)"


def _log(line: str) -> None:
    print(line, flush=True)


class ServerPool:
    """Async context manager owning one stdio child per requested server.

    Enter the pool, `list_tools()` for the merged namespaced catalog, `call()`
    tools by namespaced name, exit to terminate every child. Children that
    fail to start are logged and skipped; their tools simply don't appear."""

    def __init__(self, servers: list[str], creds_by_server: dict[str, dict[str, str]],
                 *, elicitation_callback=None, env: Mapping[str, str] | None = None):
        for name in servers:
            if name not in SERVERS:
                raise ValueError(f"unknown server {name!r}")
        self.servers = list(servers)
        self.creds_by_server = dict(creds_by_server or {})
        self.elicitation_callback = elicitation_callback
        self.env = dict(os.environ if env is None else env)
        self._stack: AsyncExitStack | None = None
        self._sessions: dict[str, tuple[str, object]] = {}  # target -> (server, session)

    async def __aenter__(self) -> "ServerPool":
        ClientSession, StdioServerParameters, stdio_client = _mcp_imports()
        self._stack = AsyncExitStack()
        await self._stack.__aenter__()
        try:
            await self._start_children(ClientSession, StdioServerParameters, stdio_client)
        except BaseException:
            # A fatal error (Ctrl-C, cancellation) escaping mid-startup means
            # __aexit__ never runs -- tear down the children we already own.
            await self.__aexit__(None, None, None)
            raise
        return self

    async def _start_children(self, ClientSession, StdioServerParameters, stdio_client):
        for server in self.servers:
            spec = SERVERS[server]
            cmd = npx_command(spec)
            params = StdioServerParameters(
                command=cmd[0], args=cmd[1:],
                env=build_child_env(self.env, self.creds_by_server.get(server)),
            )
            child = AsyncExitStack()
            try:
                async with asyncio.timeout(START_TIMEOUT_S):
                    read, write = await child.enter_async_context(stdio_client(params))
                    kwargs = ({"elicitation_callback": self.elicitation_callback}
                              if self.elicitation_callback else {})
                    session = await child.enter_async_context(
                        ClientSession(read, write, **kwargs))
                    await session.initialize()
            except BaseException as exc:  # noqa: BLE001 -- one child never kills the pool
                reason = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
                if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
                    reason = f"initialize timed out after {START_TIMEOUT_S:.0f}s"
                _log(f"✗ {server} failed to start: {reason[:140]}")
                try:
                    await child.aclose()
                except Exception:
                    pass  # a half-started child may be noisy on teardown
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
        except Exception as teardown:  # child exit races are non-actionable
            _log(f"(server pool teardown: {type(teardown).__name__})")
        finally:
            self._stack = None
            self._sessions.clear()
        return False

    async def list_tools(self) -> list[NamespacedTool]:
        """Paginated tools/list per child, merged and namespaced. A child that
        fails to list logs one line and contributes nothing -- same degradation
        the gateway gave a broken target on AWS."""
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
        """tools/call by namespaced name. Catches EVERY exception -- a failed
        tool is reported back to the model, never allowed to crash the loop."""
        try:
            target, tool = split_namespaced(namespaced)
            entry = self._sessions.get(target)
            if entry is None:
                raise RuntimeError(f"no running server for target {target!r}")
            _, session = entry
            res = await session.call_tool(tool, args or {})
            return {"text": _result_text(res),
                    "error": bool(getattr(res, "isError", False))}
        except Exception as exc:  # noqa: BLE001
            return {"text": f"tool call failed: {exc}", "error": True}
