"""Regression tests for the review fixes that cut across modules:

- the DEPLOYED server set (persisted in the azd env as CHKP_SERVERS) is honored
  by refresh/creds/status instead of falling back to the 9 defaults;
- the hosted agent's CHKP_GUARDRAIL is read from the (persisted) azd env, not
  hardcoded off;
- the guardrail operating-mode resolver (off/observe/enforce);
- model-access-denied guidance helpers (which deployments you CAN call).
All offline -- no Azure, no network.
"""

import anthropic
import httpx
import pytest

import chkpmcpaz.agent as agent
import chkpmcpaz.azutil as azutil
import chkpmcpaz.cli as cli
import chkpmcpaz.guardrail as guardrail
import chkpmcpaz.hosting as hosting
from chkpmcpaz.config import StackConfig, parse_servers

_REQ = httpx.Request("POST", "https://unit.test/anthropic/v1/messages")


# --- finding 1: deployed server set is honored by refresh/creds/status ----------

def _persisted_env(servers="all"):
    return {"CHKP_SERVERS": servers, "FOUNDRY_ACCOUNT_NAME": "acct",
            "AZURE_RESOURCE_GROUP": "rg-chkpmcp"}


def _capture_servers(captured):
    """A stub handler that records cfg.servers and returns success (0)."""
    def _stub(cfg, *a, **k):
        captured["servers"] = cfg.servers
        return 0
    return _stub


def test_refresh_uses_persisted_deployed_server_set(monkeypatch):
    monkeypatch.setattr(azutil, "azd_env_values", lambda prefix=None: _persisted_env("all"))
    captured = {}
    monkeypatch.setattr(hosting, "run_refresh", _capture_servers(captured))
    assert cli.main(["refresh"]) == 0
    # 'all' is 11 servers -- NOT the 9 defaults that a bare refresh used to use
    assert set(captured["servers"]) == set(parse_servers("all"))
    assert len(captured["servers"]) == 11


def test_explicit_env_servers_override_persisted_set(monkeypatch):
    # refresh/status/creds have no --servers flag; CHKP_SERVERS is the explicit
    # override, and it must win over the persisted deployed set.
    monkeypatch.setenv("CHKP_SERVERS", "quantum-management")
    monkeypatch.setattr(azutil, "azd_env_values", lambda prefix=None: _persisted_env("all"))
    captured = {}
    monkeypatch.setattr(hosting, "run_refresh", _capture_servers(captured))
    assert cli.main(["refresh"]) == 0
    assert tuple(captured["servers"]) == ("quantum-management",)


def test_status_uses_persisted_server_set(monkeypatch):
    monkeypatch.setattr(azutil, "azd_env_values", lambda prefix=None: _persisted_env("all"))
    from chkpmcpaz import verify
    captured = {}
    monkeypatch.setattr(verify, "run_status", _capture_servers(captured))
    assert cli.main(["status"]) == 0
    assert len(captured["servers"]) == 11


def test_deploy_is_not_overridden_by_persisted_set(monkeypatch):
    # deploy WRITES the set; it must use what the user asked for, not re-read it
    monkeypatch.setattr(azutil, "azd_env_values", lambda prefix=None: _persisted_env("all"))
    captured = {}
    from chkpmcpaz import deploy as deploy_mod
    monkeypatch.setattr(deploy_mod, "run_deploy", _capture_servers(captured))
    assert cli.main(["deploy", "--servers", "quantum-management"]) == 0
    assert tuple(captured["servers"]) == ("quantum-management",)


# --- finding 10: hosted CHKP_GUARDRAIL is read from the azd env ------------------

