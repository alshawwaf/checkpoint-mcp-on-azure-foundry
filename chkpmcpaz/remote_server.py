"""Remote-MCP container command: run ONE @chkp server in streamable-HTTP mode.

This is the command each per-server Azure Container App runs
(`python -m chkpmcpaz.remote_server`) when the opt-in remote tier is deployed
with `deploy --remote-mcp`. It is the Azure analogue of the AWS server image's
entrypoint.mjs: fetch the server's credentials from Key Vault (via the app's
user-assigned managed identity), inject them into the environment, then exec
the pinned @chkp package with the HTTP transport flags so it serves MCP over
`https://<app-fqdn>/mcp` behind Entra Easy Auth.

Reuses the SAME agent image (agent/Dockerfile) -- Node 20, the @chkp packages
pre-installed globally, and chkpmcpaz/ on PYTHONPATH are all already there, so
the remote tier needs no second image build, only a different container command.

Design decisions (mirroring mcp_stdio + the AWS entrypoint):
  - Credential VALUES pass only through the child process environment; nothing
    here logs or prints them (only the secret NAME and package are logged).
  - A Key Vault read failure is NOT fatal: log one line and start the server on
    whatever env is present (it will fail auth cleanly at call time, exactly how
    a placeholder-seeded stdio child degrades) -- the endpoint still lists tools.
  - Transport is requested BOTH ways the @chkp packages accept it -- the
    `--transport http --transport-port` flags AND the MCP_TRANSPORT_* env vars --
    the same belt-and-suspenders the AWS image used.
  - The process is replaced with `os.execvpe` so the server is PID-parented by
    the container runtime and receives its stop signals directly.
"""

from __future__ import annotations

import os
import shlex
import sys
from typing import Mapping

from .config import (
    ENV_KEY_VAULT_URI,
    ENV_REMOTE_ARGS,
    ENV_REMOTE_HTTP_PORT,
    ENV_REMOTE_PKG,
    ENV_REMOTE_SECRET_NAME,
    REMOTE_MCP_PORT,
)
from .mcp_stdio import build_child_env


def parse_extra_args(value: str | None) -> list[str]:
    """Split CHKP_ARGS into a list. Space-separated for parity with the AWS
    entrypoint (`(process.env.CHKP_ARGS || "").split(" ")`), but shlex-aware so
    a quoted value survives. None/'' -> []."""
    if not value or not value.strip():
        return []
    try:
        return shlex.split(value)
    except ValueError:
        # An unbalanced quote should not wedge the container -- fall back to the
        # AWS-identical naive split rather than crashing on boot.
        return [p for p in value.split(" ") if p]


def resolve_port(value: str | None) -> int:
    """The transport port from CHKP_HTTP_PORT, defaulting to REMOTE_MCP_PORT.
    A non-integer value falls back to the default (never crash on boot)."""
    try:
        port = int(str(value).strip())
    except (TypeError, ValueError):
        return REMOTE_MCP_PORT
    return port if 1 <= port <= 65535 else REMOTE_MCP_PORT


def build_server_command(package: str, port: int, extra_args: list[str]) -> list[str]:
    """The exact child command line: the pinned @chkp package plus the HTTP
    transport flags and any per-server extra args (e.g. documentation
    `--region US`). Mirrors the AWS entrypoint's spawn argv."""
    return [
        "npx", "-y", package,
        "--transport", "http",
        "--transport-port", str(port),
        *extra_args,
    ]


def build_server_env(base: Mapping[str, str], creds: dict[str, str] | None,
                     port: int) -> dict[str, str]:
    """Child env = parent env + the server's Key Vault secret body (if any) +
    TELEMETRY_DISABLED=true (via build_child_env) + the MCP_TRANSPORT_* hints the
    @chkp packages also read. Credential values are never logged."""
    env = build_child_env(base, creds)
    env["MCP_TRANSPORT_TYPE"] = "http"
    env["MCP_TRANSPORT_PORT"] = str(port)
    # Bind on all interfaces so ACA ingress can reach the server (harmless if
    # the package ignores it -- the AWS container ingress worked on the default).
    env.setdefault("MCP_TRANSPORT_HOST", "0.0.0.0")
    return env


def _log(line: str) -> None:
    print(line, flush=True)


def _load_creds(vault_uri: str | None, secret_name: str | None) -> dict[str, str] | None:
    """Fetch the server's Key Vault secret via the container's managed identity.
    Best-effort: any failure logs the NAME (never the body) and returns None so
    the server still starts (and fails auth cleanly) on placeholders."""
    if not vault_uri or not secret_name:
        return None
    try:
        from . import keyvault

        return keyvault.get_secret_json(vault_uri, secret_name)
    except Exception as exc:  # noqa: BLE001 -- a KV miss must not stop the server
        _log(f"  Key Vault read for secret '{secret_name}' failed "
             f"({type(exc).__name__}) -- starting on existing env; "
             "tool calls will fail auth until creds are applied.")
        return None


def main(argv: list[str] | None = None) -> int:
    """Container entry: hydrate creds, then exec the @chkp server in HTTP mode.
    Returns non-zero only on a misconfiguration caught before exec; on success
    it never returns (execvpe replaces the process)."""
    env = dict(os.environ)
    package = env.get(ENV_REMOTE_PKG, "").strip()
    if not package:
        _log(f"FATAL: {ENV_REMOTE_PKG} is not set -- the Container App must name "
             "the pinned @chkp package to run (e.g. @chkp/quantum-management-mcp@1.4.7).")
        return 2

    port = resolve_port(env.get(ENV_REMOTE_HTTP_PORT))
    extra_args = parse_extra_args(env.get(ENV_REMOTE_ARGS))
    creds = _load_creds(env.get(ENV_KEY_VAULT_URI), env.get(ENV_REMOTE_SECRET_NAME))

    child_env = build_server_env(env, creds, port)
    cmd = build_server_command(package, port, extra_args)
    _log(f"starting {package} on :{port}/mcp (http transport"
         + (f", {len(creds)} cred key(s) from Key Vault" if creds else
            ", no Key Vault creds -- placeholders")
         + ")")
    os.execvpe(cmd[0], cmd, child_env)
    return 0  # unreachable when exec succeeds


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
