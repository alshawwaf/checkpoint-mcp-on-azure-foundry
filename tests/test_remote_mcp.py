"""Remote MCP tier -- the opt-in `deploy --remote-mcp` path (Container Apps in
streamable-HTTP mode behind Entra Easy Auth). Pure logic + monkeypatched
orchestration; the live ACA/Easy Auth/Toolbox provisioning is validated by an
operator `deploy --remote-mcp`, not here.
"""

import asyncio
import json
import pathlib

import pytest

from chkpmcpaz import config, remote_mcp as R, remote_server as RS
from chkpmcpaz.mcp_remote import (
    RemoteServerPool,
    auth_headers,
    parse_endpoints,
    scope_for,
)

REPO = pathlib.Path(__file__).resolve().parents[1]


class _R:  # a stand-in for the azutil.run CompletedProcess (only .stdout is read)
    def __init__(self, stdout=""):
        self.stdout = stdout


# ===========================================================================
# config: names + descriptors
# ===========================================================================

def test_container_app_names_are_valid_for_every_server():
    import re

    for server in config.SERVERS:
        name = config.container_app_name(server, "chkpmcp")
        assert len(name) <= 32
        assert re.match(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$", name), (server, name)


def test_container_app_name_trims_long_prefix_and_stays_valid():
    name = config.container_app_name("quantum-management", "a-very-long")  # 11 chars
    assert len(name) <= 32 and not name.endswith("-")


def test_remote_derived_names_and_audience():
    assert config.container_env_name("chkpmcp") == "cae-chkpmcp"
    assert config.remote_identity_name("chkpmcp") == "id-chkpmcp-mcp"
    assert config.remote_app_registration_name("chkpmcp") == "chkpmcp-mcp-gateway"
    assert config.remote_audience("abc") == "api://abc"
    assert config.remote_scope("abc") == "api://abc/.default"
    assert config.remote_endpoint_url("host.azurecontainerapps.io") == \
        "https://host.azurecontainerapps.io/mcp"


def test_server_descriptors_shape():
    d = config.remote_server_descriptors(["quantum-management", "quantum-gaia",
                                          "documentation"], "chkpmcp")
    by = {x["server"]: x for x in d}
    assert by["quantum-management"]["appName"] == "chkpmcp-mcp-quantum-management"
    assert by["quantum-management"]["secretName"] == "chkpmcp-quantum-management"
    assert by["quantum-management"]["package"] == "@chkp/quantum-management-mcp@1.4.7"
    # documentation carries its --region args; quantum-gaia has NO server secret
    assert by["documentation"]["args"] == "--region US"
    assert by["quantum-gaia"]["secretName"] == ""


def test_server_descriptors_reject_unknown():
    with pytest.raises(ValueError):
        config.remote_server_descriptors(["nope"], "chkpmcp")


# ===========================================================================
# remote_server: the container command
# ===========================================================================

def test_parse_extra_args_and_port():
    assert RS.parse_extra_args("--region US") == ["--region", "US"]
    assert RS.parse_extra_args("") == [] and RS.parse_extra_args(None) == []
    assert RS.resolve_port("9000") == 9000
    assert RS.resolve_port("nan") == config.REMOTE_MCP_PORT
    assert RS.resolve_port(None) == config.REMOTE_MCP_PORT
    assert RS.resolve_port("99999") == config.REMOTE_MCP_PORT


def test_build_server_command_and_env():
    cmd = RS.build_server_command("@chkp/quantum-management-mcp@1.4.7", 8000,
                                  ["--region", "US"])
    assert cmd == ["npx", "-y", "@chkp/quantum-management-mcp@1.4.7",
                   "--transport", "http", "--transport-port", "8000",
                   "--region", "US"]
    env = RS.build_server_env({"PATH": "/x"}, {"API_KEY": "k"}, 8000)
    assert env["API_KEY"] == "k" and env["TELEMETRY_DISABLED"] == "true"
    assert env["MCP_TRANSPORT_TYPE"] == "http" and env["MCP_TRANSPORT_PORT"] == "8000"
    assert env["MCP_TRANSPORT_HOST"] == "0.0.0.0"


def test_remote_server_main_execs(monkeypatch):
    rec = {}
    monkeypatch.setattr(RS.os, "environ",
                        {"CHKP_PKG": "@chkp/quantum-management-mcp@1.4.7",
                         "CHKP_ARGS": "--region US", "CHKP_HTTP_PORT": "8000"})
    monkeypatch.setattr(RS, "_load_creds", lambda v, n: {"API_KEY": "k"})
    monkeypatch.setattr(RS.os, "execvpe",
                        lambda file, argv, env: rec.update(file=file, argv=argv, env=env))
    assert RS.main() == 0
    assert rec["argv"][:3] == ["npx", "-y", "@chkp/quantum-management-mcp@1.4.7"]
    assert "--region" in rec["argv"] and "US" in rec["argv"]
    assert rec["env"]["API_KEY"] == "k" and rec["env"]["MCP_TRANSPORT_TYPE"] == "http"


def test_remote_server_main_without_pkg_is_a_clean_misconfig(monkeypatch):
    monkeypatch.setattr(RS.os, "environ", {})
    assert RS.main() == 2


def test_load_creds_returns_none_without_vault():
    assert RS._load_creds(None, "chkpmcp-x") is None
    assert RS._load_creds("https://kv/", None) is None


# ===========================================================================
# mcp_remote: client pure bits + pool behavior with fake sessions
# ===========================================================================

def test_parse_endpoints_and_scope():
    eps = parse_endpoints('[{"server":"a","url":"https://a/mcp"},{"bad":1}]')
    assert eps == [{"server": "a", "url": "https://a/mcp"}]
    assert parse_endpoints("garbage") == [] and parse_endpoints(None) == []
    assert scope_for("api://abc") == "api://abc/.default"
    assert scope_for("api://abc/.default") == "api://abc/.default"
    assert scope_for("") is None
    assert auth_headers("t") == {"Authorization": "Bearer t"} and auth_headers(None) == {}


def test_pool_bearer_and_servers():
    pool = RemoteServerPool([{"server": "quantum-management", "url": "https://a/mcp"}],
                            audience="api://abc", token_provider=lambda s: "TOK:" + s)
    assert pool.servers == ["quantum-management"]
    assert pool._bearer() == "TOK:api://abc/.default"
    assert RemoteServerPool([], token="static")._bearer() == "static"
    # No audience and no token -> no bearer (endpoint's own 401 will degrade it).
    assert RemoteServerPool([], token=None)._bearer() is None


class _FakeTool:
    def __init__(self, name, description="", schema=None):
        self.name = name
        self.description = description
        self.inputSchema = schema or {"type": "object", "properties": {}}


class _FakeList:
    def __init__(self, tools):
        self.tools = tools
        self.nextCursor = None


class _FakeCall:
    def __init__(self, text, is_error=False):
        self.content = [type("B", (), {"text": text})()]
        self.isError = is_error


class _FakeSession:
    def __init__(self, tools):
        self._tools = tools

    async def list_tools(self, cursor=None):
        return _FakeList(self._tools)

    async def call_tool(self, tool, args):
        return _FakeCall(f"called {tool} {json.dumps(args)}")


def test_pool_lists_and_calls_with_fake_sessions():
    pool = RemoteServerPool([{"server": "quantum-management", "url": "https://a/mcp"}],
                            token="t")
    pool._sessions = {config.target_name("quantum-management"):
                      ("quantum-management", _FakeSession([_FakeTool("show_hosts",
                                                                     "List hosts")]))}
    tools = asyncio.run(pool.list_tools())
    assert tools[0].namespaced == "quantummanagement___show_hosts"
    assert tools[0].server == "quantum-management"

    ok = asyncio.run(pool.call("quantummanagement___show_hosts", {"x": 1}))
    assert ok["error"] is False and "called show_hosts" in ok["text"]

    # An unknown target is reported back as an error result, never raised.
    bad = asyncio.run(pool.call("missing___tool", {}))
    assert bad["error"] is True


# ===========================================================================
# remote_mcp: orchestration (mocked az) + Bicep drift guard
# ===========================================================================

def test_deployment_parameters_and_endpoints():
    cfg = config.StackConfig(prefix="chkpmcp", location="eastus2",
                             servers=("quantum-management", "documentation"))
    env = {"AZURE_CONTAINER_REGISTRY_NAME": "acr",
           "AZURE_CONTAINER_REGISTRY_ENDPOINT": "acr.azurecr.io",
           "KEY_VAULT_NAME": "kv", "KEY_VAULT_URI": "https://kv.vault.azure.net/"}
    desc = config.remote_server_descriptors(cfg.servers, cfg.prefix)
    p = R.deployment_parameters(cfg, env, "APPID", "log-chkpmcp-x",
                                "acr.azurecr.io/chkp-agent@sha256:x", desc)["parameters"]
    assert p["audienceClientId"]["value"] == "APPID"
    assert p["containerEnvName"]["value"] == "cae-chkpmcp"
    assert p["identityName"]["value"] == "id-chkpmcp-mcp"
    assert p["servers"]["value"][0]["appName"] == "chkpmcp-mcp-quantum-management"

    outs = {"endpoints": {"value": [
        {"server": "quantum-management", "fqdn": "qm.eastus2.azurecontainerapps.io"},
        {"server": "documentation"},  # missing fqdn -> dropped
    ]}}
    cat = R.endpoints_from_outputs(outs)
    assert cat == [{"server": "quantum-management",
                    "url": "https://qm.eastus2.azurecontainerapps.io/mcp"}]
    assert R.endpoints_from_outputs({}) == []


def test_remote_status_round_trip():
    cat = [{"server": "quantum-management", "url": "https://q/mcp"}]
    env = {config.ENV_REMOTE_ENDPOINTS: json.dumps(cat),
           config.ENV_REMOTE_AUDIENCE: "api://APP"}
    st = R.remote_status(env)
    assert st["audience"] == "api://APP" and st["endpoints"] == cat
    assert R.remote_status({}) is None


def test_provision_flow_mocked(monkeypatch):
    calls = {"stream": [], "azd": [], "patched": False}

    def fake_az_json(args):
        if args[:3] == ["ad", "app", "list"]:
            return []                      # none exists yet
        if args[:3] == ["ad", "app", "create"]:
            return {"appId": "APPID-1"}
        if args[:2] == ["resource", "list"]:
            return ["log-chkpmcp-tok"]
        return None

    def fake_run(cmd, **kw):
        if cmd[:2] == ["az", "deployment"] and "show" in cmd:
            outs = {"endpoints": {"value": [
                {"server": "quantum-management",
                 "appName": "chkpmcp-mcp-quantum-management",
                 "fqdn": "qm.eastus2.azurecontainerapps.io"}]}}
            return _R(json.dumps(outs))
        if cmd[:4] == ["az", "ad", "app", "show"]:
            return _R("OBJID-1")            # object id for the Graph PATCH
        if cmd[:2] == ["az", "rest"]:
            calls["patched"] = True         # requestedAccessTokenVersion PATCH
        if cmd[:3] == ["azd", "env", "set"]:
            calls["azd"].append((cmd[3], cmd[4]))
        return _R("")

    monkeypatch.setattr(R, "_az_json", fake_az_json)
    monkeypatch.setattr(R, "run", fake_run)
    monkeypatch.setattr(R, "stream", lambda cmd, **kw: calls["stream"].append(cmd))

    cfg = config.StackConfig(prefix="chkpmcp", location="eastus2",
                             servers=("quantum-management",))
    env = {"AZURE_RESOURCE_GROUP": "rg-chkpmcp",
           "AZURE_CONTAINER_REGISTRY_NAME": "acr",
           "AZURE_CONTAINER_REGISTRY_ENDPOINT": "acr.azurecr.io",
           "KEY_VAULT_NAME": "kv", "KEY_VAULT_URI": "https://kv.vault.azure.net/",
           "IMAGE_DIGEST": "sha256:x"}
    res = R.provision(cfg, env)
    assert res["app_id"] == "APPID-1" and res["audience"] == "api://APPID-1"
    assert res["catalog"][0]["url"].endswith("/mcp")
    assert not res["failures"]
    # deployed the module + persisted the catalog and audience
    assert any(c[:4] == ["az", "deployment", "group", "create"] for c in calls["stream"])
    persisted = dict(calls["azd"])
    assert config.ENV_REMOTE_ENDPOINTS in persisted
    assert persisted[config.ENV_REMOTE_AUDIENCE] == "api://APPID-1"
    # v2 access tokens set via the Graph PATCH (not the broken --set path)
    assert calls["patched"] is True


def test_teardown_deletes_app_registration_and_clears_env(monkeypatch):
    deleted, azd = [], []
    monkeypatch.setattr(R, "_az_json",
                        lambda args: ["APPID-9"] if args[:3] == ["ad", "app", "list"] else None)

    def fake_run(cmd, **kw):
        if cmd[:4] == ["az", "ad", "app", "delete"]:
            deleted.append(cmd[cmd.index("--id") + 1])
        if cmd[:3] == ["azd", "env", "set"]:
            azd.append((cmd[3], cmd[4]))
        return _R("")

    monkeypatch.setattr(R, "run", fake_run)
    notes = R.teardown(config.StackConfig(prefix="chkpmcp"), {})
    assert "APPID-9" in deleted
    assert dict(azd)[config.ENV_REMOTE_ENDPOINTS] == ""      # catalog cleared
    assert any("deleted Entra app registration" in n for n in notes)


def test_bicep_env_names_match_config_constants():
    """Guard against drift: the Bicep hardcodes the container env-var NAMES that
    chkpmcpaz.remote_server reads (Bicep can't import Python)."""
    text = (REPO / "infra/modules/remote-mcp-app.bicep").read_text()
    for name in (config.ENV_REMOTE_PKG, config.ENV_REMOTE_ARGS,
                 config.ENV_REMOTE_SECRET_NAME, config.ENV_REMOTE_HTTP_PORT,
                 config.ENV_KEY_VAULT_URI):
        assert f"'{name}'" in text, name


# ===========================================================================
# deploy wiring: the step list + need-image logic
# ===========================================================================

def test_deploy_step_list_adds_remote_and_forces_image(monkeypatch):
    from chkpmcpaz import deploy as D

    captured = {}

    class FakeUI:
        def __init__(self, *a, **k):
            captured["steps"] = list(a[2])

        def close(self, *a, **k):
            pass

    monkeypatch.setattr(D.ui, "StepUI", FakeUI)
    monkeypatch.setattr(D.ui, "activate", lambda r: None)
    monkeypatch.setattr(D.ui, "deactivate", lambda: None)
    monkeypatch.setattr(D, "azd_env_values", lambda p: {})
    monkeypatch.setattr(D, "_deploy", lambda *a, **k: (0, True, ["ok"]))

    cfg = config.StackConfig(prefix="chkpmcp", provider=config.PROVIDER_AZURE_OPENAI)
    # --no-agent + --remote-mcp: the image must still build (the tier reuses it),
    # the hosted-agent steps must NOT be present, and the remote step must be.
    D.run_deploy(cfg, include_agent=False, remote_mcp=True)
    steps = captured["steps"]
    assert "Remote MCP tier (Container Apps + Easy Auth)" in steps
    assert "Agent image (az acr build)" in steps
    assert "Hosted agent (create + route traffic)" not in steps


def test_deploy_step_list_default_has_no_remote(monkeypatch):
    from chkpmcpaz import deploy as D

    captured = {}

    class FakeUI:
        def __init__(self, *a, **k):
            captured["steps"] = list(a[2])

        def close(self, *a, **k):
            pass

    monkeypatch.setattr(D.ui, "StepUI", FakeUI)
    monkeypatch.setattr(D.ui, "activate", lambda r: None)
    monkeypatch.setattr(D.ui, "deactivate", lambda: None)
    monkeypatch.setattr(D, "azd_env_values", lambda p: {})
    monkeypatch.setattr(D, "_deploy", lambda *a, **k: (0, True, ["ok"]))

    cfg = config.StackConfig(prefix="chkpmcp", provider=config.PROVIDER_AZURE_OPENAI)
    D.run_deploy(cfg)
    assert "Remote MCP tier (Container Apps + Easy Auth)" not in captured["steps"]
