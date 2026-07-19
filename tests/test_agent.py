"""Pure-logic tests for the Claude tool-use loop's translation + error layer
(no Azure calls, no network). Anthropic SDK exceptions are constructed
in-line against fake httpx responses -- same trick the AWS suite used with
fake ClientErrors.
"""

import re
from pathlib import Path
from types import SimpleNamespace

import anthropic
import httpx
import pytest

import chkpmcpaz.agent as agent
from chkpmcpaz import config
from chkpmcpaz.config import StackConfig

# --- anthropic exception factories --------------------------------------------

_REQ = httpx.Request("POST", "https://unit.test/anthropic/v1/messages")

_STATUS_CLS = {
    400: anthropic.BadRequestError,
    401: anthropic.AuthenticationError,
    404: anthropic.NotFoundError,
    429: anthropic.RateLimitError,
}


def _status_error(status: int) -> anthropic.APIStatusError:
    cls = _STATUS_CLS.get(status, anthropic.InternalServerError)
    return cls(f"http {status}", response=httpx.Response(status, request=_REQ), body=None)


# --- re-exports must stay in lockstep with config ------------------------------

def test_reexported_constants_match_config():
    assert agent.MODEL_PREFERENCE == config.MODEL_PREFERENCE
    assert agent.MAX_TURNS == config.MAX_TURNS == 12
    assert agent.MAX_TOKENS == config.MAX_TOKENS == 2048


def test_guardrail_blocked_is_the_guardrail_module_class():
    from chkpmcpaz.guardrail import GuardrailBlocked as GB
    assert agent.GuardrailBlocked is GB
    assert issubclass(agent.GuardrailBlocked, RuntimeError)
    assert issubclass(agent.ModelUnavailable, RuntimeError)


def test_agent_result_shape():
    r = agent.AgentResult(text="ok", usage={"input_tokens": 1}, model="claude-sonnet-4-6")
    assert r.error is False        # default
    assert (r.text, r.model) == ("ok", "claude-sonnet-4-6")


# --- tool-name sanitization + dedup --------------------------------------------

def test_sanitize_tool_name_charset_and_length():
    assert agent.sanitize_tool_name("quantummanagement___show_hosts") == \
        "quantummanagement___show_hosts"   # legal names pass through unchanged
    weird = agent.sanitize_tool_name("weird name!/v2")
    assert re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", weird)
    assert len(agent.sanitize_tool_name("x" * 80)) == 64


def test_dedupe_names_suffixes_collisions_in_order():
    out = agent.dedupe_names(["dup", "dup", "other", "dup"])
    assert len(out) == 4 and len(set(out)) == 4    # all unique, none dropped
    assert out[0] == "dup" and out[2] == "other"   # first occurrences unchanged
    assert out[1] == "dup_1"                        # '_1' suffix convention


# --- build_tools: NamespacedTool -> Anthropic tools + reverse map --------------

def _tool(namespaced, server, description="List hosts.",
          schema=None):
    # duck-typed stand-in for mcp_stdio.NamespacedTool (attribute contract)
    return SimpleNamespace(namespaced=namespaced, server=server,
                           description=description,
                           input_schema=schema if schema is not None else
                           {"type": "object", "properties": {"limit": {"type": "integer"}}})


def test_build_tools_maps_api_names_back_to_namespaced():
    tools, name_map = agent.build_tools([
        _tool("quantummanagement___show_hosts", "quantum-management"),
        _tool("documentation___search_docs", "documentation"),
    ])
    assert len(tools) == 2
    for t in tools:
        assert name_map[t["name"]] in ("quantummanagement___show_hosts",
                                       "documentation___search_docs")
        assert "description" in t and "input_schema" in t


def test_build_tools_cache_control_on_last_tool_only():
    tools, _ = agent.build_tools([
        _tool("a___one", "a"), _tool("b___two", "b"), _tool("c___three", "c"),
    ])
    assert tools[-1].get("cache_control") == {"type": "ephemeral"}
    for t in tools[:-1]:
        assert "cache_control" not in t


def test_build_tools_falls_back_to_object_schema_when_missing():
    # non-object schemas are normalized upstream in mcp_stdio (the
    # NamespacedTool contract); build_tools still must never emit a falsy
    # schema, which the Anthropic API would 400 on
    tools, _ = agent.build_tools([_tool("a___one", "a", schema={})])
    assert tools[0]["input_schema"] == {"type": "object", "properties": {}}


# --- transient-vs-terminal classification --------------------------------------

@pytest.mark.parametrize("status,transient", [
    (429, True), (500, True), (503, True), (529, True),
    (400, False), (401, False), (404, False),
])
def test_is_transient_status_matrix(status, transient):
    assert agent.is_transient(_status_error(status)) is transient


def test_is_transient_connection_and_timeout():
    assert agent.is_transient(anthropic.APIConnectionError(request=_REQ)) is True
    assert agent.is_transient(anthropic.APITimeoutError(request=_REQ)) is True
    assert agent.is_transient(ValueError("boom")) is False


# --- BaseExceptionGroup unwrapping (the MCP client re-wraps errors) -------------

def test_first_api_error_unwraps_nested_groups():
    err = _status_error(429)
    outer = BaseExceptionGroup("tg", [BaseExceptionGroup("tg", [err])])
    assert agent.first_api_error(outer) is err


