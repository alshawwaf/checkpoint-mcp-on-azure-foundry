"""A Check Point security-operations agent.

Claude (on Microsoft Foundry) reasons over your Check Point estate; every tool
call it makes goes through the stdio ServerPool of @chkp MCP child processes.
That is the full chain in one command:

    chat  ->  AnthropicFoundry (Claude)  ->  stdio ServerPool  ->  @chkp tools

One agent loop, two deployment targets (the `--runtime` flag):
  local   run the loop in THIS process (children spawned locally).
  hosted  the identical loop inside the Foundry Hosted Agent container
          (chkpmcpaz._hosting_server wraps run_task_captured).

Model calls use the `anthropic` package's AnthropicFoundry client -- the
Claude-on-Foundry surface, authenticated with DefaultAzureCredential on the
`https://ai.azure.com/.default` scope (locally your `az login`; hosted the
per-agent Entra identity). No agent framework: a plain messages tool-use loop,
mirroring the AWS repo's Converse loop turn for turn.

Model selection: the `model` argument is a DEPLOYMENT name (not a model id).
With no explicit model, CHKP_MODEL / CLAUDE_MODEL_DEPLOYMENT wins; otherwise
the agent probes MODEL_PREFERENCE with an 8-token message and uses the first
deployment that answers (access problems then surface as ModelUnavailable
with remediation, not a traceback).

Error posture (parity with the AWS agent): transient errors (429/5xx/timeouts)
retry with backoff; a mid-stream failure retries the whole stream with a
visible notice; tool failures are fed back to the model instead of crashing
the loop; run_task_captured NEVER raises -- the hosted envelope reports
{"error": true} honestly instead of a green "done".
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass, field

from . import gaia as gaia_mod
from . import keyvault
from .config import (
    DEFAULT_ACTOR,
    ENV_KEY_VAULT_URI,
    ENV_MCP_TRANSPORT,
    ENV_REMOTE_AUDIENCE,
    ENV_REMOTE_ENDPOINTS,
    MAX_TOKENS,
    MAX_TURNS,
    MODEL_PREFERENCE,
    SERVERS,
    SYSTEM_PROMPT,
    TOOL_RESULT_MAX_CHARS,
    TRANSPORT_REMOTE,
    StackConfig,
    sanitize_id,
)
from .guardrail import GuardrailBlocked, active_engine_name, screen_prompt
from .mcp_stdio import NamespacedTool, ServerPool
from .providers import (
    AnthropicProvider,
    ModelUnavailable,
    ToolCall,
    TurnResult,
    dedupe_names,
    get_provider,
    sanitize_tool_name,
)

__all__ = [
    "MODEL_PREFERENCE", "MAX_TURNS", "MAX_TOKENS",
    "AgentResult", "ModelUnavailable", "GuardrailBlocked",
    "run_task", "run_task_captured",
    "sanitize_tool_name", "dedupe_names", "build_tools", "telemetry_line",
    "make_client", "pick_model", "is_transient", "first_api_error",
    "callable_deployments", "is_model_access_error",
]

# The one agent loop is provider-agnostic (chkpmcpaz.providers). ModelUnavailable,
# sanitize_tool_name and dedupe_names now live in providers and are re-exported
# above for backward compatibility. The module-level make_client/pick_model/
# build_tools/... below stay as thin shims over the ACTIVE provider so tests that
# monkeypatch `agent.make_client` / `agent.pick_model` keep intercepting, and so
# the anthropic default is byte-for-byte the old behavior.
_ANTHROPIC = AnthropicProvider()


@dataclass
class AgentResult:
    text: str
    usage: dict = field(default_factory=dict)
    model: str = ""
    error: bool = False


# Usage fields summed across turns (Anthropic Messages API names).
USAGE_FIELDS = ("input_tokens", "output_tokens",
                "cache_read_input_tokens", "cache_creation_input_tokens")

# HTTP statuses worth retrying (capacity/model-side), as opposed to 401/403/
# 404 which are terminal and must surface immediately.
_TRANSIENT_STATUS = {429, 500, 503, 529}

# Statuses meaning "this identity cannot use this deployment" (as opposed to a
# transient/capacity error): 401 wrong scope, 403 missing role, 404 no such
# deployment. Used to give next-best-model guidance instead of a bare failure.
_MODEL_ACCESS_STATUS = {401, 403, 404}


# =============================================================================
# Backward-compat shims over the ACTIVE (or Anthropic default) provider.
# sanitize_tool_name / dedupe_names / ModelUnavailable are imported from
# providers above; these keep `agent.build_tools` / `agent.is_transient` /
# `agent.first_api_error` importable and monkeypatch-safe -- tests patch the
# module-level names and the loop below reads them back (it passes the ACTIVE
# provider so a gpt run still dispatches correctly, while a patched
# `agent.build_tools`/`agent.is_transient`/`agent.pick_model` intercepts).
# Each shim takes an optional `provider`; None means the Anthropic default, so
# an old-signature call (the fakes in tests/test_agent.py) is byte-for-byte the
# previous behavior.
# =============================================================================
def build_tools(mcp_tools: list[NamespacedTool], provider=None
                ) -> tuple[list[dict], dict[str, str]]:
    """MCP catalog -> (provider tools list, {api_name: namespaced}) with a
    reversible name map. Defaults to the Anthropic (production) tool wire format
    -- including the cache_control marker on the last tool -- when no provider is
    passed; the loop passes the active provider so a gpt run gets the OpenAI
    function-tool shape."""
    return (provider or _ANTHROPIC).format_tools(mcp_tools)


def is_transient(exc: BaseException, provider=None) -> bool:
    """Retryable? Connection/timeout failures and 429/500/503/529 statuses.
    AuthenticationError/NotFound/BadRequest are terminal -- surface at once.
    Defaults to the Anthropic classifier; the retry wrapper passes the active
    provider so a gpt run classifies openai errors."""
    return (provider or _ANTHROPIC).is_transient(exc)


def first_api_error(exc: BaseException, provider=None) -> BaseException:
    """Pull the first provider APIError out of a possibly-nested
    BaseExceptionGroup. The MCP client's anyio task group re-wraps errors
    raised inside it, so a model API error does NOT arrive bare -- without
    unwrapping it escapes as a raw ExceptionGroup traceback. Returns the
    original exception when no APIError is nested inside. Defaults to the
    Anthropic SDK's APIError base; callers on the gpt path pass the provider."""
    return (provider or _ANTHROPIC).first_api_error(exc)


