"""Answer the Gaia MCP server's login elicitation on the agent's behalf.

The published quantum-gaia server (github.com/CheckPointSW/mcp-servers,
packages/gaia/src/gaia-auth.ts) reads NO credential environment variables and
takes no credential tool-args. It authenticates per gateway by ELICITING the
details mid-call: an MCP `elicitation/create` request back to the client for
the gateway IP + port, then the Gaia admin user + password (cached ~15 min).

This module supplies an elicitation callback that fills each requested field
from credentials the operator provides, so the agent can authenticate to Gaia
non-interactively.

  ** Topology note vs. AWS **
  On AWS the AgentCore Gateway did NOT relay `elicitation/create`, so this
  answerer only worked in direct-server topologies. Here the @chkp servers are
  stdio children of the agent process itself, and stdio DOES relay
  elicitation -- the callback actually fires. quantum-gaia stays out of the
  default and 'all' sets to mirror the AWS catalog, but deploying it by name
  (`--servers quantum-gaia`) gives a working login flow.

Credentials come from (first hit wins):
  1. GAIA_GATEWAY_IP / GAIA_PORT / GAIA_USER / GAIA_PASSWORD env vars (local)
  2. Key Vault secret <prefix>-quantum-gaia (JSON) -- works local AND hosted

Set the secret via the creds workflow (the [quantum-gaia] section):
    python3 -m chkpmcpaz creds template   # writes a [quantum-gaia] section
    python3 -m chkpmcpaz creds apply      # writes <prefix>-quantum-gaia

Nothing here is logged; the password never leaves the elicitation response.
"""

from __future__ import annotations

import os
from typing import Mapping

from . import keyvault
from .config import GAIA_ENV_KEYS, StackConfig

# Map the fields a Gaia elicitation may request -> our credential keys. The
# server's schema uses names like gateway_ip / port / user / password / address
# (gaia-auth.ts); match case-insensitively and by common aliases.
_FIELD_ALIASES = {
    "gateway_ip": "GAIA_GATEWAY_IP",
    "ip": "GAIA_GATEWAY_IP",
    "address": "GAIA_GATEWAY_IP",
    "host": "GAIA_GATEWAY_IP",
    "port": "GAIA_PORT",
    "user": "GAIA_USER",
    "username": "GAIA_USER",
    "password": "GAIA_PASSWORD",
}


def _configured(creds: Mapping[str, str] | None) -> bool:
    """True when we have enough to answer a Gaia login (gateway + user + pass)."""
    creds = creds or {}
    return bool(creds.get("GAIA_GATEWAY_IP") and creds.get("GAIA_USER")
                and creds.get("GAIA_PASSWORD"))


def load_gaia_creds(cfg: StackConfig, env: Mapping[str, str] | None = None) -> dict[str, str] | None:
    """Return {GAIA_GATEWAY_IP, GAIA_PORT, GAIA_USER, GAIA_PASSWORD} from env
    vars first (handy for a quick local run), else the Key Vault secret.
    Returns None when unconfigured or still on placeholders. Non-fatal on any
    Key Vault error -- Gaia support is optional and must never break a run."""
    e = os.environ if env is None else env
    creds: dict[str, str] = {}
    for key in GAIA_ENV_KEYS:
        if e.get(key):
            creds[key.replace("GAIA_ADDRESS", "GAIA_GATEWAY_IP")] = e[key]
    if not _configured(creds) and cfg.key_vault_uri:
        try:
            payload = keyvault.get_secret_json(
                cfg.key_vault_uri, cfg.secret_name("quantum-gaia"))
        except Exception:  # noqa: BLE001 -- optional feature, degrade silently
            payload = None
        if not keyvault.is_placeholder(payload):
            for k, v in (payload or {}).items():
                creds.setdefault(str(k), str(v))  # env vars win over the secret
    # Drop unfilled placeholders so the "configured?" check stays honest.
    creds = {k: v for k, v in creds.items()
             if v and not str(v).startswith(("PLACEHOLDER", "REPLACE"))}
    if not _configured(creds):
        return None
    creds.setdefault("GAIA_PORT", "443")
    return creds


def map_fields(requested: list[str], creds: dict[str, str]) -> dict[str, str] | None:
    """Pure field mapper (unit-tested): resolve each requested field name via
    the alias table, case-insensitively. Returns {field: value} covering EVERY
    requested field, or None when any of them cannot be satisfied -- the
    caller then declines instead of hanging on a half-answered form."""
    out: dict[str, str] = {}
    for field in requested:
        key = _FIELD_ALIASES.get(
            str(field).strip().lower().replace("-", "_").replace(" ", "_"))
        val = creds.get(key) if key else None
        if val is None or str(val) == "":
            return None
        out[field] = str(val)
    return out


def make_elicitation_callback(creds: dict[str, str] | None):
    """Build an MCP elicitation callback that answers Gaia login forms from
    `creds`. Fills every requested field it can; DECLINES (fails fast, never
    hangs) when the required fields can't be satisfied. Returns None if no
    creds are configured, so the caller leaves the default behavior in place."""
    if not _configured(creds):
        return None
    assert creds is not None

    async def elicitation_callback(context, params):
        # Lazy imports so `mcp` stays an optional dependency of the package.
        from mcp.types import INVALID_REQUEST, ElicitResult, ErrorData

        schema = getattr(params, "requestedSchema", None)
        props = (schema or {}).get("properties", {}) if isinstance(schema, dict) \
            else getattr(schema, "properties", None) or {}
        if not props:
            # URL-mode or schemaless elicitation -- nothing we can answer.
            return ErrorData(code=INVALID_REQUEST, message="unsupported elicitation")
        required = (schema.get("required") if isinstance(schema, dict) else
                    getattr(schema, "required", None)) or list(props)
        # All fields if possible, else just the required ones, else decline.
        content = map_fields(list(props), creds)
        if content is None:
            content = map_fields(list(required), creds)
        if content is None:
            return ElicitResult(action="decline")
        # Coerce to the schema's declared types (port is usually an integer).
        answered: dict[str, object] = {}
        for field, val in content.items():
            prop = props.get(field)
            prop = prop if isinstance(prop, dict) else {}
            if prop.get("type") in ("integer", "number"):
                try:
                    answered[field] = int(str(val))
                except ValueError:
                    return ElicitResult(action="decline")
            else:
                answered[field] = str(val)
        return ElicitResult(action="accept", content=answered)

    return elicitation_callback
