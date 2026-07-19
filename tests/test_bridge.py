"""Pure-logic tests for the agent HTTPS bridge (the Azure analogue of the AWS
bearer-token front door). No Azure calls, no network -- only the deterministic
name derivation, the session-id sanitizer, the token generator, the zip
package shape, and the org-policy invariant that the bearer token is NEVER
embedded in the handler source.
"""

import io
import zipfile

import chkpmcpaz.bridge as bridge
from chkpmcpaz.config import StackConfig


def _cfg_env(sub="sub-123", prefix="chkpmcp"):
    cfg = StackConfig(prefix=prefix, subscription_id=sub)
    env = {"AZURE_SUBSCRIPTION_ID": sub, "AZURE_RESOURCE_GROUP": f"rg-{prefix}"}
    return cfg, env


def test_bridge_names_are_deterministic_and_charset_valid():
    cfg, env = _cfg_env()
    a = bridge.bridge_names(cfg, env)
    b = bridge.bridge_names(cfg, env)
    assert a == b                                   # stable across runs -> idempotent
    # storage: 3-24 lowercase alphanumeric (Azure hard limits)
    st = a["storage"]
    assert 3 <= len(st) <= 24 and st.isalnum() and st.islower()
    assert a["token_secret"] == "chkpmcp-bridge-token"
    assert a["url"].startswith("https://") and a["url"].endswith("/api/invoke")


def test_bridge_names_change_with_subscription_and_prefix():
    a = bridge.bridge_names(*_cfg_env(sub="sub-A"))
    b = bridge.bridge_names(*_cfg_env(sub="sub-B"))
    c = bridge.bridge_names(*_cfg_env(prefix="demo2"))
    assert a["function_app"] != b["function_app"]   # different sub -> different suffix
    assert a["function_app"] != c["function_app"]   # different prefix -> different name


def test_session_id_is_sanitized_and_bounded():
    sid = bridge.session_id("a b/c!@#")
    assert sid.startswith("chkpmcp-bridge-")
    assert all(ch.isalnum() or ch in "-_" for ch in sid)
    assert len(sid) <= 100
    assert bridge.session_id(None).startswith("chkpmcp-bridge-default")


def test_new_token_is_long_and_unique():
    t1, t2 = bridge.new_token(), bridge.new_token()
    assert t1 != t2                                 # never a fixed/hardcoded token
    assert len(t1) >= 32


def test_zip_package_shape_and_no_embedded_token():
    zf = zipfile.ZipFile(io.BytesIO(bridge._zip_bytes()))
    names = set(zf.namelist())
    assert {"function_app.py", "requirements.txt", "host.json"} <= names
    handler = zf.read("function_app.py").decode("utf-8")
    # org policy: the token is READ from Key Vault at runtime, never baked in.
    assert "KEY_VAULT_URI" in handler and "SecretClient" in handler
    assert "hmac.compare_digest" in handler        # constant-time auth check
    # the handler never disables TLS verification
    assert "verify=False" not in handler and "verify = False" not in handler


def test_describe_without_stack_reports_absent():
    assert bridge.describe(StackConfig(), {}) == {"present": False, "url": None}


def test_run_bridge_unknown_action_returns_2(monkeypatch):
    monkeypatch.setattr(bridge, "azd_env_values", lambda prefix=None: {}, raising=False)
    # patch the lazily-imported helpers the dispatcher pulls from azutil
    import chkpmcpaz.azutil as azutil
    monkeypatch.setattr(azutil, "azd_env_values", lambda prefix=None: {})
    monkeypatch.setattr(azutil, "hydrate_config", lambda cfg, env: cfg)
    assert bridge.run_bridge(StackConfig(), "nonsense") == 2
