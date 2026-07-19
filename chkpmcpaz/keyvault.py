"""Key Vault secret helpers -- the Azure twin of the AWS per-server Secrets
Manager secrets (`chkp/<server>` there, `<prefix>-<server>` here; KV names
allow only [0-9a-zA-Z-]).

Bodies are JSON objects of env-var keys (see config.CRED_SHAPE). Deploy seeds
placeholders; `creds apply` writes real values; the agent reads each server's
secret at startup and injects it into that server's child-process environment.

Design decisions:
  - Secret VALUES are never logged, printed, or embedded in exceptions -- only
    secret NAMES. Every error path below names the secret, never its body.
  - `credential=None` means "construct DefaultAzureCredential() here": locally
    that resolves to `az login`, inside the hosted container to the per-agent
    Entra identity -- the same code works in both places.
  - set_secret_json recovers a soft-deleted secret first if needed, mirroring
    the AWS restore-if-scheduled-for-deletion behavior (the vault keeps 7-day
    soft deletes, so a destroy/redeploy cycle must not wedge on a tombstone).
"""

from __future__ import annotations

import json
import time

from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.identity import DefaultAzureCredential
from azure.keyvault.secrets import SecretClient

from .config import PLACEHOLDER_VALUE, validate_prefix  # noqa: F401 -- re-export

__all__ = [
    "PLACEHOLDER_VALUE",
    "get_secret_json",
    "set_secret_json",
    "is_placeholder",
    "list_stack_secrets",
    "delete_secret",
]


def _client(vault_uri: str, credential=None) -> SecretClient:
    if not vault_uri:
        raise ValueError("vault_uri is required (is KEY_VAULT_URI set? deploy first)")
    return SecretClient(vault_url=vault_uri,
                        credential=credential or DefaultAzureCredential())


def get_secret_json(vault_uri: str, name: str, credential=None) -> dict[str, str] | None:
    """Read a secret and JSON-decode its body into {key: value} strings.
    Returns None when the secret does not exist. A non-JSON body raises
    ValueError naming the secret (never quoting the body)."""
    client = _client(vault_uri, credential)
    try:
        secret = client.get_secret(name)
    except ResourceNotFoundError:
        return None
    try:
        payload = json.loads(secret.value or "")
    except ValueError:
        raise ValueError(f"secret '{name}' does not contain a JSON object") from None
    if not isinstance(payload, dict):
        raise ValueError(f"secret '{name}' does not contain a JSON object")
    return {str(k): str(v) for k, v in payload.items()}


def set_secret_json(vault_uri: str, name: str, payload: dict[str, str],
                    credential=None) -> None:
    """Create or update a secret with a JSON body. If a soft-deleted secret of
    the same name is blocking the write (vaults here keep 7-day soft deletes),
    recover it first and retry -- redeploy-after-destroy must never wedge."""
    client = _client(vault_uri, credential)
    body = json.dumps({str(k): str(v) for k, v in payload.items()})
    try:
        client.set_secret(name, body)
        return
    except ResourceExistsError:
        pass  # name is held by a soft-deleted secret -- recover it below
    client.begin_recover_deleted_secret(name).wait()
    # Recovery finishes asynchronously server-side; retry with patience.
    for delay in (2, 4, 8, 15, 30):
        try:
            client.set_secret(name, body)
            return
        except ResourceExistsError:
            time.sleep(delay)
    client.set_secret(name, body)  # final attempt -- let the real error surface


def is_placeholder(payload: dict[str, str] | None) -> bool:
    """True when the secret body is absent, empty, or still holds ANY
    placeholder value -- i.e. real credentials were never applied. Deploy uses
    this to guarantee it never clobbers real values with placeholders."""
    if not payload:
        return True
    return any(str(v) == PLACEHOLDER_VALUE for v in payload.values())


def list_stack_secrets(vault_uri: str, prefix: str, credential=None) -> list[str]:
    """Names (only names -- never values) of this stack's secrets: everything
    in the vault starting with '<prefix>-'."""
    client = _client(vault_uri, credential)
    want = f"{validate_prefix(prefix)}-"
    return sorted(p.name for p in client.list_properties_of_secrets()
                  if p.name and p.name.startswith(want))


def delete_secret(vault_uri: str, name: str, purge: bool = False,
                  credential=None) -> None:
    """Soft-delete a secret (recoverable for 7 days, mirroring the AWS
    recovery window). purge=True additionally purges the tombstone so the
    name is immediately reusable. Not-found is swallowed -- destroy re-runs
    must stay idempotent."""
    client = _client(vault_uri, credential)
    try:
        poller = client.begin_delete_secret(name)
    except ResourceNotFoundError:
        poller = None
    if purge:
        if poller is not None:
            poller.wait()
        try:
            client.purge_deleted_secret(name)
        except ResourceNotFoundError:
            pass