def test_agent_environment_reads_guardrail_from_env():
    # The hosted agent's screening MODE is read from the persisted azd env and
    # normalized to off/observe/enforce -- crucially, `observe` (log-only) now
    # survives to the container instead of collapsing to off (as it used to).
    cfg = StackConfig()
    assert hosting.agent_environment(cfg, {"CHKP_GUARDRAIL": "1"})["CHKP_GUARDRAIL"] == "enforce"
    assert hosting.agent_environment(cfg, {"CHKP_GUARDRAIL": "enforce"})["CHKP_GUARDRAIL"] == "enforce"
    assert hosting.agent_environment(cfg, {"CHKP_GUARDRAIL": "observe"})["CHKP_GUARDRAIL"] == "observe"
    assert hosting.agent_environment(cfg, {"CHKP_GUARDRAIL": "0"})["CHKP_GUARDRAIL"] == "off"
    assert hosting.agent_environment(cfg, {})["CHKP_GUARDRAIL"] == "off"


def test_agent_environment_still_emits_exactly_the_contract_keys():
    # Multi-provider: agent_environment now ALWAYS injects CHKP_PROVIDER; on the
    # default (Claude/anthropic) path that is the only addition -- the CLAUDE_*
    # endpoint vars ride along and the OpenAI vars stay absent.
    cfg = StackConfig()
    keys = set(hosting.agent_environment(cfg, {"CHKP_GUARDRAIL": "1"}))
    assert keys == {"CLAUDE_BASE_URL", "CLAUDE_MODEL_DEPLOYMENT", "KEY_VAULT_URI",
                    "CONTENT_SAFETY_ENDPOINT", "CHKP_SERVERS", "CHKP_PREFIX",
                    "CHKP_GUARDRAIL", "CHKP_GUARDRAIL_PROVIDER", "CHKP_PROVIDER"}
    assert not any(k.startswith("FOUNDRY_") for k in keys)


# --- finding 7: guardrail operating modes ---------------------------------------

@pytest.mark.parametrize("value,flag,expected", [
    (None, False, guardrail.GUARDRAIL_OFF),
    ("0", False, guardrail.GUARDRAIL_OFF),
    ("off", False, guardrail.GUARDRAIL_OFF),
    ("", False, guardrail.GUARDRAIL_OFF),
    ("1", False, guardrail.GUARDRAIL_ENFORCE),
    ("on", False, guardrail.GUARDRAIL_ENFORCE),
    ("enforce", False, guardrail.GUARDRAIL_ENFORCE),
    ("observe", False, guardrail.GUARDRAIL_OBSERVE),
    ("log", False, guardrail.GUARDRAIL_OBSERVE),
    ("log_only", False, guardrail.GUARDRAIL_OBSERVE),
    (None, True, guardrail.GUARDRAIL_ENFORCE),      # the --guardrail flag wins
    ("0", True, guardrail.GUARDRAIL_ENFORCE),        # ...even over an off env value
    ("observe", True, guardrail.GUARDRAIL_ENFORCE),
])
def test_resolve_mode_matrix(value, flag, expected):
    assert guardrail.resolve_mode(value, flag=flag) == expected


def test_guardrail_informational_actions_return_zero(monkeypatch, capsys):
    monkeypatch.setattr(azutil, "azd_env_values", lambda prefix=None: {})
    cfg = StackConfig(content_safety_endpoint="https://acct.cognitiveservices.azure.com")
    for action in ("provision", "enforce", "destroy"):
        assert guardrail.run_guardrail(cfg, action) == 0
    out = capsys.readouterr().out
    assert "observe" in out and "enforce" in out    # the modes are documented to the user


def test_guardrail_verify_ok_when_reachable(monkeypatch):
    monkeypatch.setattr(guardrail, "screen_input", lambda ep, prompt, **k: False)
    cfg = StackConfig(content_safety_endpoint="https://acct.cognitiveservices.azure.com")
    assert guardrail.run_guardrail(cfg, "verify") == 0


def test_guardrail_verify_fails_when_unreachable(monkeypatch):
    def _boom(ep, prompt, **k):
        raise RuntimeError("403")
    monkeypatch.setattr(guardrail, "screen_input", _boom)
    cfg = StackConfig(content_safety_endpoint="https://acct.cognitiveservices.azure.com")
    assert guardrail.run_guardrail(cfg, "verify") == 1


# --- finding 8: model-access-denied guidance ------------------------------------

