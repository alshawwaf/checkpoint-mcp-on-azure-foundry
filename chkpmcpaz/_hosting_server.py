"""Responses-protocol entrypoint for the Foundry-hosted agent.

The container this runs in serves the Responses (OpenAI-compatible) protocol
on port 8088 via `azure-ai-agentserver-responses`; the platform gateway
handles inbound Entra auth and auto-serves `/readiness` -- we implement
NEITHER ourselves (contract of the protocol library).

This is the containerized twin of `chat --runtime local`: the response
handler reuses the exact same Claude -> ServerPool -> @chkp-tools loop
(chkpmcpaz.agent.run_task_captured), just wrapped in the Responses envelope.
The heavy lifting stays in chkpmcpaz.agent so the two runtimes can never
drift (same principle as the AWS chkpmcpaws._hosting_server).

Honest error reporting (AWS `8dd198f` parity): run_task_captured NEVER
raises -- an in-agent failure comes back as {"error": true, "result": ...},
and this handler surfaces it as a TextResponse prefixed `ERROR: ` so the CLI
(chkpmcpaz.hosting) can exit non-zero instead of printing a green done.

Fail-fast startup: main() verifies the required env vars BEFORE binding the
port, so a misconfigured version goes `failed` visibly in
`azd ai agent monitor` within seconds instead of erroring on the first
invoke minutes later. Only env-var NAMES are printed -- never values.

Session continuity comes from the platform-managed Responses history
(context.get_history(), fetched 20 items deep): prior turns are folded into
the task as a clearly-labeled PRIOR CONTEXT block, capped and marked
possibly-stale so the grounding rules still force tool verification.
"""

from __future__ import annotations

import asyncio
import os

from . import agent
from .config import (
    ENV_CLAUDE_BASE_URL,
    ENV_CONTENT_SAFETY,
    ENV_GUARDRAIL,
    ENV_KEY_VAULT_URI,
    ENV_MODEL,
    ENV_OPENAI_BASE_URL,
    ENV_PROVIDER,
    MEMORY_CONTEXT_MAX_CHARS,
    PROVIDER_AZURE_OPENAI,
    StackConfig,
    resolve_provider,
)


def _required_env():
    """The env vars the container must have to serve the ACTIVE provider (names
    only -- never values). A gpt (azure-openai) container needs OPENAI_BASE_URL;
    a Claude one needs CLAUDE_BASE_URL. The provider comes from CHKP_PROVIDER
    (or is detected from CHKP_MODEL), matching StackConfig.from_env() so the
    preflight checks exactly what the handler will read."""
    provider = os.environ.get(ENV_PROVIDER) or resolve_provider(
        None, os.environ.get(ENV_MODEL))
    return ((ENV_OPENAI_BASE_URL, ENV_KEY_VAULT_URI)
            if provider == PROVIDER_AZURE_OPENAI
            else (ENV_CLAUDE_BASE_URL, ENV_KEY_VAULT_URI))


def _guardrail_mode() -> str:
    """The container's screening mode from CHKP_GUARDRAIL (off/observe/enforce).
    The agent version baked this in at deploy (deploy --guardrail /
    CHKP_GUARDRAIL); it is NOT hardcoded off anymore."""
    from .guardrail import resolve_mode

    return resolve_mode(os.environ.get(ENV_GUARDRAIL))


def _preflight() -> None:
    """Fail fast on missing configuration -- names only, never values."""
    from .guardrail import GUARDRAIL_OFF

    missing = [k for k in _required_env() if not os.environ.get(k)]
    if _guardrail_mode() != GUARDRAIL_OFF and not os.environ.get(ENV_CONTENT_SAFETY):
        missing.append(ENV_CONTENT_SAFETY)
    if missing:
        raise SystemExit(
            "hosted agent misconfigured -- missing env vars: "
            + ", ".join(missing)
            + "  (set them on the agent version; redeploy: "
            "python3 -m chkpmcpaz deploy)")


def _agentserver_imports():
    """Lazy import: azure-ai-agentserver-responses is a container-only
    dependency (agent/requirements.txt), not part of the pip package."""
    try:
        from azure.ai.agentserver.responses import (
            ResponsesAgentServerHost,
            ResponsesServerOptions,
            TextResponse,
        )
    except ImportError as exc:
        raise SystemExit(
            "the hosted-agent server needs azure-ai-agentserver-responses "
            "(installed in the container via agent/requirements.txt):\n"
            "  pip install azure-ai-agentserver-responses==1.0.0b8") from exc
    return ResponsesAgentServerHost, ResponsesServerOptions, TextResponse


