"""Local credentials workflow.

Keep Check Point credentials in a gitignored .env-style file -- one
``[server]`` section per server, plain ``KEY=VALUE`` lines -- then apply them
to the per-server Key Vault secrets and refresh the hosted agent so its
sandboxes re-read them:

    python3 -m chkpmcpaz creds template   # write a starter file for your servers
    # edit chkp-credentials.env with real values
    python3 -m chkpmcpaz creds apply      # write the secrets + refresh the agent

Each ``[section]`` is a SERVER name; its ``KEY=VALUE`` lines become that
server's own ``<prefix>-<server>`` Key Vault secret verbatim (JSON body). Two
servers can therefore hold entirely different credentials. Values are NEVER
printed or committed (the file is gitignored; only server and key NAMES are
ever logged). The special ``[quantum-gaia]`` section is AGENT-side: it answers
the Gaia server's interactive elicitation login and is never injected into a
child process environment wholesale.
"""

import configparser
import io
import os

from . import config
from .azutil import azd_env_values, hydrate_config, log
from .config import CRED_SHAPE, SERVERS

DEFAULT_CREDS_FILE = "chkp-credentials.env"

_TEMPLATE_HEADER = (
    "# Check Point credentials for chkpmcpaz -- gitignored, NEVER commit.\n"
    "# One [section] per SERVER; each KEY=VALUE becomes that server's own\n"
    "# <prefix>-<server> Key Vault secret. Fill in real values, then run:\n"
    "#   python3 -m chkpmcpaz creds apply\n"
)


def _parse_ini(text):
    """Parse .env / INI text into {server: {KEY: value}}. Case-preserving;
    no % interpolation (API keys may contain % or =). Sections that map to no
    catalog server are skipped with a printed reason (CONTRACT 6.7 pins this
    on the parser itself, so direct callers never see an unknown server)."""
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str  # preserve KEY case (env vars are case-sensitive)
    try:
        parser.read_string(text)
    except configparser.MissingSectionHeaderError:
        raise ValueError(
            "each server's credentials need a [section] header, e.g. "
            "[quantum-management] above its KEY=VALUE lines"
        )
    except configparser.Error as e:
        raise ValueError(str(e))
    doc = {}
    for section in parser.sections():
        if section not in SERVERS:
            log(f"  skipping '{section}': not a Check Point MCP server "
                f"(known: {', '.join(SERVERS)})")
            continue
        doc[section] = dict(parser[section])
    return doc


def parse_creds_text(text):
    """Public entry for tests: .env text -> {server: {KEY: value}}."""
    return _parse_ini(text)


def parse_creds_file(path):
    """Read a creds file into {server: {KEY: value}}. Raises OSError on a
    missing/unreadable file, ValueError on unparseable content."""
    with open(path, encoding="utf-8") as fh:
        return _parse_ini(fh.read())


def _has_placeholder(body):
    """True if any value is an unedited placeholder."""
    return any(str(v).startswith("PLACEHOLDER") or str(v).startswith("replace-with")
               for v in body.values())


def _shape_for(server):
    """The credential shape (env-var keys) a server's secret holds, or None
    for credential-free servers. quantum-gaia maps to its AGENT-side shape."""
    spec = SERVERS.get(server)
    if spec is None:
        return None
    if spec.creds:
        return CRED_SHAPE[spec.creds]
    if spec.agent_creds:
        return CRED_SHAPE[spec.agent_creds]
    return None


def write_template(path, servers):
    """Write a starter INI at `path`: one [server] section (with that server's
    CRED_SHAPE placeholder keys) per credentialed server in `servers`, plus
    the agent-side [quantum-gaia] section. Refuses to overwrite -- raises
    FileExistsError (the existing file may hold real credentials)."""
    if os.path.exists(path):
        raise FileExistsError(path)
    buf = io.StringIO()
    buf.write(_TEMPLATE_HEADER)
    written = []
    for s in servers:
        if s == "quantum-gaia":
            continue  # always appended below, deployed or not
        shape = _shape_for(s)
        if not shape:
            continue
        buf.write(f"\n[{s}]\n")
        for k, v in shape.items():
            buf.write(f"{k}={v}\n")
        written.append(s)
    buf.write("\n[quantum-gaia]\n")
    for k, v in CRED_SHAPE["gaia"].items():
        buf.write(f"{k}={v}\n")
    written.append("quantum-gaia")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(buf.getvalue())
    return written