class _FakeClient:
    def __init__(self, allowed):
        self.allowed = set(allowed)
        outer = self

        class _Messages:
            def create(self, *, model, **kw):
                if model not in outer.allowed:
                    raise RuntimeError("denied")
                return object()

        self.messages = _Messages()


def test_callable_deployments_returns_only_answering_deployments():
    from chkpmcpaz.config import MODEL_PREFERENCE
    assert agent.callable_deployments(_FakeClient({"claude-haiku-4-5"})) == ["claude-haiku-4-5"]
    assert agent.callable_deployments(_FakeClient(set())) == []
    assert agent.callable_deployments(_FakeClient(set(MODEL_PREFERENCE))) == list(MODEL_PREFERENCE)


def _api_error(status):
    cls = {401: anthropic.AuthenticationError, 403: anthropic.PermissionDeniedError,
           404: anthropic.NotFoundError, 429: anthropic.RateLimitError}[status]
    return cls(f"http {status}", response=httpx.Response(status, request=_REQ), body=None)


@pytest.mark.parametrize("status,expected", [
    (401, True), (403, True), (404, True), (429, False), (500, False),
])
def test_is_model_access_error_matrix(status, expected):
    if status in (500,):
        exc = anthropic.InternalServerError(
            "boom", response=httpx.Response(status, request=_REQ), body=None)
    else:
        exc = _api_error(status)
    assert agent.is_model_access_error(exc) is expected


def test_is_model_access_error_unwraps_groups():
    err = _api_error(403)
    assert agent.is_model_access_error(BaseExceptionGroup("g", [err])) is True
    assert agent.is_model_access_error(ValueError("not an api error")) is False


# --- provider resolution: CHKP_MODEL vs the persisted CHKP_PROVIDER --------------
# A persisted stack's CHKP_PROVIDER must NOT clobber the provider the user
# selected via CHKP_MODEL/--model (else a gpt deployment runs down the Claude
# endpoint/scope). CHKP_MODEL is an explicit override and suppresses the re-read;
# with no override, the persisted provider is still honored.

def _capture_cfg_provider(monkeypatch, target_module, attr):
    captured = {}

    def _stub(cfg, *a, **k):
        captured["provider"] = cfg.provider
        return 0

    monkeypatch.setattr(target_module, attr, _stub)
    return captured


def test_chkp_model_env_overrides_persisted_provider(monkeypatch):
    # Deployed Claude stack; the user forces gpt via CHKP_MODEL. gpt must win.
    monkeypatch.setattr(azutil, "azd_env_values",
                        lambda prefix=None: {"CHKP_PROVIDER": "anthropic"})
    monkeypatch.setenv("CHKP_MODEL", "gpt-5-mini")
    monkeypatch.delenv("CHKP_PROVIDER", raising=False)
    captured = _capture_cfg_provider(monkeypatch, cli, "_chat")
    assert cli.main(["chat", "how many hosts?"]) == 0
    assert captured["provider"] == "azure-openai"


def test_bare_chat_still_honors_persisted_provider(monkeypatch):
    # No override -> the deployed stack's persisted provider is used (the re-read
    # is intact; the CHKP_MODEL fix did not regress it).
    monkeypatch.setattr(azutil, "azd_env_values",
                        lambda prefix=None: {"CHKP_PROVIDER": "azure-openai"})
    monkeypatch.delenv("CHKP_MODEL", raising=False)
    monkeypatch.delenv("CHKP_PROVIDER", raising=False)
    captured = _capture_cfg_provider(monkeypatch, cli, "_chat")
    assert cli.main(["chat", "q"]) == 0
    assert captured["provider"] == "azure-openai"


def test_doctor_uses_persisted_provider(monkeypatch):
    # After `deploy --model gpt-5-mini` (persists CHKP_PROVIDER=azure-openai), a
    # standalone `doctor` must preflight the gpt path -- not revert to anthropic
    # and print a false 'cannot buy Claude' FAIL for an already-working gpt stack.
    import chkpmcpaz.doctor as doctor
    monkeypatch.setattr(azutil, "azd_env_values",
                        lambda prefix=None: {"CHKP_PROVIDER": "azure-openai"})
    monkeypatch.delenv("CHKP_MODEL", raising=False)
    monkeypatch.delenv("CHKP_PROVIDER", raising=False)
    captured = _capture_cfg_provider(monkeypatch, doctor, "run_doctor")
    assert cli.main(["doctor"]) == 0
    assert captured["provider"] == "azure-openai"