def _item_text(item) -> tuple[str, str]:
    """Best-effort (role, text) extraction from one Responses history item.
    History shapes vary by SDK version; anything unrecognized yields ''."""
    get = item.get if isinstance(item, dict) else \
        (lambda k, d=None: getattr(item, k, d))
    role = str(get("role", "") or "")
    content = get("content", None)
    if isinstance(content, str):
        return role, content
    parts = []
    for part in content or []:
        pget = part.get if isinstance(part, dict) else \
            (lambda k, d=None, p=part: getattr(p, k, d))
        text = pget("text", None)
        if isinstance(text, str):
            parts.append(text)
    return role, "\n".join(parts)


async def _history_context(context) -> str:
    """Fold the platform-managed conversation history into a PRIOR CONTEXT
    block (capped, stale-warned -- the grounding rules then force the model to
    re-verify with tools). Best-effort: any failure means no history, never a
    failed response."""
    try:
        history = await context.get_history()
    except Exception:  # noqa: BLE001 -- continuity is best-effort
        return ""
    lines = []
    for item in history or []:
        role, text = _item_text(item)
        text = (text or "").strip()
        if role and text:
            lines.append(f"{role}: {text}")
    if not lines:
        return ""
    block = "\n".join(lines)[-MEMORY_CONTEXT_MAX_CHARS:]
    return ("PRIOR CONTEXT (earlier turns of this conversation; may be stale "
            "-- verify with a tool before relying on it):\n" + block
            + "\n\nCURRENT TASK: ")


def _build_app():
    ResponsesAgentServerHost, ResponsesServerOptions, TextResponse = \
        _agentserver_imports()
    app = ResponsesAgentServerHost(
        options=ResponsesServerOptions(default_fetch_history_count=20),
    )

    @app.response_handler
    async def handler(request, context, _cancellation_signal: asyncio.Event):
        task = (await context.get_input_text() or "").strip()
        if not task:
            return TextResponse(context, request,
                                text="ERROR: no task in the request input")
        prior = await _history_context(context)
        cfg = StackConfig.from_env()
        from .guardrail import (
            GUARDRAIL_ENFORCE,
            GUARDRAIL_OBSERVE,
            screen_prompt,
        )

        mode = _guardrail_mode()
        # observe: screen and REPORT (to the container log / App Insights), but
        # never block -- so run_task_captured runs with guardrail=False. enforce:
        # run_task_captured screens and blocks internally (guardrail=True).
        # Provider-aware: screen_prompt honors CHKP_GUARDRAIL_PROVIDER (lakera or
        # content-safety), so observe reports the SAME engine enforce would use.
        if mode == GUARDRAIL_OBSERVE:
            try:
                flagged, label, detail = await asyncio.to_thread(screen_prompt, cfg, task)
                verdict = (f"attack DETECTED{' (' + detail + ')' if detail else ''} "
                           "-- would block in enforce mode; continuing"
                           if flagged else "screening passed")
                print(f"guardrail (observe · {label}): {verdict}", flush=True)
            except Exception as exc:  # noqa: BLE001 -- observe must not fail the run
                print(f"guardrail (observe): screening unavailable "
                      f"({type(exc).__name__}); continuing", flush=True)
        # run_task_captured is a synchronous facade (it owns its own asyncio
        # loop for the stdio children), so it runs in a worker thread here.
        result = await asyncio.to_thread(
            agent.run_task_captured,
            prior + task if prior else task,
            cfg,
            model=os.environ.get(ENV_MODEL) or None,
            guardrail=(mode == GUARDRAIL_ENFORCE),
        )
        text = str(result.get("result") or "")
        if result.get("guardrail_block"):
            # A block is a SUCCESS -- tag it so the CLI renders it as a security
            # win, never through the ERROR path.
            from .guardrail import GUARDRAIL_BLOCK_SENTINEL

            text = GUARDRAIL_BLOCK_SENTINEL + text
        elif result.get("error"):
            text = "ERROR: " + text
        return TextResponse(context, request, text=text)

    return app


def main() -> None:  # pragma: no cover -- exercised only inside the container
    _preflight()
    _build_app().run()


if __name__ == "__main__":  # pragma: no cover
    main()