def telemetry_line(usage: dict) -> str:
    """One-line token telemetry; the cache-hit percentage makes the prompt
    caching win visible run to run."""
    tokens_in = usage.get("input_tokens", 0) or 0
    tokens_out = usage.get("output_tokens", 0) or 0
    cache_read = usage.get("cache_read_input_tokens", 0) or 0
    seen_input = cache_read + tokens_in
    pct = round(100 * cache_read / seen_input) if seen_input else 0
    return (f"tokens  {tokens_in:,} in · {tokens_out:,} out · "
            f"{cache_read:,} cache-read · {pct}% of input from cache")


# =============================================================================
# Client + model selection -- module-level shims over the ACTIVE (or Anthropic
# default) provider. make_client dispatches on cfg.provider (so a gpt stack
# builds an AzureOpenAI client); pick_model / callable_deployments /
# is_model_access_error default to the Anthropic flavor (the shims tests
# monkeypatch) but accept the active provider so the loop dispatches correctly
# on a gpt run while `agent.pick_model` monkeypatching still intercepts
# run_task's auto-selection. `preference` is the caller's list; for the loop's
# provider-aware call it is that provider's own preference().
# =============================================================================
def make_client(cfg: StackConfig):
    """Build the model client for the ACTIVE provider (cfg.provider). For
    anthropic this is AnthropicFoundry on the /anthropic route; for azure-openai
    the classic AzureOpenAI client on the account root. The bearer provider
    refreshes per request; hosted it resolves to the per-agent Entra identity,
    locally to your az login."""
    return get_provider(cfg.provider).make_client(cfg)


def pick_model(client, preference: list[str] = MODEL_PREFERENCE, provider=None) -> str:
    """Probe each deployment with a tiny 8-token message and return the first
    that answers -- deployments (and RBAC) vary per stack, so there is no single
    name guaranteed to work. Total failure raises ModelUnavailable listing
    exactly what was tried. Defaults to the Anthropic probe; run_task passes the
    active provider (and its preference) so a gpt run probes gpt deployments."""
    return (provider or _ANTHROPIC).pick_model(client, preference)