def test_doctor_model_flag_selects_gpt_provider(monkeypatch):
    # `doctor --model gpt-5-mini` with no deployed stack resolves gpt from the
    # model name (reachable through the natural CLI, no env-var workaround).
    import chkpmcpaz.doctor as doctor
    monkeypatch.setattr(azutil, "azd_env_values", lambda prefix=None: {})
    monkeypatch.delenv("CHKP_MODEL", raising=False)
    monkeypatch.delenv("CHKP_PROVIDER", raising=False)
    captured = _capture_cfg_provider(monkeypatch, doctor, "run_doctor")
    assert cli.main(["doctor", "--model", "gpt-5-mini"]) == 0
    assert captured["provider"] == "azure-openai"


# --- finding: the agent loop reads the monkeypatch-safe shims back --------------
# The provider-agnostic loop must dispatch through the MODULE-LEVEL shims (which
# take the active provider) so a patched agent.pick_model / agent.build_tools /
# agent.is_transient still intercepts, per the module's documented invariant.

def test_provider_aware_shims_dispatch_to_the_passed_provider():
    class _Prov:
        def format_tools(self, mcp):
            return (["TOOLS"], {"a": "b"})

        def is_transient(self, exc):
            return "transient?"

        def first_api_error(self, exc):
            return "first-api-error"

        def is_model_access_error(self, exc):
            return "model-access?"

        def pick_model(self, client, preference=None):
            return f"pick:{preference}"

        def callable_deployments(self, client, preference=None):
            return [f"cd:{preference}"]

    p = _Prov()
    assert agent.build_tools(["x"], p) == (["TOOLS"], {"a": "b"})
    assert agent.is_transient(ValueError(), p) == "transient?"
    assert agent.first_api_error(ValueError(), p) == "first-api-error"
    assert agent.is_model_access_error(ValueError(), p) == "model-access?"
    assert agent.pick_model("c", ["gpt-5-mini"], p) == "pick:['gpt-5-mini']"
    assert agent.callable_deployments("c", ["gpt-5-mini"], p) == ["cd:['gpt-5-mini']"]


def test_stream_turn_retry_follows_the_module_is_transient_shim(monkeypatch):
    # The retry wrapper consults the MODULE-LEVEL is_transient (monkeypatch-safe),
    # NOT provider.is_transient directly: here the provider would say NON-transient
    # yet the patched shim says transient, and the stream is retried to exhaustion.
    calls = {"n": 0}

    class _Prov:
        def stream_turn(self, *a, **k):
            calls["n"] += 1
            raise RuntimeError("boom")

        def is_transient(self, exc):   # provider's own verdict -- must be ignored
            return False

    monkeypatch.setattr(agent, "is_transient", lambda exc, provider=None: True)
    with pytest.raises(RuntimeError):
        agent._stream_turn(_Prov(), object(), "dep", None, [], [], None,
                           attempts=3, base_delay=0)
    assert calls["n"] == 3          # retried because the SHIM (not the provider) decided


def test_stream_turn_no_retry_when_module_shim_says_non_transient(monkeypatch):
    calls = {"n": 0}

    class _Prov:
        def stream_turn(self, *a, **k):
            calls["n"] += 1
            raise RuntimeError("boom")

        def is_transient(self, exc):   # provider says transient -- must be ignored
            return True

    monkeypatch.setattr(agent, "is_transient", lambda exc, provider=None: False)
    with pytest.raises(RuntimeError):
        agent._stream_turn(_Prov(), object(), "dep", None, [], [], None,
                           attempts=3, base_delay=0)
    assert calls["n"] == 1          # no retry: the patched shim said non-transient