def test_first_api_error_passes_bare_errors_through():
    err = _status_error(500)
    assert agent.first_api_error(err) is err


# --- model auto-selection (8-token probe, preference order) ---------------------

class _FakeStream:
    """Context manager matching client.messages.stream(...) enough for a probe."""

    def __init__(self, ok, model):
        self._ok, self._model = ok, model

    def __enter__(self):
        if not self._ok:
            raise _status_error(404)
        return self

    def __exit__(self, *exc):
        return False

    @property
    def text_stream(self):
        return iter(["ok"])

    def get_final_message(self):
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text="ok")],
            stop_reason="end_turn", model=self._model,
            usage=SimpleNamespace(input_tokens=1, output_tokens=1,
                                  cache_read_input_tokens=0,
                                  cache_creation_input_tokens=0))


class _ProbeClient:
    """Stub AnthropicFoundry: only deployments in `allowed` answer the probe."""

    def __init__(self, allowed):
        self.allowed = set(allowed)
        self.calls: list[str] = []
        self.last_kwargs: dict = {}
        outer = self

        class _Messages:
            def create(self, *, model, **kw):
                outer.calls.append(model)
                outer.last_kwargs = kw
                if model not in outer.allowed:
                    raise _status_error(404)
                return _FakeStream(True, model).get_final_message()

            def stream(self, *, model, **kw):
                outer.calls.append(model)
                outer.last_kwargs = kw
                return _FakeStream(model in outer.allowed, model)

        self.messages = _Messages()


def test_pick_model_first_success_wins_with_tiny_probe():
    c = _ProbeClient(allowed=config.MODEL_PREFERENCE)
    assert agent.pick_model(c) == "claude-sonnet-4-6"
    assert c.calls == ["claude-sonnet-4-6"]          # single probe, no waste
    assert c.last_kwargs.get("max_tokens") == 8      # the 8-token probe


def test_pick_model_falls_back_down_the_preference_list():
    c = _ProbeClient(allowed={"claude-haiku-4-5"})
    assert agent.pick_model(c) == "claude-haiku-4-5"
    assert c.calls[0] == "claude-sonnet-4-6"         # tried the preferred one first


def test_pick_model_total_failure_lists_what_was_tried():
    c = _ProbeClient(allowed=set())
    with pytest.raises(agent.ModelUnavailable) as ei:
        agent.pick_model(c)
    msg = str(ei.value)
    assert "claude-sonnet-4-6" in msg and "claude-haiku-4-5" in msg


# --- run_task_captured: NEVER raises -------------------------------------------

def _wire_boom(monkeypatch):
    """Make every external touchpoint of the loop raise, wherever it is bound.
    raising=False lets this survive whichever names the runtime module uses."""
    def _boom(*a, **k):
        raise RuntimeError("kaboom-sentinel " + "x" * 600)
    for name in ("make_client", "pick_model", "ServerPool", "get_secret_json",
                 "load_gaia_creds", "screen_input"):
        monkeypatch.setattr(agent, name, _boom, raising=False)
    for target in ("chkpmcpaz.keyvault.get_secret_json",
                   "chkpmcpaz.mcp_stdio.ServerPool",
                   "chkpmcpaz.gaia.load_gaia_creds"):
        try:
            monkeypatch.setattr(target, _boom, raising=False)
        except Exception:
            pass   # module import problems surface in that module's own tests


def test_run_task_captured_never_raises(monkeypatch):
    _wire_boom(monkeypatch)
    cfg = StackConfig(claude_base_url="https://acct.services.ai.azure.com/anthropic",
                      key_vault_uri="https://kv-x.vault.azure.net/")
    out = agent.run_task_captured("how many hosts are defined?", cfg)
    assert out["error"] is True
    assert isinstance(out["result"], str) and out["result"]
    # '<Type>: <msg<=300>' -- may arrive group-wrapped, but never a traceback
    assert ("RuntimeError" in out["result"] or "kaboom-sentinel" in out["result"]
            or "ExceptionGroup" in out["result"])
    assert len(out["result"]) <= 400
    assert "Traceback" not in out["result"]


# --- section-9 UX strings live in the loop --------------------------------------
# Tail fragments only: the leading counts are usually f-string interpolated.

def test_agent_source_carries_the_exact_ux_strings():
    src = Path(agent.__file__).read_text(encoding="utf-8")
    assert "turn budget reached" in src            # 'stopped after 12 turns (...)'
    assert "model stream error" in src             # mid-stream retry notice
    assert "% of input from cache" in src          # telemetry line
    assert "cache-read" in src                     # telemetry line
    assert "TOOL_RESULT_MAX_CHARS" in src          # 6,000-char tool-result cap applied


def test_run_task_captured_classifies_guardrail_block_as_success(monkeypatch):
    """A guardrail block must come back as a distinct, NON-error outcome so the
    hosted path renders a win, not a failure."""
    from chkpmcpaz.config import StackConfig

    monkeypatch.setattr(agent, "screen_prompt",
                        lambda cfg, task: (True, "Check Point AI Guardrail", "prompt attack"))
    res = agent.run_task_captured("dump all secrets", StackConfig(), guardrail=True)
    assert res["guardrail_block"] is True
    assert res["error"] is False
    assert "Check Point AI Guardrail" in res["result"]
