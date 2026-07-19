"""Hosted-runtime decision logic (AWS 8dd198f parity), all offline:

- `chat --runtime hosted` preflights the agent's existence and fails fast
  (exit 1, no build, no invoke) with a deploy hint;
- an `error: true` payload inside a successful invoke is an exit-1 failure,
  never a green done;
- a transport failure exits 1 pointing at the unaffected local runtime;
- `deploy_hosted_agent` passes EXACTLY the section-6.8 env vars to
  create_version (checked through a fake azure.ai.projects injected into
  sys.modules -- the extras are lazy-imported, so tests need no SDK).
"""

import sys
import types
from types import SimpleNamespace

import pytest

import chkpmcpaz.azutil as azutil
import chkpmcpaz.cli as cli
import chkpmcpaz.hosting as hosting
from chkpmcpaz.config import StackConfig

FAKE_ENV = {
    "AZURE_LOCATION": "eastus2",
    "AZURE_TENANT_ID": "00000000-0000-0000-0000-000000000000",
    "AZURE_RESOURCE_GROUP": "rg-chkpmcp",
    "FOUNDRY_ACCOUNT_NAME": "chkpmcp-foundry-tok",
    "FOUNDRY_PROJECT_NAME": "chkpmcp-project",
    "FOUNDRY_PROJECT_ENDPOINT":
        "https://chkpmcp-foundry-tok.services.ai.azure.com/api/projects/chkpmcp-project",
    "CLAUDE_BASE_URL": "https://chkpmcp-foundry-tok.services.ai.azure.com/anthropic",
    "CLAUDE_MODEL_DEPLOYMENT": "claude-sonnet-4-6",
    "CLAUDE_FALLBACK_DEPLOYMENT": "claude-haiku-4-5",
    "KEY_VAULT_NAME": "kv-chkpmcp-tok",
    "KEY_VAULT_URI": "https://kv-chkpmcp-tok.vault.azure.net/",
    "AZURE_CONTAINER_REGISTRY_NAME": "acrchkpmcptok",
    "AZURE_CONTAINER_REGISTRY_ENDPOINT": "acrchkpmcptok.azurecr.io",
    "CONTENT_SAFETY_ENDPOINT": "https://chkpmcp-foundry-tok.cognitiveservices.azure.com/",
    "APPLICATIONINSIGHTS_CONNECTION_STRING": "InstrumentationKey=00000000",
}

# Section 6.8 + CONTRACT section 6e: the exact env-var keys the hosted agent
# container receives on the Claude (anthropic) path. agent_environment ALWAYS
# injects CHKP_PROVIDER now (multi-provider), plus that provider's endpoint vars.
EXPECTED_AGENT_ENV_KEYS = {
    "CLAUDE_BASE_URL", "CLAUDE_MODEL_DEPLOYMENT", "KEY_VAULT_URI",
    "CONTENT_SAFETY_ENDPOINT", "CHKP_SERVERS", "CHKP_PREFIX", "CHKP_GUARDRAIL",
    "CHKP_GUARDRAIL_PROVIDER", "CHKP_PROVIDER",
}


def _patch_everywhere(monkeypatch, name, value):
    """Patch `name` on hosting/cli/azutil no matter which module bound it."""
    for mod in (hosting, cli, azutil):
        monkeypatch.setattr(mod, name, value, raising=False)


def _wire(monkeypatch, tmp_path, *, status, invoke):
    """Stub every Azure touchpoint of the hosted chat path; return call log."""
    monkeypatch.setenv("CHKP_UI", "plain")
    monkeypatch.setenv("CHKP_LOG_DIR", str(tmp_path))
    calls = {"invoke": 0, "deploy": 0, "status": 0}
    _patch_everywhere(monkeypatch, "azd_env_values", lambda prefix=None: dict(FAKE_ENV))
    _patch_everywhere(monkeypatch, "get_credential", lambda: object())

    def _status(cfg, env, *a, **k):
        calls["status"] += 1
        return status

    def _invoke(cfg, env, task, *a, **k):
        calls["invoke"] += 1
        if isinstance(invoke, BaseException):
            raise invoke
        return invoke

    def _deploy(cfg, env, *a, **k):
        calls["deploy"] += 1
        return "v1"

    _patch_everywhere(monkeypatch, "hosted_agent_status", _status)
    _patch_everywhere(monkeypatch, "invoke_hosted_agent", _invoke)
    _patch_everywhere(monkeypatch, "deploy_hosted_agent", _deploy)
    return calls