def callable_deployments(client, preference: list[str] = MODEL_PREFERENCE,
                         provider=None) -> list[str]:
    """Which of the preferred deployments answer an 8-token probe RIGHT NOW with
    this client's identity -- the 'here are the models you CAN call' list the
    denied-access guidance prints (mirrors the AWS re-probe on denial). Never
    raises: a per-deployment failure just excludes that deployment. Defaults to
    the Anthropic probe; callers pass the active provider for the gpt path."""
    return (provider or _ANTHROPIC).callable_deployments(client, preference)


def is_model_access_error(exc: BaseException, provider=None) -> bool:
    """True when `exc` (possibly nested in a BaseExceptionGroup) is a model API
    error whose status means the deployment is not usable by this identity
    (401/403/404) -- as opposed to a transient/capacity error, which is retried.
    Defaults to the Anthropic classifier; callers pass the active provider."""
    return (provider or _ANTHROPIC).is_model_access_error(exc)


# =============================================================================
# Streaming with retry (runs in a worker thread; the asyncio loop owns the
# stdio children, so blocking sleeps are fine here). Provider-agnostic: it owns
# only the bounded transient-retry + the mid-stream notice; the provider owns
# the single SDK stream attempt and normalizes it into a TurnResult.
# =============================================================================
def _stream_turn(provider, client, deployment, system, messages, tools, on_text,
                 attempts=3, base_delay=1.5):
    """One streamed model turn with bounded retry on transient errors. A
    mid-stream failure retries the WHOLE stream; if text already reached the
    user, a retry notice explains the repeated text (AWS parity)."""
    last: Exception | None = None
    for attempt in range(attempts):
        emitted = {"any": False}

        def track(delta, _e=emitted):
            _e["any"] = True
            if on_text:
                on_text(delta)

        try:
            return provider.stream_turn(client, deployment, system, messages,
                                        tools, track)
        except Exception as exc:  # noqa: BLE001 -- retry only what is transient
            last = exc
            # Module-level shim (monkeypatch-safe), passing the active provider
            # so a gpt run classifies openai errors and a patched
            # `agent.is_transient` still steers the retry decision.
            if is_transient(exc, provider) and attempt < attempts - 1:
                if emitted["any"] and on_text:
                    on_text("\n… model stream error — retrying …\n")
                time.sleep(base_delay * (attempt + 1))
                continue
            raise
    raise last  # pragma: no cover (loop always returns or raises)


# =============================================================================
# The agentic loop
# =============================================================================
def _line(out, text: str) -> None:
    out.write(text + "\n")
    if hasattr(out, "flush"):
        out.flush()


def _load_server_creds(cfg: StackConfig, out) -> dict[str, dict[str, str]]:
    """Fetch each credentialed server's Key Vault secret at startup (the boot-
    time fetch the AWS entrypoint did with Secrets Manager). Placeholder bodies
    are still injected -- the server starts and fails auth cleanly until real
    creds are applied. Per-secret failures degrade that one server; only NAMES
    are ever printed."""
    creds: dict[str, dict[str, str]] = {}
    if not cfg.key_vault_uri:
        _line(out, f"⚠ {ENV_KEY_VAULT_URI} is not set -- @chkp servers start "
                   "without credentials (tools will fail auth)")
        return creds
    for server in cfg.servers:
        if not SERVERS[server].creds:
            continue
        name = cfg.secret_name(server)
        try:
            payload = keyvault.get_secret_json(cfg.key_vault_uri, name)
        except Exception as exc:  # noqa: BLE001 -- degrade one server, keep going
            _line(out, f"⚠ could not read secret {name} "
                       f"({type(exc).__name__}) -- {server} starts without credentials")
            continue
        if payload:
            creds[server] = payload
    return creds