def apply_file(cfg, path, *, refresh=True):
    """Apply the creds file to the per-server Key Vault secrets, skipping
    unknown/empty/still-placeholder sections with printed reasons, then
    refresh the hosted agent so sandboxes re-read them. Values never printed.
    Returns 0 on success, 1 when nothing valid could be applied.

    refresh=False writes the secrets but SKIPS the hosted-agent refresh -- used
    by `deploy --creds`, which applies secrets BEFORE it builds the image, and
    then rolls out the agent itself (deploy_hosted_agent(roll=True)) once the
    image exists. Refreshing here would mint a version pinned to a not-yet-built
    image and fail (ImageError)."""
    if not os.path.exists(path):
        log(f"No creds file at {path}. Create one first: python3 -m chkpmcpaz creds template")
        return 1
    try:
        doc = parse_creds_file(path)
    except (OSError, ValueError) as e:
        log(f"Could not read {path}: {e}")
        return 1

    if not cfg.key_vault_uri:
        cfg = hydrate_config(cfg, azd_env_values(cfg.prefix))
    if not cfg.key_vault_uri:
        log(f"No Key Vault found for prefix '{cfg.prefix}' -- deploy first: "
            "python3 -m chkpmcpaz deploy")
        return 1
    from . import keyvault  # runtime-owned; imported once the vault is known

    applied, skipped = [], []
    for server, body in doc.items():
        if _shape_for(server) is None:
            known = ", ".join(s for s in SERVERS if _shape_for(s))
            log(f"  skipping '{server}': not a credentialed server (known: {known})")
            skipped.append(server)
            continue
        if not isinstance(body, dict) or not body:
            log(f"  skipping '{server}': section has no KEY=VALUE lines")
            skipped.append(server)
            continue
        # Guard against applying an unedited template.
        if _has_placeholder(body):
            log(f"  '{server}' still has placeholder values -- edit them before applying; skipping")
            skipped.append(server)
            continue
        name = cfg.secret_name(server)
        keyvault.set_secret_json(cfg.key_vault_uri, name, body)
        log(f"  {server} -> {name}  ({len(body)} key(s) set; values not printed)")
        applied.append(server)

    if not applied:
        log("No servers applied (nothing valid in the file). Nothing to refresh.")
        return 1 if skipped else 0

    if not refresh:
        log(f"\nApplied {len(applied)} secret(s): {', '.join(applied)}. "
            "(hosted-agent rollout deferred to the caller)")
        return 0
    log(f"\nApplied {len(applied)} secret(s): {', '.join(applied)}. "
        "Refreshing the hosted agent so it re-reads them...")
    # The local runtime re-reads Key Vault on every run; only the hosted
    # agent's sandboxes cache creds from boot and need the version bump.
    from . import hosting

    return hosting.run_refresh(cfg)


# ---------------------------------------------------------------------------
# CLI entry points
# ---------------------------------------------------------------------------

def _deployed_servers(cfg):
    """Servers whose Key Vault secrets actually exist (so the template matches
    reality). Falls back to the configured server set when the vault is not
    reachable/deployed yet."""
    hydrated = hydrate_config(cfg, azd_env_values(cfg.prefix))
    if hydrated.key_vault_uri:
        try:
            from . import keyvault

            names = keyvault.list_stack_secrets(hydrated.key_vault_uri, cfg.prefix)
            marker = f"{cfg.prefix}-"
            servers = [n[len(marker):] for n in names if n.startswith(marker)]
            servers = [s for s in servers if s in SERVERS]
            if servers:
                return servers
        except Exception as e:  # noqa: BLE001 -- template still works offline
            from .cli import _is_credential_error

            if _is_credential_error(e):
                raise
            log(f"  (could not list Key Vault secrets: {type(e).__name__} -- "
                "using the configured server set)")
    return list(cfg.servers)


def run_template(cfg, path=None):
    path = path or DEFAULT_CREDS_FILE
    servers = _deployed_servers(cfg)
    try:
        written = write_template(path, servers)
    except FileExistsError:
        log(f"{path} already exists -- refusing to overwrite (it may hold real creds).")
        log("Edit it, or pass --file <path> for a different location.")
        return 1
    log(f"Wrote {path} with {len(written)} server section(s): {', '.join(written)}")
    log("Edit the values (it is gitignored), then: python3 -m chkpmcpaz creds apply")
    return 0


def run_apply(cfg, path=None):
    return apply_file(cfg, path or DEFAULT_CREDS_FILE)


# Re-export for callers that want the constant without importing config.
PLACEHOLDER_VALUE = config.PLACEHOLDER_VALUE
