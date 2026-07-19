"""Pure-logic tests for the model-Provider abstraction (chkpmcpaz.providers) --
the two genuinely-different Azure model surfaces the ONE agent loop drives:

  * AnthropicProvider   -- Claude on Foundry (anthropic Messages API). This is
    the existing agent.py logic, moved verbatim; the assertions here mirror
    tests/test_agent.py so the refactor stays behavior-for-behavior identical.
  * AzureOpenAIProvider -- first-party Azure OpenAI gpt-5-mini (openai SDK,
    Chat Completions: `tools` function schema, `tool_calls`, role:"tool"
    messages, JSON-STRING arguments). The cheap test target, the Azure analogue
    of Amazon Nova on AWS Bedrock.

No Azure calls, no network: the anthropic/openai SDK exceptions are built
in-line against fake httpx responses (same trick as tests/test_agent.py), and
the streamed turns run against hand-rolled fake clients that emit the exact
delta shapes each wire format uses.

STATUS NOTE (parallel build): the RUNTIME builder fills the provider method
bodies in chkpmcpaz/providers.py (they raise NotImplementedError in the
architect skeleton). The frozen surface these tests also cover -- the ToolCall/
TurnResult dataclasses, sanitize_tool_name/dedupe_names, get_provider(), the
.name/.preference() attributes and ModelUnavailable -- passes today; the method
bodies pass once RUNTIME lands. Assertions encode the FROZEN CONTRACT (section 4)
and are not weakened to match an unfinished body.
"""

import json
import re
from types import SimpleNamespace

import anthropic
import httpx
import openai
import pytest

import chkpmcpaz.agent as agent
from chkpmcpaz import config
from chkpmcpaz.config import (
    MAX_TOKENS,
    TOOL_DESCRIPTION_MAX_CHARS,
    TOOL_RESULT_MAX_CHARS,
    StackConfig,
)
from chkpmcpaz.providers import (
    AnthropicProvider,
    AzureOpenAIProvider,
    ModelUnavailable,
    Provider,
    ToolCall,
    TurnResult,
    dedupe_names,
    get_provider,
    sanitize_tool_name,
)

# =============================================================================
# Fixtures / factories
# =============================================================================

_REQ = httpx.Request("POST", "https://unit.test/v1/messages")

_ANTHROPIC_STATUS_CLS = {
    400: anthropic.BadRequestError,
    401: anthropic.AuthenticationError,
    404: anthropic.NotFoundError,
    429: anthropic.RateLimitError,
}


def _anthropic_status(status: int) -> anthropic.APIStatusError:
    cls = _ANTHROPIC_STATUS_CLS.get(status, anthropic.InternalServerError)
    return cls(f"http {status}", response=httpx.Response(status, request=_REQ), body=None)


def _openai_status(status: int) -> openai.APIStatusError:
    """An openai status error with `.status_code` set (constructed directly so
    non-mapped codes like 503/529 keep their exact status)."""
    return openai.APIStatusError(
        f"http {status}", response=httpx.Response(status, request=_REQ), body=None)


def _tool(namespaced, server="quantum-management", description="List hosts.",
          schema=None):
    """Duck-typed stand-in for mcp_stdio.NamespacedTool (attribute contract)."""
    return SimpleNamespace(
        namespaced=namespaced, server=server, description=description,
        input_schema=schema if schema is not None else
        {"type": "object", "properties": {"limit": {"type": "integer"}}})


# --- fake anthropic client (Messages API) -------------------------------------

class _FakeAnthropicStream:
    """Context manager matching client.messages.stream(...): streams text then
    yields a final message."""

    def __init__(self, final, texts):
        self._final, self._texts = final, texts

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    @property
    def text_stream(self):
        return iter(self._texts)

    def get_final_message(self):
        return self._final


class _FakeAnthropicClient:
    """Stub AnthropicFoundry. `stream(...)` replays `final`; `create(...)` is the
    8-token probe -- only deployments in `allowed` answer, the rest 404."""

    def __init__(self, final=None, texts=("thinking ",), allowed=()):
        self._final, self._texts = final, texts
        self.allowed = set(allowed)
        self.calls: list[str] = []
        self.last_kwargs: dict = {}
        outer = self

        class _Messages:
            def stream(self, *, model, **kw):
                outer.calls.append(model)
                outer.last_kwargs = kw
                return _FakeAnthropicStream(outer._final, outer._texts)

            def create(self, *, model, **kw):
                outer.calls.append(model)
                outer.last_kwargs = kw
                if model not in outer.allowed:
                    raise _anthropic_status(404)
                return SimpleNamespace(stop_reason="end_turn")

        self.messages = _Messages()