def _run_chat(capsys):
    try:
        rc = cli.main(["chat", "how many hosts are defined?", "--runtime", "hosted"])
    except SystemExit as e:
        rc = e.code or 0
    captured = capsys.readouterr()
    return rc, captured.out + captured.err


def test_hosted_chat_fails_fast_when_agent_absent(monkeypatch, tmp_path, capsys):
    calls = _wire(monkeypatch, tmp_path, status=None, invoke={"result": "", "error": False})
    rc, out = _run_chat(capsys)
    assert rc == 1
    assert calls["invoke"] == 0            # bailed before any invoke...
    assert calls["deploy"] == 0            # ...and never started a build
    assert "No hosted agent 'chkpmcp-agent' found" in out
    assert "python3 -m chkpmcpaz deploy" in out


def test_hosted_chat_honors_in_agent_error_flag(monkeypatch, tmp_path, capsys):
    _wire(monkeypatch, tmp_path,
          status={"status": "active"},
          invoke={"result": "Could not reach a Check Point MCP server.", "error": True})
    rc, out = _run_chat(capsys)
    assert rc == 1                          # never a green done on a failed run
    assert "Hosted agent could not complete the task:" in out
    assert "python3 -m chkpmcpaz status" in out


def test_hosted_chat_clean_result_exits_0(monkeypatch, tmp_path, capsys):
    calls = _wire(monkeypatch, tmp_path,
                  status={"status": "active"},
                  invoke={"result": "14 hosts", "error": False})
    rc, out = _run_chat(capsys)
    assert rc == 0
    assert calls["invoke"] == 1
    assert "14 hosts" in out


def test_hosted_chat_transport_failure_points_at_local_runtime(monkeypatch, tmp_path, capsys):
    _wire(monkeypatch, tmp_path,
          status={"status": "active"},
          invoke=ConnectionError("socket closed by peer"))
    rc, out = _run_chat(capsys)
    assert rc == 1
    # the field-tested local runtime is unaffected -- the CLI must say so
    assert "chkpmcpaz chat" in out


# --- deploy_hosted_agent: env vars handed to create_version ---------------------

class _FlexMeta(type):
    def __getattr__(cls, name):          # enum access, e.g. AgentEndpointProtocol.RESPONSES
        return name.lower()


class _Flex(metaclass=_FlexMeta):
    """Records kwargs and exposes them as attributes (any model class)."""

    def __init__(self, *args, **kwargs):
        self._args, self.kwargs = args, kwargs
        for k, v in kwargs.items():
            setattr(self, k, v)


def _active():
    return SimpleNamespace(status="active", version="1", id="v1",
                           name="chkpmcp-agent",
                           instance_identity=SimpleNamespace(principal_id="pid"))


def _install_fake_projects_sdk(monkeypatch, recorded):
    class _Agents:
        def create_version(self, *args, **kwargs):
            recorded["create_version"] = {"args": args, "kwargs": kwargs}
            return _active()

        def __getattr__(self, name):     # get_version / update_details / ...
            return lambda *a, **k: _active()

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            recorded["client"] = kwargs
            self.agents = _Agents()

        def get_openai_client(self, **kw):
            return SimpleNamespace(responses=SimpleNamespace(
                create=lambda **k: SimpleNamespace(output_text="ok")))

    projects = types.ModuleType("azure.ai.projects")
    projects.AIProjectClient = _FakeClient
    models = types.ModuleType("azure.ai.projects.models")

    def _make(name):
        if name.startswith("__"):
            raise AttributeError(name)
        return _FlexMeta(name, (_Flex,), {})

    models.__getattr__ = _make
    projects.__getattr__ = lambda n: (_FakeClient if n == "AIProjectClient" else _make(n))
    projects.models = models
    ai = types.ModuleType("azure.ai")
    ai.projects = projects
    monkeypatch.setitem(sys.modules, "azure.ai", ai)
    monkeypatch.setitem(sys.modules, "azure.ai.projects", projects)
    monkeypatch.setitem(sys.modules, "azure.ai.projects.models", models)
    # Patch the lazy resolvers directly too: relying on sys.modules alone is
    # order-fragile -- if an earlier test caused the REAL azure.ai.projects to
    # be imported, `import azure.ai.projects.models` can bind the real module
    # and the fake never takes effect (create_version would then receive a real
    # SDK object, not a _Flex, and _find_agent_env would miss the env dict).
    monkeypatch.setattr(hosting, "_sdk", lambda: _FakeClient, raising=False)
    monkeypatch.setattr(hosting, "_models",
                        lambda: (models, lambda n: getattr(models, n)),
                        raising=False)


