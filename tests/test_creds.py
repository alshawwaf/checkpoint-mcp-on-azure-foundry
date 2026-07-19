"""Credentials workflow: template generation, INI parsing (case-preserving,
no % interpolation -- API keys contain '%', '=', '+', '/'), and apply-time
guards (unknown/empty/still-placeholder sections are skipped with reasons,
and secret VALUES never reach stdout).
"""

import pytest

import chkpmcpaz.creds as creds
import chkpmcpaz.keyvault as keyvault
from chkpmcpaz.config import PLACEHOLDER_VALUE, StackConfig

CFG = StackConfig(key_vault_uri="https://kv-unittest.vault.azure.net/")


# --- template -------------------------------------------------------------------

def test_template_covers_deployed_servers_plus_gaia(tmp_path):
    path = tmp_path / "chkp-credentials.env"
    creds.write_template(str(path), ["quantum-management", "documentation"])
    text = path.read_text(encoding="utf-8")
    assert "[quantum-management]" in text
    for key in ("MANAGEMENT_HOST", "MANAGEMENT_PORT", "API_KEY"):
        assert key in text
    assert "[documentation]" in text
    assert "CLIENT_ID" in text and "SECRET_KEY" in text
    # the agent-side gaia secret is always part of the creds workflow
    assert "[quantum-gaia]" in text
    assert "GAIA_PASSWORD" in text
    # placeholder values only -- never anything real
    assert PLACEHOLDER_VALUE in text


def test_template_refuses_to_overwrite(tmp_path):
    path = tmp_path / "chkp-credentials.env"
    creds.write_template(str(path), ["quantum-management"])
    path.write_text("SENTINEL -- hand-edited real credentials\n", encoding="utf-8")
    try:
        creds.write_template(str(path), ["quantum-management"])
    except Exception:
        pass    # raising is an acceptable way to refuse
    assert path.read_text(encoding="utf-8").startswith("SENTINEL")


# --- parsing --------------------------------------------------------------------

def test_parse_preserves_case_and_special_chars(tmp_path):
    path = tmp_path / "c.env"
    path.write_text(
        "[quantum-management]\n"
        "MANAGEMENT_HOST=mgmt.example.com\n"
        "API_KEY=+DcTTZeCy7/abc=dyA==\n"
        "lower_key=kept-as-is\n",
        encoding="utf-8")
    doc = creds.parse_creds_file(str(path))
    section = doc["quantum-management"]
    assert section["MANAGEMENT_HOST"] == "mgmt.example.com"
    assert section["API_KEY"] == "+DcTTZeCy7/abc=dyA=="    # '=' survives in values
    assert "lower_key" in section                           # case preserved


def test_parse_does_not_interpolate_percent(tmp_path):
    path = tmp_path / "c.env"
    path.write_text("[reputation-service]\nAPI_KEY=ab%cd%%ef\n", encoding="utf-8")
    doc = creds.parse_creds_file(str(path))
    assert doc["reputation-service"]["API_KEY"] == "ab%cd%%ef"


def test_parse_skips_unknown_sections(tmp_path):
    # CONTRACT 6.7 pins this on parse_creds_file itself ("unknown sections ->
    # skip with reason"), not only on apply_file -- direct callers of the
    # parser must never see a section that maps to no catalog server
    path = tmp_path / "c.env"
    path.write_text(
        "[quantum-management]\nAPI_KEY=k\n\n[not-a-server]\nFOO=bar\n",
        encoding="utf-8")
    doc = creds.parse_creds_file(str(path))
    assert "quantum-management" in doc
    assert "not-a-server" not in doc


# --- apply ----------------------------------------------------------------------

def _wire_apply(monkeypatch):
    written = {}

    def _set(vault_uri, name, payload, credential=None):
        written[name] = dict(payload)

    for mod in (creds, keyvault):
        monkeypatch.setattr(mod, "set_secret_json", _set, raising=False)
        monkeypatch.setattr(mod, "get_secret_json",
                            lambda *a, **k: None, raising=False)
    # the post-apply refresh is hosting/CLI machinery -- stub every likely binding
    for name in ("refresh", "_refresh", "trigger_refresh", "run_refresh",
                 "refresh_hosted_agent"):
        monkeypatch.setattr(creds, name, lambda *a, **k: "v2", raising=False)
    try:
        monkeypatch.setattr("chkpmcpaz.hosting.refresh_hosted_agent",
                            lambda *a, **k: "v2", raising=False)
    except Exception:
        pass
    return written


def test_apply_writes_real_sections_and_skips_the_rest(tmp_path, monkeypatch, capsys):
    written = _wire_apply(monkeypatch)
    path = tmp_path / "c.env"
    path.write_text(
        "[quantum-management]\n"
        "MANAGEMENT_HOST=mgmt.example.com\n"
        "MANAGEMENT_PORT=443\n"
        "API_KEY=real-key-abc123\n"
        "\n"
        f"[documentation]\nCLIENT_ID={PLACEHOLDER_VALUE}\nSECRET_KEY={PLACEHOLDER_VALUE}\n"
        "\n"
        "[not-a-server]\nFOO=bar\n"
        "\n"
        "[reputation-service]\n",
        encoding="utf-8")
    creds.apply_file(CFG, str(path))
    # only the real section reached Key Vault, under the derived secret name
    assert list(written) == [CFG.secret_name("quantum-management")]
    assert written[CFG.secret_name("quantum-management")]["API_KEY"] == "real-key-abc123"
    out = capsys.readouterr().out
    # skips are explained by section name...
    assert "documentation" in out and "not-a-server" in out
    # ...and secret values are never printed
    assert "real-key-abc123" not in out


def test_apply_never_prints_values_for_written_sections(tmp_path, monkeypatch, capsys):
    _wire_apply(monkeypatch)
    path = tmp_path / "c.env"
    path.write_text("[threat-emulation]\nAPI_KEY=te-secret-value-987\n", encoding="utf-8")
    creds.apply_file(CFG, str(path))
    captured = capsys.readouterr()
    assert "te-secret-value-987" not in captured.out + captured.err


def test_apply_file_refresh_false_skips_the_hosted_rollout(tmp_path, monkeypatch):
    """deploy --creds applies secrets BEFORE the image build, so it must NOT
    refresh the agent here (that would pin a not-yet-built image and fail)."""
    import chkpmcpaz.hosting as hosting
    written = _wire_apply(monkeypatch)
    calls = {"refresh": 0}
    monkeypatch.setattr(hosting, "run_refresh",
                        lambda cfg: calls.__setitem__("refresh", calls["refresh"] + 1) or 0,
                        raising=False)
    path = tmp_path / "c.env"
    path.write_text("[threat-emulation]\nAPI_KEY=real-abc\n", encoding="utf-8")
    rc = creds.apply_file(CFG, str(path), refresh=False)
    assert rc == 0
    assert written[CFG.secret_name("threat-emulation")]["API_KEY"] == "real-abc"  # secret written
    assert calls["refresh"] == 0                                                   # ...but no rollout


def test_apply_file_default_refreshes_the_hosted_agent(tmp_path, monkeypatch):
    import chkpmcpaz.hosting as hosting
    _wire_apply(monkeypatch)
    calls = {"refresh": 0}
    monkeypatch.setattr(hosting, "run_refresh",
                        lambda cfg: calls.__setitem__("refresh", calls["refresh"] + 1) or 0,
                        raising=False)
    path = tmp_path / "c.env"
    path.write_text("[threat-emulation]\nAPI_KEY=real-abc\n", encoding="utf-8")
    creds.apply_file(CFG, str(path))
    assert calls["refresh"] == 1