def _anthropic_final(text_blocks=("thinking ",), tool_uses=(),
                     stop_reason="tool_use", usage=None):
    content = [SimpleNamespace(type="text", text=t) for t in text_blocks]
    for tu in tool_uses:
        content.append(SimpleNamespace(type="tool_use", id=tu["id"],
                                       name=tu["name"], input=tu["input"]))
    if usage is None:
        usage = SimpleNamespace(input_tokens=10, output_tokens=3,
                                cache_read_input_tokens=2,
                                cache_creation_input_tokens=0)
    return SimpleNamespace(content=content, stop_reason=stop_reason, usage=usage)


# --- fake openai client (Chat Completions) ------------------------------------

def _chunk(content=None, tool_calls=None, finish_reason=None, usage=None):
    delta = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(usage=usage,
                           choices=[SimpleNamespace(delta=delta,
                                                    finish_reason=finish_reason)])


def _usage_chunk(usage):
    """A usage-only terminal chunk (stream_options include_usage=True): choices
    is empty, usage is populated."""
    return SimpleNamespace(usage=usage, choices=[])


def _tcd(index, id=None, name=None, arguments=None):
    """One streamed tool_call delta fragment."""
    return SimpleNamespace(index=index, id=id,
                           function=SimpleNamespace(name=name, arguments=arguments))


def _oai_usage(prompt=20, completion=7, cached=4):
    return SimpleNamespace(
        prompt_tokens=prompt, completion_tokens=completion,
        prompt_tokens_details=SimpleNamespace(cached_tokens=cached))


class _FakeOpenAIClient:
    """Stub AzureOpenAI/OpenAI client. `chat.completions.create(stream=True)`
    replays `chunks`; the non-stream form is the 8-token probe (models in
    `allowed` answer, the rest 404)."""

    def __init__(self, chunks=(), allowed=()):
        self._chunks = list(chunks)
        self.allowed = set(allowed)
        self.calls: list[str] = []
        self.last_kwargs: dict = {}
        outer = self

        class _Completions:
            def create(self, *, model, **kw):
                outer.calls.append(model)
                outer.last_kwargs = kw
                if kw.get("stream"):
                    return iter(outer._chunks)
                if model not in outer.allowed:
                    raise _openai_status(404)
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])

        self.chat = SimpleNamespace(completions=_Completions())


# =============================================================================
# Shared, provider-agnostic surface (frozen; passes today)
# =============================================================================

def test_tool_name_helpers_are_the_shared_implementation():
    # agent.py re-exports these; they must be the SAME behavior (one impl).
    assert sanitize_tool_name is agent.sanitize_tool_name or \
        sanitize_tool_name("x") == agent.sanitize_tool_name("x")
    assert sanitize_tool_name("quantummanagement___show_hosts") == \
        "quantummanagement___show_hosts"
    assert re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", sanitize_tool_name("weird name!/v2"))
    assert len(sanitize_tool_name("x" * 80)) == 64
    assert sanitize_tool_name("") == "tool"     # never empty


def test_dedupe_names_suffixes_collisions_in_order():
    out = dedupe_names(["dup", "dup", "other", "dup"])
    assert len(out) == 4 and len(set(out)) == 4
    assert out[0] == "dup" and out[2] == "other" and out[1] == "dup_1"


def test_toolcall_and_turnresult_dataclass_shapes():
    tc = ToolCall(id="tu_1", name="quantummanagement___show_hosts", input={"limit": 5})
    assert (tc.id, tc.name, tc.input) == ("tu_1", "quantummanagement___show_hosts", {"limit": 5})
    tr = TurnResult(text="hi", tool_calls=[tc], is_tool_use=True)
    assert tr.usage == {} and tr.assistant_message is None    # defaulted
    assert tr.is_tool_use is True and tr.tool_calls[0] is tc


def test_model_unavailable_is_runtimeerror():
    assert issubclass(ModelUnavailable, RuntimeError)
    # agent.py re-exports the SAME class for backward compatibility
    assert agent.ModelUnavailable is ModelUnavailable