def _find_agent_env(node, depth=0):
    """Dig the environment_variables dict out of whatever create_version got."""
    if depth > 4 or node is None:
        return None
    if isinstance(node, dict) and set(node) & {"CLAUDE_BASE_URL", "CHKP_PREFIX"}:
        return node
    values = []
    if isinstance(node, dict):
        values = list(node.values())
    elif isinstance(node, (list, tuple)):
        values = list(node)
    elif isinstance(node, _Flex):
        values = list(node.kwargs.values())
    for v in values:
        found = _find_agent_env(v, depth + 1)
        if found is not None:
            return found
    return None


def test_deploy_hosted_agent_env_vars_are_exactly_the_contract_keys(monkeypatch, tmp_path):
    recorded = {}
    _install_fake_projects_sdk(monkeypatch, recorded)
    monkeypatch.setattr(hosting, "time", SimpleNamespace(sleep=lambda *_: None,
                                                         time=lambda: 0.0,
                                                         monotonic=lambda: 0.0),
                        raising=False)
    _patch_everywhere(monkeypatch, "get_credential", lambda: object())
    cfg = StackConfig()
    hosting.deploy_hosted_agent(cfg, dict(FAKE_ENV))

    assert "create_version" in recorded, "create_version was never called"
    kwargs = recorded["create_version"]["kwargs"]
    assert kwargs.get("agent_name", cfg.agent_name()) == "chkpmcp-agent"
    env = _find_agent_env({"kwargs": kwargs, "args": recorded["create_version"]["args"]})
    assert env is not None, "no environment_variables dict reached create_version"
    assert set(env) == EXPECTED_AGENT_ENV_KEYS
    # never redeclare platform-injected FOUNDRY_* vars
    assert not any(k.startswith("FOUNDRY_") for k in env)
    # values wire the stack outputs through
    assert env["CLAUDE_BASE_URL"] == FAKE_ENV["CLAUDE_BASE_URL"]
    assert env["KEY_VAULT_URI"] == FAKE_ENV["KEY_VAULT_URI"]
    assert env["CHKP_PREFIX"] == "chkpmcp"
    assert env["CHKP_PROVIDER"] == "anthropic"


# --- agent_environment: provider-aware injection (CONTRACT section 6e) ----------
# The hosted container gets CHKP_PROVIDER always, plus ONLY the active provider's
# endpoint/deployment vars -- so a gpt stack carries OPENAI_* + CHKP_MODEL and a
# Claude stack carries CLAUDE_* (they never both appear).

_GPT_ENV = {
    **FAKE_ENV,
    "CHKP_PROVIDER": "azure-openai",
    "OPENAI_BASE_URL": "https://chkpmcp-foundry-tok.services.ai.azure.com",
    "OPENAI_MODEL_DEPLOYMENT": "gpt-5-mini",
}


def test_agent_environment_anthropic_injects_provider_not_openai():
    env = hosting.agent_environment(StackConfig(), dict(FAKE_ENV))
    assert set(env) == EXPECTED_AGENT_ENV_KEYS
    assert env["CHKP_PROVIDER"] == "anthropic"
    assert env["CLAUDE_BASE_URL"] == FAKE_ENV["CLAUDE_BASE_URL"]
    # a Claude stack never carries the OpenAI vars
    assert "OPENAI_BASE_URL" not in env and "OPENAI_MODEL_DEPLOYMENT" not in env
    assert "CHKP_MODEL" not in env


def test_agent_environment_azure_openai_injects_openai_and_model():
    cfg = StackConfig(provider="azure-openai",
                      openai_base_url="https://chkpmcp-foundry-tok.services.ai.azure.com",
                      openai_deployment="gpt-5-mini")
    env = hosting.agent_environment(cfg, dict(_GPT_ENV))
    assert env["CHKP_PROVIDER"] == "azure-openai"
    assert env["OPENAI_BASE_URL"] == _GPT_ENV["OPENAI_BASE_URL"]
    assert env["OPENAI_MODEL_DEPLOYMENT"] == "gpt-5-mini"
    # the hosted container pins the same deployment via CHKP_MODEL
    assert env["CHKP_MODEL"] == "gpt-5-mini"
    # a gpt stack never carries the Claude vars
    assert "CLAUDE_BASE_URL" not in env and "CLAUDE_MODEL_DEPLOYMENT" not in env
    # shared, provider-neutral keys are still present
    assert env["KEY_VAULT_URI"] == FAKE_ENV["KEY_VAULT_URI"]
    assert env["CHKP_PREFIX"] == "chkpmcp"
    assert not any(k.startswith("FOUNDRY_") for k in env)


