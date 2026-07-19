"""Gaia elicitation answerer: pure field mapping (aliases, case-insensitive,
decline-on-missing-required) and credential sourcing (env first, then the
Key Vault gaia secret; placeholder bodies mean 'not configured').
The async callback itself needs the mcp package and is exercised by the
runtime's own live validation -- here we pin the pure logic it delegates to.
"""

import chkpmcpaz.gaia as gaia
import chkpmcpaz.keyvault as keyvault
from chkpmcpaz.config import CRED_SHAPE, StackConfig
from chkpmcpaz.gaia import load_gaia_creds, map_fields

CREDS = {"GAIA_GATEWAY_IP": "10.1.1.5", "GAIA_PORT": "8443",
         "GAIA_USER": "admin", "GAIA_PASSWORD": "s3cret"}


# --- map_fields alias matrix -----------------------------------------------------

def test_map_fields_full_login_form():
    out = map_fields(["gateway_ip", "port", "user", "password"], CREDS)
    assert out is not None
    assert out["gateway_ip"] == "10.1.1.5"
    assert str(out["port"]) == "8443"
    assert out["user"] == "admin"
    assert out["password"] == "s3cret"


def test_map_fields_gateway_aliases():
    for alias in ("gateway_ip", "ip", "address", "host"):
        out = map_fields([alias], CREDS)
        assert out == {alias: "10.1.1.5"}, alias


def test_map_fields_username_alias():
    assert map_fields(["username"], CREDS) == {"username": "admin"}


def test_map_fields_is_case_insensitive_but_answers_verbatim():
    # the server rejects answers keyed differently than it asked, so the
    # requested spelling must be preserved even when matched case-insensitively
    out = map_fields(["Gateway_IP", "USER"], CREDS)
    assert out is not None
    assert set(out) == {"Gateway_IP", "USER"}
    assert out["Gateway_IP"] == "10.1.1.5" and out["USER"] == "admin"


def test_map_fields_declines_when_a_required_field_is_unfillable():
    # a one-time code we cannot supply -> None (decline fast, never hang)
    assert map_fields(["user", "otp_code"], CREDS) is None


# --- credential sourcing -----------------------------------------------------------

CFG = StackConfig(key_vault_uri="https://kv-unittest.vault.azure.net/")


def _wire_kv(monkeypatch, payload):
    requested = []

    def _get(vault_uri, name, credential=None):
        requested.append(name)
        return payload

    for mod in (gaia, keyvault):
        monkeypatch.setattr(mod, "get_secret_json", _get, raising=False)
    return requested


def test_env_creds_win_without_touching_key_vault(monkeypatch):
    requested = _wire_kv(monkeypatch, {"GAIA_PASSWORD": "from-kv"})
    out = load_gaia_creds(CFG, env=CREDS)
    assert out is not None and out["GAIA_PASSWORD"] == "s3cret"
    assert requested == []          # env creds short-circuit the KV read


def test_kv_fallback_reads_the_gaia_secret(monkeypatch):
    real = {"GAIA_GATEWAY_IP": "10.9.9.9", "GAIA_PORT": "443",
            "GAIA_USER": "admin", "GAIA_PASSWORD": "kv-secret"}
    requested = _wire_kv(monkeypatch, real)
    out = load_gaia_creds(CFG, env={})
    assert out is not None and out["GAIA_PASSWORD"] == "kv-secret"
    assert requested == [CFG.secret_name("quantum-gaia")]   # 'chkpmcp-quantum-gaia'


def test_placeholder_kv_body_means_not_configured(monkeypatch):
    _wire_kv(monkeypatch, dict(CRED_SHAPE["gaia"]))   # seeded-but-unfilled secret
    assert load_gaia_creds(CFG, env={}) is None


def test_absent_kv_secret_means_not_configured(monkeypatch):
    _wire_kv(monkeypatch, None)
    assert load_gaia_creds(CFG, env={}) is None