def test_get_provider_returns_the_right_singletons():
    anth = get_provider(config.PROVIDER_ANTHROPIC)
    oai = get_provider(config.PROVIDER_AZURE_OPENAI)
    assert isinstance(anth, AnthropicProvider) and anth.name == "anthropic"
    assert isinstance(oai, AzureOpenAIProvider) and oai.name == "azure-openai"
    # singletons: same object each call (registry keyed by PROVIDER_*)
    assert get_provider(config.PROVIDER_ANTHROPIC) is anth
    assert get_provider(config.PROVIDER_AZURE_OPENAI) is oai


def test_get_provider_unknown_raises_valueerror():
    with pytest.raises(ValueError):
        get_provider("azure-oai")


def test_preference_methods_match_config():
    assert AnthropicProvider().preference() == config.MODEL_PREFERENCE
    assert AzureOpenAIProvider().preference() == config.OPENAI_MODEL_PREFERENCE
    # a fresh list, not the shared constant (mutation-safe)
    p = AzureOpenAIProvider().preference()
    p.append("x")
    assert config.OPENAI_MODEL_PREFERENCE == ["gpt-5-mini"]


# =============================================================================
# AnthropicProvider (RUNTIME fills the bodies; assertions mirror test_agent.py)
# =============================================================================

class TestAnthropicProvider:
    P = AnthropicProvider()

    def test_format_tools_shape_and_reverse_map(self):
        tools, name_map = self.P.format_tools([
            _tool("quantummanagement___show_hosts", "quantum-management"),
            _tool("documentation___search_docs", "documentation"),
        ])
        assert len(tools) == 2
        for t in tools:
            assert set(t) >= {"name", "description", "input_schema"}
            assert name_map[t["name"]] in ("quantummanagement___show_hosts",
                                           "documentation___search_docs")

    def test_format_tools_cache_control_on_last_tool_only(self):
        tools, _ = self.P.format_tools([
            _tool("a___one", "a"), _tool("b___two", "b"), _tool("c___three", "c")])
        assert tools[-1].get("cache_control") == {"type": "ephemeral"}
        for t in tools[:-1]:
            assert "cache_control" not in t

    def test_format_tools_falls_back_to_object_schema(self):
        tools, _ = self.P.format_tools([_tool("a___one", "a", schema={})])
        assert tools[0]["input_schema"] == {"type": "object", "properties": {}}

    def test_build_conversation_system_is_cache_controlled(self):
        system, messages = self.P.build_conversation(config.SYSTEM_PROMPT, "how many hosts?")
        assert system == [{"type": "text", "text": config.SYSTEM_PROMPT,
                           "cache_control": {"type": "ephemeral"}}]
        assert messages == [{"role": "user", "content": "how many hosts?"}]

    def test_stream_turn_builds_normalized_turnresult(self):
        final = _anthropic_final(
            text_blocks=("thinking ",),
            tool_uses=[{"id": "tu_1", "name": "quantummanagement___show_hosts",
                        "input": {"limit": 5}}],
            stop_reason="tool_use")
        client = _FakeAnthropicClient(final=final, texts=("thinking ",))
        seen = []
        turn = self.P.stream_turn(client, "claude-sonnet-4-6", [{"type": "text"}],
                                  [{"role": "user", "content": "hi"}], [], seen.append)
        assert client.last_kwargs.get("max_tokens") == MAX_TOKENS
        assert seen == ["thinking "]                   # text streamed to on_text
        assert turn.text == "thinking "
        assert turn.is_tool_use is True                # stop_reason == tool_use
        assert len(turn.tool_calls) == 1
        tc = turn.tool_calls[0]
        assert (tc.id, tc.name) == ("tu_1", "quantummanagement___show_hosts")
        assert tc.input == {"limit": 5} and isinstance(tc.input, dict)
        # usage normalized to the four USAGE_FIELDS keys
        assert turn.usage == {"input_tokens": 10, "output_tokens": 3,
                              "cache_read_input_tokens": 2,
                              "cache_creation_input_tokens": 0}
        # assistant_message carries the native content blocks verbatim
        assert turn.assistant_message == {"role": "assistant", "content": final.content}
        assert self.P.format_assistant_message(turn) is turn.assistant_message

    def test_stream_turn_end_turn_is_not_tool_use(self):
        final = _anthropic_final(text_blocks=("14 hosts.",), tool_uses=(),
                                 stop_reason="end_turn")
        turn = self.P.stream_turn(_FakeAnthropicClient(final=final, texts=("14 hosts.",)),
                                  "claude-sonnet-4-6", [], [], [], None)
        assert turn.is_tool_use is False and turn.text == "14 hosts."

    def test_format_tool_results_single_user_message_with_blocks(self):
        tcs = [ToolCall(id="tu_1", name="a", input={}),
               ToolCall(id="tu_2", name="b", input={})]
        outcomes = [{"text": "X" * 9000, "error": False},
                    {"text": "boom", "error": True}]
        msgs = self.P.format_tool_results(tcs, outcomes, TOOL_RESULT_MAX_CHARS)
        assert len(msgs) == 1 and msgs[0]["role"] == "user"     # ONE message
        blocks = msgs[0]["content"]
        assert len(blocks) == 2
        assert blocks[0]["type"] == "tool_result"
        assert blocks[0]["tool_use_id"] == "tu_1"               # keyed by tool_use_id
        assert len(blocks[0]["content"]) == TOOL_RESULT_MAX_CHARS   # truncated
        assert blocks[0]["is_error"] is False
        assert blocks[1]["tool_use_id"] == "tu_2" and blocks[1]["is_error"] is True

    def test_usage_of_maps_anthropic_names(self):
        u = SimpleNamespace(input_tokens=11, output_tokens=4,
                            cache_read_input_tokens=3, cache_creation_input_tokens=1)
        assert self.P.usage_of(u) == {"input_tokens": 11, "output_tokens": 4,
                                      "cache_read_input_tokens": 3,
                                      "cache_creation_input_tokens": 1}

    @pytest.mark.parametrize("status,transient", [
        (429, True), (500, True), (503, True), (529, True),
        (400, False), (401, False), (404, False),
    ])
    def test_is_transient_status_matrix(self, status, transient):
        assert self.P.is_transient(_anthropic_status(status)) is transient

    def test_is_transient_connection_and_timeout(self):
        assert self.P.is_transient(anthropic.APIConnectionError(request=_REQ)) is True
        assert self.P.is_transient(anthropic.APITimeoutError(request=_REQ)) is True
        assert self.P.is_transient(ValueError("boom")) is False

    @pytest.mark.parametrize("status,access", [
        (401, True), (403, True), (404, True),
        (429, False), (500, False),
    ])
    def test_is_model_access_error_matrix(self, status, access):
        assert self.P.is_model_access_error(_anthropic_status(status)) is access

    def test_first_api_error_unwraps_nested_groups(self):
        err = _anthropic_status(429)
        outer = BaseExceptionGroup("tg", [BaseExceptionGroup("tg", [err])])
        assert self.P.first_api_error(outer) is err
        assert self.P.first_api_error(err) is err               # bare passthrough

    def test_pick_model_preference_order_and_tiny_probe(self):
        c = _FakeAnthropicClient(allowed=config.MODEL_PREFERENCE)
        assert self.P.pick_model(c) == "claude-sonnet-4-6"
        assert c.calls == ["claude-sonnet-4-6"]                 # first success wins
        assert c.last_kwargs.get("max_tokens") == 8             # 8-token probe

    def test_pick_model_falls_back_down_the_list(self):
        c = _FakeAnthropicClient(allowed={"claude-haiku-4-5"})
        assert self.P.pick_model(c) == "claude-haiku-4-5"
        assert c.calls[0] == "claude-sonnet-4-6"

    def test_pick_model_total_failure_raises_modelunavailable(self):
        with pytest.raises(ModelUnavailable) as ei:
            self.P.pick_model(_FakeAnthropicClient(allowed=set()))
        assert "claude-sonnet-4-6" in str(ei.value)

    def test_callable_deployments_never_raises_and_filters(self):
        c = _FakeAnthropicClient(allowed={"claude-haiku-4-5"})
        assert self.P.callable_deployments(c) == ["claude-haiku-4-5"]
        assert self.P.callable_deployments(_FakeAnthropicClient(allowed=set())) == []

    def test_resolve_deployment_precedence(self):
        client = _FakeAnthropicClient(allowed=config.MODEL_PREFERENCE)
        cfg = StackConfig(claude_deployment="claude-haiku-4-5")
        # explicit model wins over configured deployment
        assert self.P.resolve_deployment(cfg, "claude-sonnet-4-6", client) == "claude-sonnet-4-6"
        # else the configured deployment
        assert self.P.resolve_deployment(cfg, None, client) == "claude-haiku-4-5"
        # else auto-select via the probe
        assert self.P.resolve_deployment(StackConfig(), None, client) == "claude-sonnet-4-6"