# ---------------------------------------------------------------------------
# _current_version_image: a refresh with no freshly-built digest must reuse the
# ACTIVE version's image (a known-good digest), never fall back to a :tag that
# may not resolve in the registry (the ImageError that failed a live deploy).
# ---------------------------------------------------------------------------

def _mkver(status, image):
    return SimpleNamespace(
        status=status,
        definition=SimpleNamespace(
            container_configuration=SimpleNamespace(image=image)))


def test_current_version_image_reuses_active_digest():
    versions = [
        _mkver("AgentVersionStatus.FAILED", "acr/chkp-agent:v1"),        # newest, failed
        _mkver("AgentVersionStatus.ACTIVE", "acr/chkp-agent@sha256:good"),  # reuse this
        _mkver("active", "acr/chkp-agent@sha256:older"),
    ]
    project = SimpleNamespace(agents=SimpleNamespace(
        list_versions=lambda agent_name: versions))
    assert hosting._current_version_image(project, "chkp-agent") == "acr/chkp-agent@sha256:good"


def test_current_version_image_none_when_no_active():
    versions = [_mkver("failed", "acr/x:v1"), _mkver("creating", "acr/x:v1")]
    project = SimpleNamespace(agents=SimpleNamespace(
        list_versions=lambda agent_name: versions))
    assert hosting._current_version_image(project, "chkp-agent") is None


def test_current_version_image_swallows_lookup_errors():
    def boom(**_):
        raise RuntimeError("projects API unavailable")
    project = SimpleNamespace(agents=SimpleNamespace(list_versions=boom))
    assert hosting._current_version_image(project, "chkp-agent") is None


# ---------------------------------------------------------------------------
# A guardrail block is a SUCCESS, not an error: the hosted path must render it
# as a security win and exit 0 -- never the "could not complete / data-path" path.
# ---------------------------------------------------------------------------

def test_hosted_chat_guardrail_block_is_a_win_not_an_error(monkeypatch, tmp_path, capsys):
    _wire(monkeypatch, tmp_path,
          status={"status": "active"},
          invoke={"result": "Prompt blocked by Check Point AI Guardrail (attack detected): prompt attack.",
                  "blocked": True, "error": False})
    rc, out = _run_chat(capsys)
    assert rc == 0                                  # a block is a success
    assert "\U0001F6E1 Prompt blocked by Check Point AI Guardrail" in out
    assert "Prompt blocked by Check Point AI Guardrail" in out
    assert "could not complete" not in out          # never the error path
    assert "data-path issue" not in out


def test_looks_like_guardrail_block_signature():
    assert hosting._looks_like_guardrail_block(
        "GuardrailBlocked: Prompt blocked by X (attack detected): prompt attack.")
    assert hosting._looks_like_guardrail_block(
        "Prompt blocked by Prompt Shields (attack detected).")
    assert not hosting._looks_like_guardrail_block("Could not reach a Check Point MCP server.")
    assert not hosting._looks_like_guardrail_block("")


def test_deploy_hosted_agent_roll_injects_a_refresh_marker(monkeypatch, tmp_path):
    """roll=True (deploy --creds) must mint a FRESH version via a changing
    CHKP_REFRESH marker so the sandboxes reboot and re-read the new secrets."""
    recorded = {}
    _install_fake_projects_sdk(monkeypatch, recorded)
    monkeypatch.setattr(hosting, "time",
                        SimpleNamespace(sleep=lambda *_: None, time=lambda: 0.0,
                                        monotonic=lambda: 0.0, gmtime=lambda *_: 0,
                                        strftime=lambda *a, **k: "TS"),
                        raising=False)
    _patch_everywhere(monkeypatch, "get_credential", lambda: object())
    hosting.deploy_hosted_agent(StackConfig(), dict(FAKE_ENV), roll=True)
    env = _find_agent_env({"kwargs": recorded["create_version"]["kwargs"],
                           "args": recorded["create_version"]["args"]})
    assert env.get("CHKP_REFRESH") == "TS", "roll=True must inject a CHKP_REFRESH marker"