def _select_pool(cfg: StackConfig, creds_by_server: dict, elicitation_cb, env=None):
    """The MCP transport for this run. Default: the stdio ServerPool (each @chkp
    server as a local child). When CHKP_MCP_TRANSPORT=remote AND a remote
    endpoint catalog exists (from `deploy --remote-mcp`), use the streamable-HTTP
    RemoteServerPool instead -- proving the SAME agent can consume the shared,
    Entra-authenticated remote tier. Missing endpoints fall back to stdio with a
    note rather than failing the run."""
    e = dict(os.environ if env is None else env)
    if (e.get(ENV_MCP_TRANSPORT) or "").strip().lower() == TRANSPORT_REMOTE:
        from .mcp_remote import RemoteServerPool, parse_endpoints

        endpoints = parse_endpoints(e.get(ENV_REMOTE_ENDPOINTS))
        audience = e.get(ENV_REMOTE_AUDIENCE)
        if not endpoints:
            # A local shell has CHKP_MCP_TRANSPORT set but the catalog lives in
            # the azd env -- hydrate it from there.
            try:
                from .azutil import azd_env_values

                azenv = azd_env_values(cfg.prefix)
                endpoints = parse_endpoints(azenv.get(ENV_REMOTE_ENDPOINTS))
                audience = audience or azenv.get(ENV_REMOTE_AUDIENCE)
            except Exception:  # noqa: BLE001 -- fall back to stdio below
                endpoints = []
        subset = [ep for ep in endpoints if ep["server"] in set(cfg.servers)]
        endpoints = subset or endpoints
        if endpoints:
            print(f"MCP transport: remote ({len(endpoints)} endpoint(s) · Entra "
                  "Easy Auth)", flush=True)
            return RemoteServerPool(endpoints, audience=audience)
        print("CHKP_MCP_TRANSPORT=remote but no remote endpoints are deployed "
              "(run deploy --remote-mcp) -- using the local stdio transport.",
              flush=True)
    return ServerPool(list(cfg.servers), creds_by_server,
                      elicitation_callback=elicitation_cb)


async def _agent_loop(client, deployment: str, cfg: StackConfig, task: str,
                      creds_by_server: dict, elicitation_cb, out) -> dict:
    provider = get_provider(cfg.provider)
    async with _select_pool(cfg, creds_by_server, elicitation_cb) as pool:
        mcp_tools = await pool.list_tools()
        if not mcp_tools:
            raise RuntimeError(
                "no MCP tools available -- every @chkp server failed to start "
                "(see the ✗ lines above); fix credentials/network and re-run")
        # Provider-native tool schema + the seed (system, messages). For
        # anthropic the system is a cache_control'd block list AND the last tool
        # carries a cache marker (the whole tool block -- hundreds of schemas
        # under --servers all -- is served from cache after the first turn); for
        # azure-openai the system prompt is the first message (no cache marker,
        # OpenAI caches automatically). Recalled/agent-varying context goes AFTER
        # these blocks so it can never invalidate the cached static prefix.
        tools, name_map = build_tools(mcp_tools, provider)   # module shim (monkeypatch-safe)
        system, messages = provider.build_conversation(SYSTEM_PROMPT, task)

        _line(out, f"{len(mcp_tools)} tools discovered from "
                   f"{len(pool.servers)} @chkp servers · prompt caching on")

        # Live token streaming: assistant text prints as it arrives.
        stream_state = {"open": False}

        def on_text(delta: str) -> None:
            if not stream_state["open"]:
                out.write("assistant  ")
                stream_state["open"] = True
            out.write(delta)
            if hasattr(out, "flush"):
                out.flush()

        totals = {k: 0 for k in USAGE_FIELDS}
        last_answer = ""
        for _turn in range(MAX_TURNS):
            stream_state["open"] = False
            turn = await asyncio.to_thread(
                _stream_turn, provider, client, deployment, system, messages,
                tools, on_text)
            if stream_state["open"]:
                out.write("\n")
            for key in USAGE_FIELDS:
                totals[key] += turn.usage.get(key, 0) or 0
            messages.append(provider.format_assistant_message(turn))

            if turn.text.strip():
                last_answer = turn.text.strip()

            if not turn.is_tool_use:
                break

            outcomes = []
            for tc in turn.tool_calls:
                namespaced = name_map.get(tc.name, tc.name)
                args = dict(tc.input or {})
                _line(out, f"→ tool {namespaced}  {json.dumps(args)[:120]}")
                outcome = await pool.call(namespaced, args)
                glyph = "✗ error" if outcome["error"] else "✓ ok"
                _line(out, f"  {glyph}  {outcome['text'][:140].replace(chr(10), ' ')}")
                outcomes.append(outcome)
            messages.extend(provider.format_tool_results(
                turn.tool_calls, outcomes, TOOL_RESULT_MAX_CHARS))
        else:
            _line(out, f"stopped after {MAX_TURNS} turns (turn budget reached)")

        _line(out, telemetry_line(totals))
        _line(out, "✔ done")
        return {"answer": last_answer, "usage": dict(totals)}