# =============================================================================
# AzureOpenAIProvider (RUNTIME fills the bodies; per AOAI-CONTRACT)
# =============================================================================

class TestAzureOpenAIProvider:
    P = AzureOpenAIProvider()

    # --- tool schema translation (OpenAI function-calling wire) ---------------

    def test_format_tools_openai_function_schema_no_cache_marker(self):
        tools, name_map = self.P.format_tools([
            _tool("quantummanagement___show_hosts", "quantum-management"),
            _tool("documentation___search_docs", "documentation"),
        ])
        assert len(tools) == 2
        for t in tools:
            assert t["type"] == "function"                      # OpenAI shape
            fn = t["function"]
            assert set(fn) >= {"name", "description", "parameters"}
            assert "cache_control" not in t and "cache_control" not in fn
            assert name_map[fn["name"]] in ("quantummanagement___show_hosts",
                                            "documentation___search_docs")
        # NO cache_control anywhere (OpenAI caches automatically)
        assert not any("cache_control" in t for t in tools)

    def test_format_tools_parameters_fallback_and_description_cap(self):
        tools, _ = self.P.format_tools([
            _tool("a___one", "a", description="D" * 5000, schema={})])
        fn = tools[0]["function"]
        assert fn["parameters"] == {"type": "object", "properties": {}}
        assert len(fn["description"]) == TOOL_DESCRIPTION_MAX_CHARS   # 1000-char cap

    def test_format_tools_sanitizes_and_dedupes_names(self):
        tools, name_map = self.P.format_tools([
            _tool("weird name!/v2", "a"), _tool("weird name!/v2", "b")])
        names = [t["function"]["name"] for t in tools]
        assert all(re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", n) for n in names)
        assert len(set(names)) == 2                             # deduped

    def test_format_tools_caps_at_openai_limit(self):
        # OpenAI rejects > 128 tools (the 9 default @chkp servers expose ~141);
        # the azure-openai provider caps the array. name_map may keep every
        # entry -- only the emitted `tools` array is what OpenAI validates.
        many = [_tool(f"srv___tool_{i}", "srv") for i in range(config.OPENAI_MAX_TOOLS + 13)]
        tools, name_map = self.P.format_tools(many)
        assert len(tools) == config.OPENAI_MAX_TOOLS
        # Claude has no such cap: the Anthropic provider must NOT truncate.
        atools, _ = AnthropicProvider().format_tools(many)
        assert len(atools) == config.OPENAI_MAX_TOOLS + 13

    def test_build_conversation_uses_a_system_role_message(self):
        system, messages = self.P.build_conversation(config.SYSTEM_PROMPT, "how many hosts?")
        assert system is None                                   # no separate system param
        assert messages == [{"role": "system", "content": config.SYSTEM_PROMPT},
                            {"role": "user", "content": "how many hosts?"}]

    # --- streamed tool-call loop (delta accumulation) -------------------------

    def test_stream_turn_accumulates_split_args_and_parallel_calls(self):
        chunks = [
            _chunk(content="Let me check. "),
            _chunk(tool_calls=[_tcd(0, id="call_a",
                                    name="quantummanagement___show_hosts",
                                    arguments='{"li')]),
            _chunk(tool_calls=[_tcd(0, arguments='mit": 5}')]),      # same index, concat
            _chunk(tool_calls=[_tcd(1, id="call_b",
                                    name="documentation___search",
                                    arguments='{"q": "vpn"}')]),
            _chunk(finish_reason="tool_calls"),
            _usage_chunk(_oai_usage()),
        ]
        client = _FakeOpenAIClient(chunks=chunks)
        seen = []
        turn = self.P.stream_turn(client, "gpt-5-mini", None,
                                  [{"role": "user", "content": "hi"}], [], seen.append)
        # the request asked for tools + parallel + streamed usage
        kw = client.last_kwargs
        assert kw.get("stream") is True and kw.get("tool_choice") == "auto"
        assert kw.get("parallel_tool_calls") is True
        assert kw.get("max_completion_tokens") == MAX_TOKENS
        assert kw.get("stream_options") == {"include_usage": True}
        # streamed text
        assert seen == ["Let me check. "] and turn.text == "Let me check. "
        assert turn.is_tool_use is True                         # finish_reason tool_calls
        # two parallel ToolCalls, split JSON args reassembled + json.loads'd
        assert [(t.id, t.name, t.input) for t in turn.tool_calls] == [
            ("call_a", "quantummanagement___show_hosts", {"limit": 5}),
            ("call_b", "documentation___search", {"q": "vpn"}),
        ]
        assert all(isinstance(t.input, dict) for t in turn.tool_calls)
        # usage mapped from prompt/completion/cached tokens
        assert turn.usage == {"input_tokens": 20, "output_tokens": 7,
                              "cache_read_input_tokens": 4,
                              "cache_creation_input_tokens": 0}

    def test_stream_turn_assistant_message_carries_json_string_arguments(self):
        chunks = [
            _chunk(content="ok "),
            _chunk(tool_calls=[_tcd(0, id="call_a", name="t", arguments='{"a": 1}')]),
            _chunk(finish_reason="tool_calls"),
        ]
        turn = self.P.stream_turn(_FakeOpenAIClient(chunks=chunks),
                                  "gpt-5-mini", None, [], [], None)
        am = turn.assistant_message
        assert am["role"] == "assistant" and am["content"] == "ok "
        (call,) = am["tool_calls"]
        assert call["id"] == "call_a" and call["type"] == "function"
        # arguments travel as a JSON STRING on the wire (not a parsed dict)
        assert call["function"] == {"name": "t", "arguments": '{"a": 1}'}
        assert isinstance(call["function"]["arguments"], str)
        assert self.P.format_assistant_message(turn) is am

    def test_stream_turn_text_only_has_no_tool_calls_key(self):
        chunks = [_chunk(content="14 hosts."), _chunk(finish_reason="stop"),
                  _usage_chunk(_oai_usage())]
        turn = self.P.stream_turn(_FakeOpenAIClient(chunks=chunks),
                                  "gpt-5-mini", None, [], [], None)
        assert turn.is_tool_use is False and turn.text == "14 hosts."
        assert "tool_calls" not in turn.assistant_message
        assert turn.assistant_message["content"] == "14 hosts."

    def test_stream_turn_tolerates_invalid_json_arguments(self):
        # AOAI docs warn arguments "might not always be valid JSON"
        chunks = [_chunk(tool_calls=[_tcd(0, id="c1", name="t", arguments="{not json")]),
                  _chunk(finish_reason="tool_calls")]
        turn = self.P.stream_turn(_FakeOpenAIClient(chunks=chunks),
                                  "gpt-5-mini", None, [], [], None)
        assert turn.tool_calls[0].input == {}                   # graceful empty dict
        # the raw (unparsed) string is still echoed back to the model verbatim
        assert turn.assistant_message["tool_calls"][0]["function"]["arguments"] == "{not json"

    # --- tool-result messages (role:"tool", one per call) ---------------------

    def test_format_tool_results_are_n_tool_role_messages(self):
        tcs = [ToolCall(id="call_a", name="show_hosts", input={}),
               ToolCall(id="call_b", name="search", input={})]
        outcomes = [{"text": "14 hosts", "error": False},
                    {"text": "boom", "error": True}]
        msgs = self.P.format_tool_results(tcs, outcomes, TOOL_RESULT_MAX_CHARS)
        assert len(msgs) == 2                                   # ONE message PER call
        assert msgs[0] == {"role": "tool", "tool_call_id": "call_a",
                           "name": "show_hosts", "content": "14 hosts"}
        # errors get an "error: " prefix in the content
        assert msgs[1]["role"] == "tool" and msgs[1]["tool_call_id"] == "call_b"
        assert msgs[1]["content"] == "error: boom"

    def test_format_tool_results_truncates_to_max_chars(self):
        tcs = [ToolCall(id="c", name="t", input={})]
        outcomes = [{"text": "Y" * 9000, "error": False}]
        msgs = self.P.format_tool_results(tcs, outcomes, TOOL_RESULT_MAX_CHARS)
        assert len(msgs[0]["content"]) == TOOL_RESULT_MAX_CHARS
        # error prefix is counted inside the cap, not appended past it
        err = self.P.format_tool_results(
            [ToolCall(id="c", name="t", input={})],
            [{"text": "Z" * 9000, "error": True}], TOOL_RESULT_MAX_CHARS)
        assert len(err[0]["content"]) == TOOL_RESULT_MAX_CHARS
        assert err[0]["content"].startswith("error: ")

    # --- usage normalization + telemetry round-trip ---------------------------

    def test_usage_of_maps_openai_names(self):
        assert self.P.usage_of(_oai_usage(prompt=20, completion=7, cached=4)) == {
            "input_tokens": 20, "output_tokens": 7,
            "cache_read_input_tokens": 4, "cache_creation_input_tokens": 0}

    def test_usage_of_guards_none_and_missing_details(self):
        assert self.P.usage_of(None) == {"input_tokens": 0, "output_tokens": 0,
                                         "cache_read_input_tokens": 0,
                                         "cache_creation_input_tokens": 0}
        no_details = SimpleNamespace(prompt_tokens=5, completion_tokens=2,
                                     prompt_tokens_details=None)
        assert self.P.usage_of(no_details)["cache_read_input_tokens"] == 0

    def test_usage_of_feeds_the_unchanged_telemetry_line(self):
        # telemetry_line must read an OpenAI turn's usage identically to Claude's
        line = agent.telemetry_line(self.P.usage_of(_oai_usage(prompt=20, completion=7, cached=4)))
        assert line == "tokens  20 in · 7 out · 4 cache-read · 17% of input from cache"

    # --- error classification (openai SDK) ------------------------------------

    @pytest.mark.parametrize("status,transient", [
        (429, True), (500, True), (503, True), (529, True),
        (400, False), (401, False), (404, False),
    ])
    def test_is_transient_status_matrix(self, status, transient):
        assert self.P.is_transient(_openai_status(status)) is transient

    def test_is_transient_connection_and_timeout(self):
        assert self.P.is_transient(openai.APIConnectionError(request=_REQ)) is True
        assert self.P.is_transient(openai.APITimeoutError(request=_REQ)) is True
        assert self.P.is_transient(ValueError("boom")) is False

    @pytest.mark.parametrize("status,access", [
        (401, True), (403, True), (404, True),
        (429, False), (500, False),
    ])
    def test_is_model_access_error_matrix(self, status, access):
        assert self.P.is_model_access_error(_openai_status(status)) is access

    def test_first_api_error_unwraps_nested_groups(self):
        err = _openai_status(429)
        outer = BaseExceptionGroup("tg", [BaseExceptionGroup("tg", [err])])
        assert self.P.first_api_error(outer) is err
        assert self.P.first_api_error(err) is err

    # --- model auto-selection (8-token probe on chat.completions) -------------

    def test_pick_model_probes_with_tiny_message(self):
        c = _FakeOpenAIClient(allowed=config.OPENAI_MODEL_PREFERENCE)
        assert self.P.pick_model(c) == "gpt-5-mini"
        assert c.calls == ["gpt-5-mini"]
        assert c.last_kwargs.get("max_completion_tokens") == 8  # gpt-5 reasoning models require this, not max_tokens
        assert c.last_kwargs.get("stream") in (None, False)     # probe is non-stream

    def test_pick_model_total_failure_raises_modelunavailable(self):
        with pytest.raises(ModelUnavailable):
            self.P.pick_model(_FakeOpenAIClient(allowed=set()))

    def test_callable_deployments_never_raises(self):
        assert self.P.callable_deployments(
            _FakeOpenAIClient(allowed=config.OPENAI_MODEL_PREFERENCE)) == ["gpt-5-mini"]
        assert self.P.callable_deployments(_FakeOpenAIClient(allowed=set())) == []

    def test_resolve_deployment_precedence(self):
        client = _FakeOpenAIClient(allowed=config.OPENAI_MODEL_PREFERENCE)
        cfg = StackConfig(provider=config.PROVIDER_AZURE_OPENAI,
                          openai_deployment="gpt-5-mini")
        assert self.P.resolve_deployment(cfg, "gpt-4o", client) == "gpt-4o"      # explicit wins
        assert self.P.resolve_deployment(cfg, None, client) == "gpt-5-mini"     # configured
        auto = StackConfig(provider=config.PROVIDER_AZURE_OPENAI)
        assert self.P.resolve_deployment(auto, None, client) == "gpt-5-mini"    # auto-select


# =============================================================================
# Registry ties to config providers. The name/singleton half passes today; the
# runtime-checkable Provider isinstance() passes once RUNTIME lands every method.
# =============================================================================

def test_registry_covers_every_config_provider():
    for name in config.PROVIDERS:
        prov = get_provider(name)
        assert prov.name == name
        assert isinstance(prov, Provider)      # runtime-checkable protocol
