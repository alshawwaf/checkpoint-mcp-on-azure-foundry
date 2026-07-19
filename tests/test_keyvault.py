"""Key Vault secret helpers against a fake SecretClient (no Azure, no network).
The placeholder model is load-bearing: deploy seeds placeholder bodies and
must NEVER clobber a secret whose current body holds real values -- the
is_placeholder() guard is what makes re-runs safe.
"""

import json
from types import SimpleNamespace

from azure.core.exceptions import ResourceNotFoundError

import chkpmcpaz.keyvault as keyvault
from chkpmcpaz.config import CRED_SHAPE, PLACEHOLDER_VALUE

VAULT = "https://kv-unittest.vault.azure.net/"

_STORES: dict[str, dict[str, str]] = {}


class _FakeSecretClient:
    """In-memory stand-in for azure.keyvault.secrets.SecretClient."""

    def __init__(self, vault_url=None, credential=None, **kw):
        self.store = _STORES.setdefault(vault_url, {})

    def get_secret(self, name):
        if name not in self.store:
            raise ResourceNotFoundError(f"secret {name!r} not found")
        return SimpleNamespace(name=name, value=self.store[name])

    def set_secret(self, name, value, **kw):
        self.store[name] = value
        return SimpleNamespace(name=name, value=value)

    def list_properties_of_secrets(self, **kw):
        return [SimpleNamespace(name=n) for n in list(self.store)]

    def get_deleted_secret(self, name):
        raise ResourceNotFoundError(f"no deleted secret {name!r}")

    def begin_recover_deleted_secret(self, name):
        return SimpleNamespace(wait=lambda: None, result=lambda: None)

    def begin_delete_secret(self, name):
        self.store.pop(name, None)
        return SimpleNamespace(wait=lambda: None, result=lambda: None)

    def purge_deleted_secret(self, name):
        return None


def _wire(monkeypatch):
    _STORES.clear()
    # cover both a module-level bound name and a lazy in-function import
    monkeypatch.setattr(keyvault, "SecretClient", _FakeSecretClient, raising=False)
    monkeypatch.setattr("azure.keyvault.secrets.SecretClient", _FakeSecretClient)


# --- is_placeholder matrix -----------------------------------------------------

def test_is_placeholder_matrix():
    assert keyvault.is_placeholder(None) is True
    assert keyvault.is_placeholder({}) is True
    # ANY placeholder value marks the whole body as placeholder -- the
    # management shape carries real-looking defaults next to the sentinel
    assert keyvault.is_placeholder(dict(CRED_SHAPE["management"])) is True
    assert keyvault.is_placeholder({"API_KEY": PLACEHOLDER_VALUE, "HOST": "real.example"}) is True
    assert keyvault.is_placeholder(
        {"MANAGEMENT_HOST": "mgmt.example.com", "MANAGEMENT_PORT": "443",
         "API_KEY": "real-key-abc123"}) is False


def test_placeholder_value_reexported():
    assert keyvault.PLACEHOLDER_VALUE == PLACEHOLDER_VALUE


# --- JSON round-trip ------------------------------------------------------------

def test_set_get_json_round_trip(monkeypatch):
    _wire(monkeypatch)
    payload = {"MANAGEMENT_HOST": "mgmt.example.com", "MANAGEMENT_PORT": "443",
               "API_KEY": "real-key-abc123"}
    keyvault.set_secret_json(VAULT, "chkpmcp-quantum-management", payload, credential=object())
    got = keyvault.get_secret_json(VAULT, "chkpmcp-quantum-management", credential=object())
    assert got == payload
    # the stored body is JSON of exactly those keys
    assert json.loads(_STORES[VAULT]["chkpmcp-quantum-management"]) == payload


def test_get_secret_json_absent_returns_none(monkeypatch):
    _wire(monkeypatch)
    assert keyvault.get_secret_json(VAULT, "chkpmcp-never-written", credential=object()) is None


def test_secret_values_are_never_printed(monkeypatch, capsys):
    _wire(monkeypatch)
    keyvault.set_secret_json(VAULT, "chkpmcp-threat-emulation",
                             {"API_KEY": "super-secret-value-xyz"}, credential=object())
    keyvault.get_secret_json(VAULT, "chkpmcp-threat-emulation", credential=object())
    captured = capsys.readouterr()
    assert "super-secret-value-xyz" not in captured.out + captured.err


# --- stack listing + delete ------------------------------------------------------

def test_list_stack_secrets_filters_by_prefix(monkeypatch):
    _wire(monkeypatch)
    for name in ("chkpmcp-quantum-management", "chkpmcp-documentation", "unrelated-secret"):
        keyvault.set_secret_json(VAULT, name, {"K": "v"}, credential=object())
    names = keyvault.list_stack_secrets(VAULT, "chkpmcp", credential=object())
    assert "chkpmcp-quantum-management" in names
    assert "chkpmcp-documentation" in names
    assert all("unrelated-secret" not in n for n in names)


def test_delete_secret_removes(monkeypatch):
    _wire(monkeypatch)
    keyvault.set_secret_json(VAULT, "chkpmcp-documentation", {"K": "v"}, credential=object())
    keyvault.delete_secret(VAULT, "chkpmcp-documentation", credential=object())
    assert "chkpmcp-documentation" not in _STORES[VAULT]
    # purge=True on an already-gone secret must not raise (idempotent destroy)
    keyvault.delete_secret(VAULT, "chkpmcp-documentation", purge=True, credential=object())


# --- deploy seeding: the never-clobber rule ---------------------------------------

def test_deploy_seeding_consults_the_placeholder_guard():
    """The seeding step (CLI-owned deploy.py) must gate every write on
    is_placeholder() -- source-level check because the step function itself
    is not part of the frozen interface."""
    import pathlib

    import chkpmcpaz.deploy as deploy
    src = pathlib.Path(deploy.__file__).read_text(encoding="utf-8")
    assert "is_placeholder" in src, (
        "deploy.py never consults keyvault.is_placeholder -- placeholder "
        "seeding could clobber real credentials on re-runs")