# =============================================================================
# Entry points
# =============================================================================
def run_task(task: str, cfg: StackConfig, *, model: str | None = None,
             guardrail: bool = False, session: str | None = None,
             actor: str = DEFAULT_ACTOR, out=None) -> AgentResult:
    """Run one task through the full loop, streaming progress to `out`.
    Raises on terminal failures (the CLI classifies credential errors);
    GuardrailBlocked when Prompt Shields detects an attack in the input."""
    out = sys.stdout if out is None else out
    task = str(task or "").strip()
    if not task:
        raise ValueError("empty task -- give the agent a question to answer")

    # Guardrail runs BEFORE any model call: a detected attack never reaches
    # Claude (the Azure analogue of the AWS gateway-side policy deny).
    if guardrail:
        from . import ui

        # Show WHAT is happening before the (network) screen so the wait never
        # looks frozen -- flush so the line lands before the blocking call.
        _line(out, ui.muted(f"guardrail  screening the prompt with {active_engine_name()}…"))
        getattr(out, "flush", lambda: None)()
        flagged, label, detail = screen_prompt(cfg, task)
        if flagged:
            raise GuardrailBlocked(label=label, detail=detail)
        _line(out, ui.ok(f"guardrail  {label} — no attack detected, prompt allowed ✓"))

    provider = get_provider(cfg.provider)
    client = make_client(cfg)                      # module-level shim (monkeypatch-safe)
    # Auto-select through the module-level pick_model shim (monkeypatch-safe),
    # passing the active provider and ITS preference so a gpt run probes gpt
    # deployments while a patched `agent.pick_model` still intercepts.
    deployment = (model or cfg.configured_deployment
                  or pick_model(client, provider.preference(), provider))

    if session:
        # Honest note: conversation continuity lives in the hosted platform's
        # Responses history; this loop itself is stateless per task.
        _line(out, f"session {sanitize_id(session)} · actor "
                   f"{sanitize_id(actor or DEFAULT_ACTOR)}  (continuity comes "
                   "from the hosted platform history; the loop is stateless)")

    creds_by_server = _load_server_creds(cfg, out)
    gaia_cb = gaia_mod.make_elicitation_callback(gaia_mod.load_gaia_creds(cfg))
    if gaia_cb:
        _line(out, "gaia login answerer armed (stdio relays elicitation, so it "
                   "fires when a quantum-gaia tool asks to log in)")

    result = asyncio.run(_agent_loop(
        client, deployment, cfg, task, creds_by_server, gaia_cb, out))
    return AgentResult(text=result["answer"], usage=result["usage"],
                       model=deployment, error=False)


def run_task_captured(task: str, cfg: StackConfig, *, model: str | None = None,
                      guardrail: bool = False, session: str | None = None,
                      actor: str = DEFAULT_ACTOR) -> dict:
    """Run one task and RETURN the result dict (instead of raising). Used by
    the Foundry-hosted server (chkpmcpaz._hosting_server) so the container
    reuses the EXACT same loop as `chat --runtime local`. NEVER raises: any
    failure becomes {"error": True, ...} so the hosted envelope can report it
    honestly instead of crashing the worker (AWS `8dd198f` parity)."""
    try:
        res = run_task(task, cfg, model=model, guardrail=guardrail,
                       session=session, actor=actor, out=sys.stdout)
        return {"result": res.text, "usage": res.usage,
                "model": res.model, "error": res.error}
    except GuardrailBlocked as gb:
        # A block is the guardrail SUCCEEDING -- report it as a distinct,
        # non-error outcome so the CLI renders a security win, not a failure.
        return {"result": str(gb), "guardrail_block": True, "error": False,
                "usage": {}, "model": model or cfg.configured_deployment or ""}
    except Exception as raw:  # noqa: BLE001 -- report, never crash the runtime
        # Unwrap through the active provider so a gpt run surfaces the openai
        # APIError, not the raw ExceptionGroup (module shim stays monkeypatch-safe).
        exc = first_api_error(raw, get_provider(cfg.provider))
        return {"result": f"{type(exc).__name__}: {str(exc)[:300]}",
                "usage": {}, "model": model or cfg.configured_deployment or "",
                "error": True}
