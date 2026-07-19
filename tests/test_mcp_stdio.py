"""stdio MCP client plumbing -- the pure parts only. The module must import
WITHOUT the optional `mcp` package installed (lazy import contract); spawning
and the ServerPool lifecycle are validated live, not here.
"""

import chkpmcpaz.mcp_stdio as mcp_stdio
from chkpmcpaz.config import SERVERS, split_namespaced, target_name, tool_namespace
from chkpmcpaz.mcp_stdio import NamespacedTool, build_child_env, npx_command


def test_module_imports_without_mcp_installed():
    # merely importing at collection time is the test -- the suite runs with
    # only the [dev] extra; a top-level `import mcp` would have crashed above
    assert hasattr(mcp_stdio, "ServerPool")


# --- npx command construction -----------------------------------------------------

def test_npx_command_uses_the_pinned_package():
    cmd = npx_command(SERVERS["quantum-management"])
    assert cmd == ["npx", "-y", "@chkp/quantum-management-mcp@1.4.7"]


def test_npx_command_appends_catalog_args():
    cmd = npx_command(SERVERS["documentation"])
    assert cmd == ["npx", "-y", "@chkp/documentation-mcp@1.4.6", "--region", "US"]


# --- child environment --------------------------------------------------------------

def test_build_child_env_merges_creds_and_disables_telemetry():
    base = {"PATH": "/usr/bin:/bin", "HOME": "/home/analyst"}
    creds = {"MANAGEMENT_HOST": "mgmt.example.com", "API_KEY": "k"}
    env = build_child_env(base, creds)
    assert env["TELEMETRY_DISABLED"] == "true"
    assert env["API_KEY"] == "k"
    assert env["PATH"] == "/usr/bin:/bin"           # parent env preserved
    assert "TELEMETRY_DISABLED" not in base          # input never mutated
    assert "API_KEY" not in base


def test_build_child_env_without_creds():
    env = build_child_env({"PATH": "/usr/bin"}, None)
    assert env["TELEMETRY_DISABLED"] == "true"
    assert env["PATH"] == "/usr/bin"


# --- schema normalization (the NamespacedTool.input_schema invariant) ----------------

def test_object_schema_normalizes_sloppy_schemas():
    # NamespacedTool.input_schema is contractually "normalized to an object
    # schema" -- a non-object or missing schema must never reach Anthropic
    assert mcp_stdio._object_schema({"type": "string"}) == {"type": "object",
                                                            "properties": {}}
    assert mcp_stdio._object_schema(None) == {"type": "object", "properties": {}}
    ok = {"type": "object", "properties": {"a": {"type": "string"}}}
    assert mcp_stdio._object_schema(ok) == ok
    assert mcp_stdio._object_schema({"type": "object"})["properties"] == {}


# --- tool namespacing round-trip ------------------------------------------------------

def test_namespacing_round_trips_for_every_catalog_server():
    for server in SERVERS:
        namespaced = tool_namespace(server, "show_hosts")
        assert split_namespaced(namespaced) == (target_name(server), "show_hosts")
        assert "-" not in namespaced.split("___")[0]     # targets strip hyphens


def test_namespaced_tool_field_contract():
    t = NamespacedTool(namespaced="quantummanagement___show_hosts",
                       server="quantum-management",
                       description="List host objects.",
                       input_schema={"type": "object", "properties": {}})
    assert t.namespaced == "quantummanagement___show_hosts"
    assert t.server == "quantum-management"
    assert t.input_schema["type"] == "object"
