"""Model-provider abstraction: one agent loop, two wire formats.

ARCHITECT-SCAFFOLDED, RUNTIME-BUILDER-OWNED. This module isolates the two
genuinely-different model surfaces Azure exposes so `chkpmcpaz.agent` can keep a
single provider-agnostic tool-use loop (turn budget, tool dispatch, printed
lines, truncation, telemetry, guardrail, creds/gaia -- all unchanged):

  * AnthropicProvider  -- Claude on Foundry (anthropic.AnthropicFoundry, the
    Anthropic Messages API on the /anthropic route). PRODUCTION. This is the
    existing agent.py logic, moved here verbatim (refactor-only, zero behavior
    change).
  * AzureOpenAIProvider -- first-party Azure OpenAI (openai SDK, Chat
    Completions: `tools` function schema, `tool_calls`, role:"tool" messages).
    The cheap test target (gpt-5-mini), the Azure analogue of Amazon Nova.

On AWS both Nova and Claude speak ONE wire format (Bedrock Converse), so the AWS
repo only needed name->label detection. On Azure the two targets are different
SDKs and message shapes, so this Provider interface is required.

--------------------------------------------------------------------------------
STATUS: interface scaffolding. The RUNTIME builder fills in every method body
below (they currently raise NotImplementedError) and wires agent.py to call
through `get_provider(cfg.provider)`. The method signatures, the ToolCall /
TurnResult dataclasses, the get_provider() registry, and the shared
sanitize_tool_name / dedupe_names helpers are FROZEN -- other builders code
against them and must not need to change when the bodies land.
--------------------------------------------------------------------------------
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import anthropic
import openai

from .config import (
    AI_SCOPE,
    COGNITIVE_SCOPE,
    ENV_CLAUDE_BASE_URL,
    ENV_OPENAI_BASE_URL,
    MAX_TOKENS,
    MODEL_PREFERENCE,
    OPENAI_API_VERSION,
    OPENAI_MAX_TOOLS,
    OPENAI_MODEL_PREFERENCE,
    PROVIDER_ANTHROPIC,
    PROVIDER_AZURE_OPENAI,
    TOOL_DESCRIPTION_MAX_CHARS,
)
from .azutil import log

# Usage fields summed across turns (Anthropic Messages API names -- the OpenAI
# provider normalizes prompt/completion tokens INTO these keys in usage_of, so
# agent.telemetry_line stays provider-agnostic). Mirrors agent.USAGE_FIELDS.
USAGE_FIELDS = ("input_tokens", "output_tokens",
                "cache_read_input_tokens", "cache_creation_input_tokens")

# HTTP statuses worth retrying (capacity/model-side), shared by both providers'
# is_transient. 401/403/404 are terminal -- see _MODEL_ACCESS_STATUS.
_TRANSIENT_STATUS = {429, 500, 503, 529}

# Statuses meaning "this identity cannot use this deployment" (wrong scope /
# missing role / no such deployment), as opposed to a transient/capacity error.
_MODEL_ACCESS_STATUS = {401, 403, 404}


class ModelUnavailable(RuntimeError):
    """No deployment for the active provider answered the capability probe.
    Defined here (not agent.py) so both providers can raise it; agent.py
    re-exports it for backward compatibility (`agent.ModelUnavailable`)."""


# =============================================================================
# Neutral data carried between the loop and a provider (never a raw SDK object)
# =============================================================================
@dataclass
class ToolCall:
    """One tool invocation the model asked for, provider-normalized.
    `input` is ALWAYS a parsed dict (the OpenAI provider json.loads the
    `arguments` string; the Anthropic provider passes `block.input` through)."""
    id: str                 # tool_use_id (Anthropic) / tool_call_id (OpenAI)
    name: str               # the API tool name the model emitted (pre name_map)
    input: dict


@dataclass
class TurnResult:
    """The outcome of one streamed model turn, provider-normalized so
    `agent._agent_loop` never touches a raw SDK object.

      text          concatenated assistant text for this turn (§1 L367)
      tool_calls    parsed tool calls, [] when none
      is_tool_use   True when the model wants tools (Anthropic stop_reason ==
                    "tool_use" / OpenAI finish_reason == "tool_calls")  (§1 L372)
      usage         normalized to the four config USAGE_FIELDS keys
                    (input_tokens, output_tokens, cache_read_input_tokens,
                    cache_creation_input_tokens) so telemetry_line stays as-is
      assistant_message  the provider-native assistant turn to append to
                    `messages` (Anthropic: {"role":"assistant","content":blocks};
                    OpenAI: {"role":"assistant","content":..,"tool_calls":[..]})
    """
    text: str
    tool_calls: list[ToolCall]
    is_tool_use: bool
    usage: dict = field(default_factory=dict)
    assistant_message: Any = None


# =============================================================================
# Shared, provider-AGNOSTIC tool-name helpers (both APIs require the same
# [a-zA-Z0-9_-]{1,64} tool-name charset). Kept here so both providers' and
# agent.py's build_tools share ONE implementation; agent.py re-exports them.
# =============================================================================
def sanitize_tool_name(name: str) -> str:
    """Tool names must match [a-zA-Z0-9_-]{1,64}. Namespaced MCP names are
    usually fine but can exceed 64 chars or carry odd characters -- sanitize,
    never return empty."""
    return re.sub(r"[^a-zA-Z0-9_-]", "_", name or "")[:64] or "tool"


def dedupe_names(names: list[str]) -> list[str]:
    """De-duplicate sanitized names with '_1'-style suffixes (truncation can
    collide two long names), preserving order and the 64-char cap."""
    seen: set[str] = set()
    out: list[str] = []
    for name in names:
        safe, i = name, 1
        while safe in seen:
            suffix = f"_{i}"
            safe = name[: 64 - len(suffix)] + suffix
            i += 1
        seen.add(safe)
        out.append(safe)
    return out


# =============================================================================
# Provider protocol -- methods map 1:1 to the agent.py seams (CODE-MAP §1)
# =============================================================================
@runtime_checkable
class Provider(Protocol):
    name: str                              # "anthropic" | "azure-openai"

    def make_client(self, cfg) -> Any:
        """Build the SDK client for this provider from a StackConfig. Raise
        RuntimeError with a deploy hint when the provider's base-url is unset
        (§1 make_client, agent.py L189)."""

    def preference(self) -> list[str]:
        """The auto-selection preference list (config.MODEL_PREFERENCE vs
        OPENAI_MODEL_PREFERENCE)."""

    def resolve_deployment(self, cfg, model: str | None, client) -> str:
        """The deployment to use for this run: `model` (explicit --model/
        CHKP_MODEL) or cfg.configured_deployment or pick_model(client). This is
        the provider-aware form of agent.py L424."""

    def pick_model(self, client, preference: list[str] | None = None) -> str:
        """Probe each preferred deployment with a tiny 8-token message; return
        the first that answers. Raise ModelUnavailable listing what was tried
        (§1 pick_model, agent.py L205)."""

    def callable_deployments(self, client,
                             preference: list[str] | None = None) -> list[str]:
        """Which preferred deployments answer an 8-token probe RIGHT NOW. Never
        raises -- a per-deployment failure just excludes it (§1 L229)."""

    def format_tools(self, mcp_tools) -> tuple[list[dict], dict[str, str]]:
        """MCP catalog -> (provider tool schema list, {api_name: namespaced}).
        Anthropic: {"name","description","input_schema"} + cache_control on the
        LAST tool. OpenAI: {"type":"function","function":{"name","description",
        "parameters"}}, no cache marker (§1 build_tools, agent.py L126)."""

    def build_conversation(self, system_prompt: str,
                           task: str) -> tuple[Any, list[dict]]:
        """Seed (system, messages) for this provider. Anthropic returns the
        cache-controlled system block list + [{"role":"user","content":task}].
        OpenAI returns (None, [{"role":"system",...},{"role":"user",...}]) --
        it has no separate system param (§1 L336/L354)."""

    def stream_turn(self, client, deployment, system, messages, tools,
                    on_text) -> TurnResult:
        """Stream ONE model turn (owns the SDK stream call + tool-call
        accumulation) and return a normalized TurnResult. Bounded transient
        retry with the mid-stream "model stream error -- retrying" notice is
        the shared wrapper in agent.py; this owns only the single attempt's SDK
        call (§1 _stream_turn, agent.py L257)."""

    def format_assistant_message(self, turn: TurnResult) -> dict:
        """The assistant turn to append to `messages` (§1 L365)."""

    def format_tool_results(self, tool_calls: list[ToolCall],
                            outcomes: list[dict], max_chars: int) -> list[dict]:
        """The message(s) carrying tool outputs back to the model, truncated to
        max_chars. Anthropic returns a SINGLE user message with tool_result
        blocks; OpenAI returns ONE {"role":"tool",...} message PER call. Return
        a LIST either way; agent.py `messages.extend(...)` it (§1 L385)."""

    def is_transient(self, exc: BaseException) -> bool:
        """Retryable connection/timeout or 429/500/503/529 (§1 L147)."""

    def is_model_access_error(self, exc: BaseException) -> bool:
        """401/403/404 -- this identity cannot use the deployment (§1 L246)."""

    def first_api_error(self, exc: BaseException) -> BaseException:
        """Unwrap the first provider APIError from a (possibly nested)
        BaseExceptionGroup; return exc unchanged when none is nested (§1 L165)."""

    def usage_of(self, raw_usage) -> dict:
        """Normalize a provider usage object into the four config USAGE_FIELDS
        keys. OpenAI maps prompt_tokens->input_tokens, completion_tokens->
        output_tokens, prompt_tokens_details.cached_tokens->
        cache_read_input_tokens, and 0 for cache_creation_input_tokens."""


# =============================================================================
# Shared APIError unwrapping -- the MCP client's anyio task group re-wraps errors
# raised inside it, so a model API error does NOT arrive bare: without unwrapping
# it escapes as a raw (possibly nested) BaseExceptionGroup traceback. Parametrized
# by the SDK's APIError base so both providers reuse ONE recursion (agent.py's
# old _find_api_error, generalized).
# =============================================================================
def _find_api_error(exc: BaseException, api_error_cls: type) -> BaseException | None:
    if isinstance(exc, api_error_cls):
        return exc
    for inner in getattr(exc, "exceptions", None) or []:
        found = _find_api_error(inner, api_error_cls)
        if found is not None:
            return found
    return None


# =============================================================================
# Implementations
# =============================================================================
class AnthropicProvider:
    """Claude on Foundry (anthropic.AnthropicFoundry, the Anthropic Messages API
    on the /anthropic route). PRODUCTION. This is the agent.py logic moved here
    verbatim -- refactor-only, zero behavior change: tests/test_agent.py drives
    this path unchanged."""

    name = PROVIDER_ANTHROPIC

    def preference(self) -> list[str]:
        return list(MODEL_PREFERENCE)

    def make_client(self, cfg):
        """AnthropicFoundry against this stack's /anthropic route. The bearer
        provider refreshes per request; inside the hosted container the
        credential resolves to the per-agent Entra identity, locally to your
        az login."""
        if not cfg.claude_base_url:
            raise RuntimeError(
                f"{ENV_CLAUDE_BASE_URL} is not set -- deploy the stack first: "
                "python3 -m chkpmcpaz deploy")
        from azure.identity import DefaultAzureCredential, get_bearer_token_provider
        return anthropic.AnthropicFoundry(
            azure_ad_token_provider=get_bearer_token_provider(
                DefaultAzureCredential(), AI_SCOPE),
            base_url=cfg.claude_base_url,
        )

    def resolve_deployment(self, cfg, model, client) -> str:
        return model or cfg.claude_deployment or self.pick_model(client)

    def pick_model(self, client, preference: list[str] | None = None) -> str:
        """Probe each deployment with a tiny 8-token message and return the first
        that answers -- deployments (and RBAC on the /anthropic route) vary per
        stack, so there is no single name guaranteed to work. Total failure
        raises ModelUnavailable listing exactly what was tried."""
        preference = self.preference() if preference is None else preference
        tried = []
        for deployment in preference:
            try:
                client.messages.create(
                    model=deployment, max_tokens=8,
                    messages=[{"role": "user", "content": "hi"}])
                return deployment
            except Exception as raw:  # noqa: BLE001 -- classify below, keep probing
                exc = self.first_api_error(raw)
                tried.append(f"{deployment} ({type(exc).__name__})")
        raise ModelUnavailable(
            "no Claude deployment answered an 8-token probe -- tried: "
            + ", ".join(tried)
            + ". Are the deployments provisioned and does this identity hold "
            "'Cognitive Services User' at account scope?  List the deployments "
            "you CAN call and their state with:  python3 -m chkpmcpaz models "
            "status  (re-provision them with:  python3 -m chkpmcpaz deploy)")

    def callable_deployments(self, client,
                             preference: list[str] | None = None) -> list[str]:
        """Which of the preferred deployments answer an 8-token probe RIGHT NOW
        with this client's identity. Never raises: a per-deployment failure just
        excludes that deployment."""
        preference = self.preference() if preference is None else preference
        ok: list[str] = []
        for deployment in preference:
            try:
                client.messages.create(
                    model=deployment, max_tokens=8,
                    messages=[{"role": "user", "content": "hi"}])
                ok.append(deployment)
            except Exception:  # noqa: BLE001 -- probing; a failure just excludes it
                pass
        return ok

    def format_tools(self, mcp_tools) -> tuple[list[dict], dict[str, str]]:
        """MCP catalog -> (Anthropic tools list, {api_name: namespaced}) with a
        reversible name map. The LAST tool dict gets a cache_control marker so
        the whole tool-schema block -- by far the largest static payload -- is
        served from the prompt cache on every turn after the first."""
        api_names = dedupe_names([sanitize_tool_name(t.namespaced) for t in mcp_tools])
        tools: list[dict] = []
        name_map: dict[str, str] = {}
        for tool, api_name in zip(mcp_tools, api_names):
            name_map[api_name] = tool.namespaced
            desc = (tool.description or tool.namespaced).strip() or tool.namespaced
            tools.append({
                "name": api_name,
                "description": desc[:TOOL_DESCRIPTION_MAX_CHARS],
                "input_schema": tool.input_schema or {"type": "object", "properties": {}},
            })
        if tools:
            tools[-1] = {**tools[-1], "cache_control": {"type": "ephemeral"}}
        return tools, name_map

    def build_conversation(self, system_prompt: str,
                           task: str) -> tuple[Any, list[dict]]:
        """Cache-controlled system block list + the seed user turn. Prompt
        caching: cache_control on the static system prompt so it (and the tool
        block) is served from cache on every turn after the first."""
        system = [{"type": "text", "text": system_prompt,
                   "cache_control": {"type": "ephemeral"}}]
        return system, [{"role": "user", "content": task}]

    def stream_turn(self, client, deployment, system, messages, tools,
                    on_text) -> TurnResult:
        """ONE streamed Anthropic turn (the bounded transient-retry wrapper lives
        in agent._stream_turn). Streams text_stream deltas to on_text, then
        normalizes the final message into a TurnResult."""
        with client.messages.stream(
            model=deployment, max_tokens=MAX_TOKENS,
            system=system, messages=messages, tools=tools,
        ) as stream:
            for delta in stream.text_stream:
                if on_text:
                    on_text(delta)
            final = stream.get_final_message()
        text = "".join(b.text for b in final.content
                       if getattr(b, "type", "") == "text")
        tool_calls = [ToolCall(id=b.id, name=b.name, input=dict(b.input or {}))
                      for b in final.content
                      if getattr(b, "type", "") == "tool_use"]
        return TurnResult(
            text=text,
            tool_calls=tool_calls,
            is_tool_use=(final.stop_reason == "tool_use"),
            usage=self.usage_of(final.usage),
            assistant_message={"role": "assistant", "content": final.content},
        )

    def format_assistant_message(self, turn: TurnResult) -> dict:
        return turn.assistant_message

    def format_tool_results(self, tool_calls: list[ToolCall],
                            outcomes: list[dict], max_chars: int) -> list[dict]:
        """ONE user message carrying every tool_result block (Anthropic keys them
        by tool_use_id and flags is_error), truncated to max_chars."""
        return [{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tc.id,
             "content": o["text"][:max_chars], "is_error": o["error"]}
            for tc, o in zip(tool_calls, outcomes)]}]

    def is_transient(self, exc: BaseException) -> bool:
        """Retryable? Connection/timeout failures and 429/500/503/529 statuses.
        AuthenticationError/NotFound/BadRequest are terminal -- surface at once."""
        if isinstance(exc, (anthropic.APIConnectionError, anthropic.APITimeoutError)):
            return True
        return getattr(exc, "status_code", None) in _TRANSIENT_STATUS

    def is_model_access_error(self, exc: BaseException) -> bool:
        """True when `exc` (possibly nested in a BaseExceptionGroup) is an
        Anthropic API error whose status means the deployment is not usable by
        this identity (401/403/404) -- as opposed to a transient/capacity error."""
        return getattr(self.first_api_error(exc), "status_code", None) in _MODEL_ACCESS_STATUS

    def first_api_error(self, exc: BaseException) -> BaseException:
        return _find_api_error(exc, anthropic.APIError) or exc

    def usage_of(self, raw_usage) -> dict:
        """Anthropic usage names already match USAGE_FIELDS; a None usage (or a
        missing field) contributes zeros."""
        return {k: getattr(raw_usage, k, 0) or 0 for k in USAGE_FIELDS}


class AzureOpenAIProvider:
    """First-party Azure OpenAI (gpt-5-mini) -- the cheap test target, the Azure
    analogue of Amazon Nova. Speaks Chat Completions: `tools` function schema,
    `tool_calls` with a JSON-STRING `arguments`, role:"tool" result messages,
    finish_reason == "tool_calls". Uses the classic AzureOpenAI client on the
    COGNITIVE_SCOPE audience (mirrors AnthropicFoundry: azure_ad_token_provider,
    auto-refresh) so the whole scope/auth story stays consistent with Claude."""

    name = PROVIDER_AZURE_OPENAI

    def preference(self) -> list[str]:
        return list(OPENAI_MODEL_PREFERENCE)

    def make_client(self, cfg):
        """Classic AzureOpenAI against the Foundry account ROOT (the Bicep
        OPENAI_BASE_URL output; the client appends its own data-plane route).
        The bearer provider refreshes per request -- locally your az login,
        hosted the per-agent Entra identity -- exactly like AnthropicFoundry."""
        base = cfg.openai_base_url
        if not base:
            raise RuntimeError(
                f"{ENV_OPENAI_BASE_URL} is not set -- deploy a test stack: "
                "python3 -m chkpmcpaz deploy --model gpt-5-mini")
        from azure.identity import DefaultAzureCredential, get_bearer_token_provider
        from openai import AzureOpenAI
        return AzureOpenAI(
            azure_endpoint=base.rstrip("/"),
            azure_ad_token_provider=get_bearer_token_provider(
                DefaultAzureCredential(), COGNITIVE_SCOPE),
            api_version=OPENAI_API_VERSION,
        )

    def resolve_deployment(self, cfg, model, client) -> str:
        return model or cfg.openai_deployment or self.pick_model(client)

    def pick_model(self, client, preference: list[str] | None = None) -> str:
        """Probe each deployment with a tiny 8-token chat completion and return
        the first that answers. Total failure raises ModelUnavailable listing
        exactly what was tried (same semantics as the Anthropic probe)."""
        preference = self.preference() if preference is None else preference
        tried = []
        for deployment in preference:
            try:
                client.chat.completions.create(
                    model=deployment, max_completion_tokens=8,
                    messages=[{"role": "user", "content": "hi"}])
                return deployment
            except Exception as raw:  # noqa: BLE001 -- classify below, keep probing
                exc = self.first_api_error(raw)
                tried.append(f"{deployment} ({type(exc).__name__})")
        raise ModelUnavailable(
            "no Azure OpenAI deployment answered an 8-token probe -- tried: "
            + ", ".join(tried)
            + ". Are the deployments provisioned and does this identity hold "
            "'Cognitive Services OpenAI User' at account scope?  List the "
            "deployments you CAN call and their state with:  python3 -m "
            "chkpmcpaz models status  (re-provision them with:  python3 -m "
            "chkpmcpaz deploy)")

    def callable_deployments(self, client,
                             preference: list[str] | None = None) -> list[str]:
        """Which of the preferred deployments answer an 8-token probe RIGHT NOW.
        Never raises: a per-deployment failure just excludes that deployment."""
        preference = self.preference() if preference is None else preference
        ok: list[str] = []
        for deployment in preference:
            try:
                client.chat.completions.create(
                    model=deployment, max_completion_tokens=8,
                    messages=[{"role": "user", "content": "hi"}])
                ok.append(deployment)
            except Exception:  # noqa: BLE001 -- probing; a failure just excludes it
                pass
        return ok

    def format_tools(self, mcp_tools) -> tuple[list[dict], dict[str, str]]:
        """MCP catalog -> (OpenAI function-tool list, {api_name: namespaced}).
        Same name sanitize/dedupe/name_map as Anthropic; the schema shape is
        {"type":"function","function":{name,description,parameters}}. NO
        cache_control marker -- OpenAI prompt-caches automatically."""
        api_names = dedupe_names([sanitize_tool_name(t.namespaced) for t in mcp_tools])
        tools: list[dict] = []
        name_map: dict[str, str] = {}
        for tool, api_name in zip(mcp_tools, api_names):
            name_map[api_name] = tool.namespaced
            desc = (tool.description or tool.namespaced).strip() or tool.namespaced
            tools.append({
                "type": "function",
                "function": {
                    "name": api_name,
                    "description": desc[:TOOL_DESCRIPTION_MAX_CHARS],
                    "parameters": tool.input_schema or {"type": "object", "properties": {}},
                },
            })
        # OpenAI rejects a tools array longer than OPENAI_MAX_TOOLS (400
        # array_above_max_length). The default @chkp catalog is ~141 tools, so
        # keep the first OPENAI_MAX_TOOLS and say what was dropped. (Claude has
        # no such cap -- AnthropicProvider.format_tools does not truncate.)
        if len(tools) > OPENAI_MAX_TOOLS:
            dropped = len(tools) - OPENAI_MAX_TOOLS
            log(f"  ⚠ {len(tools)} MCP tools exceed OpenAI's {OPENAI_MAX_TOOLS}-tool "
                f"limit -- exposing the first {OPENAI_MAX_TOOLS}, dropping {dropped} "
                "(narrow --servers, or use Claude which has no cap)")
            tools = tools[:OPENAI_MAX_TOOLS]
        return tools, name_map

    def build_conversation(self, system_prompt: str,
                           task: str) -> tuple[Any, list[dict]]:
        """OpenAI has no separate system param, so the system prompt is the first
        message; there is no cache_control marker (caching is automatic)."""
        return None, [{"role": "system", "content": system_prompt},
                      {"role": "user", "content": task}]

    def stream_turn(self, client, deployment, system, messages, tools,
                    on_text) -> TurnResult:
        """ONE streamed Chat Completions turn. Tool calls arrive as deltas keyed
        by .index; the `arguments` field is a STRING assembled from fragments,
        json.loads'd into ToolCall.input (gpt can emit args that 'might not be
        valid JSON' -- fall back to {}). Usage rides the final chunk."""
        stream = client.chat.completions.create(
            model=deployment, messages=messages, tools=tools, tool_choice="auto",
            parallel_tool_calls=True, max_completion_tokens=MAX_TOKENS,
            stream=True, stream_options={"include_usage": True})
        text: list[str] = []
        acc: dict = {}
        finish = None
        raw_usage = None
        for chunk in stream:
            if getattr(chunk, "usage", None):
                raw_usage = chunk.usage
            if not chunk.choices:
                continue
            ch = chunk.choices[0]
            d = ch.delta
            if getattr(d, "content", None):
                text.append(d.content)
                if on_text:
                    on_text(d.content)
            for tcd in (getattr(d, "tool_calls", None) or []):
                slot = acc.setdefault(tcd.index, {"id": None, "name": None, "args": ""})
                if tcd.id:
                    slot["id"] = tcd.id
                if tcd.function and tcd.function.name:
                    slot["name"] = tcd.function.name
                if tcd.function and tcd.function.arguments:
                    slot["args"] += tcd.function.arguments
            if ch.finish_reason:
                finish = ch.finish_reason
        tool_calls: list[ToolCall] = []
        oai_calls: list[dict] = []
        for i in sorted(acc):
            s = acc[i]
            try:
                args = json.loads(s["args"] or "{}")
            except Exception:  # noqa: BLE001 -- AOAI: args "might not be valid JSON"
                args = {}
            tool_calls.append(ToolCall(id=s["id"], name=s["name"], input=args))
            oai_calls.append({"id": s["id"], "type": "function",
                              "function": {"name": s["name"],
                                           "arguments": s["args"] or "{}"}})
        assistant_msg: dict = {"role": "assistant", "content": "".join(text) or None}
        if oai_calls:
            assistant_msg["tool_calls"] = oai_calls
        return TurnResult(
            text="".join(text),
            tool_calls=tool_calls,
            is_tool_use=(finish == "tool_calls" or bool(tool_calls)),
            usage=self.usage_of(raw_usage),
            assistant_message=assistant_msg,
        )

    def format_assistant_message(self, turn: TurnResult) -> dict:
        return turn.assistant_message

    def format_tool_results(self, tool_calls: list[ToolCall],
                            outcomes: list[dict], max_chars: int) -> list[dict]:
        """N messages -- one role:"tool" message PER call, keyed by tool_call_id.
        OpenAI has no is_error flag, so an errored tool is prefixed 'error: ' in
        the content (still truncated to max_chars)."""
        return [
            {"role": "tool", "tool_call_id": tc.id, "name": tc.name,
             "content": (("error: " if o["error"] else "") + o["text"])[:max_chars]}
            for tc, o in zip(tool_calls, outcomes)]

    def is_transient(self, exc: BaseException) -> bool:
        """Retryable? openai connection/timeout failures and 429/500/503/529."""
        if isinstance(exc, (openai.APIConnectionError, openai.APITimeoutError)):
            return True
        return getattr(exc, "status_code", None) in _TRANSIENT_STATUS

    def is_model_access_error(self, exc: BaseException) -> bool:
        """401/403/404 -- this identity cannot use the deployment."""
        return getattr(self.first_api_error(exc), "status_code", None) in _MODEL_ACCESS_STATUS

    def first_api_error(self, exc: BaseException) -> BaseException:
        return _find_api_error(exc, openai.APIError) or exc

    def usage_of(self, raw_usage) -> dict:
        """Map OpenAI usage into the four USAGE_FIELDS keys so telemetry_line is
        untouched: prompt_tokens->input, completion_tokens->output,
        prompt_tokens_details.cached_tokens->cache_read; no cache_creation
        concept on OpenAI. A None usage contributes zeros."""
        if raw_usage is None:
            return {k: 0 for k in USAGE_FIELDS}
        details = getattr(raw_usage, "prompt_tokens_details", None)
        return {
            "input_tokens": getattr(raw_usage, "prompt_tokens", 0) or 0,
            "output_tokens": getattr(raw_usage, "completion_tokens", 0) or 0,
            "cache_read_input_tokens": (getattr(details, "cached_tokens", 0) or 0) if details else 0,
            "cache_creation_input_tokens": 0,
        }


# =============================================================================
# Registry
# =============================================================================
_PROVIDERS: dict[str, Provider] = {
    PROVIDER_ANTHROPIC: AnthropicProvider(),
    PROVIDER_AZURE_OPENAI: AzureOpenAIProvider(),
}


def get_provider(name: str) -> Provider:
    """Return the singleton Provider for a name (config.PROVIDER_*). Unknown
    names raise ValueError -- callers pass a resolve_provider() result, so this
    only trips on a bug."""
    try:
        return _PROVIDERS[name]
    except KeyError:
        raise ValueError(
            f"unknown provider {name!r} -- known: {', '.join(_PROVIDERS)}") from None
