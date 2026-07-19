"""Shared Azure plumbing: az/azd subprocess wrappers, `azd env get-values`
parsing, a cached DefaultAzureCredential, and the log() sink the UI reporter
captures. Mirrors chkpmcpaws/awsutil.py in spirit -- but where the AWS repo talks
to boto3 clients, this repo shells out to the az/azd CLIs for control-plane
work (Bicep owns the ARM resources) and uses azure-identity for data-plane
tokens.

Security invariants: never shell=True (commands are argv lists, so no user
input is ever shell-interpreted); secret VALUES never pass through log(); TLS
verification is never touched here (the SDKs verify by default).
"""

import json
import pathlib
import shutil
import subprocess

# Repo root (azure.yaml lives here). azd/az commands that need the project
# context run with cwd=REPO_ROOT so the CLI works from any working directory.
REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

_LOG_SINK = None


def set_log_sink(fn):
    """Route log() through a UI reporter (chkpmcpaz.ui). None restores print."""
    global _LOG_SINK
    _LOG_SINK = fn


def has_log_sink():
    """True when log() is routed through a UI reporter (deploy/destroy/status
    full-screen mode)."""
    return _LOG_SINK is not None


def log(msg=""):
    if _LOG_SINK is not None:
        _LOG_SINK(msg)
    else:
        print(msg, flush=True)


class AzCliError(RuntimeError):
    """An az/azd subprocess exited non-zero. Carries the command name, return
    code, and a stderr tail so cli._is_credential_error can classify expired
    Entra/az-login failures without ever showing a raw traceback. Only stderr
    TEXT travels here -- callers never put secret values on a command line."""

    def __init__(self, cmd_name, returncode, stderr_tail=""):
        self.cmd_name = cmd_name
        self.returncode = returncode
        self.stderr_tail = (stderr_tail or "").strip()
        msg = f"{cmd_name} exited {returncode}"
        if self.stderr_tail:
            msg += f": {self.stderr_tail}"
        super().__init__(msg)


def have(cmd):
    """True if `cmd` is on PATH (doctor/status preflight)."""
    return shutil.which(cmd) is not None


def run(cmd, *, capture=True, check=True, env=None, timeout=None, cwd=None):
    """Run an az/azd/node command (argv list -- NEVER shell=True) and return
    the CompletedProcess. On a non-zero exit with check=True, raises
    AzCliError(cmd_name, returncode, stderr_tail) -- the tail is capped so a
    page of ARM error JSON doesn't flood the re-auth classifier."""
    if not cmd or not isinstance(cmd, (list, tuple)):
        raise ValueError("run() takes a non-empty argv list")
    cp = subprocess.run(
        list(cmd),
        capture_output=capture,
        text=True,
        env=env,
        timeout=timeout,
        cwd=cwd,
        check=False,
    )
    if check and cp.returncode != 0:
        tail = (cp.stderr or cp.stdout or "")[-800:] if capture else ""
        raise AzCliError(cmd[0], cp.returncode, tail)
    return cp


def stream(cmd, *, env=None, cwd=None, check=True):
    """Run a long command (azd provision, az acr build) streaming each output
    line through log() so the live UI tail and the transcript log both see it.
    Returns the exit code; raises AzCliError (with the last lines as the tail)
    on failure when check=True."""
    proc = subprocess.Popen(
        list(cmd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,  # interleave: azd writes progress to stderr
        text=True,
        env=env,
        cwd=cwd,
    )
    tail = []
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip("\n")
        if line.strip():
            log(f"  {line}")
            tail.append(line)
            tail = tail[-8:]
    rc = proc.wait()
    if check and rc != 0:
        raise AzCliError(cmd[0], rc, "\n".join(tail)[-800:])
    return rc


def parse_env_values(text):
    """Parse `azd env get-values` output (KEY="VALUE" lines) into a dict.

    Pure (unit-tested): handles values containing '=', empty values (KEY=""),
    unquoted values, and escaped quotes inside quoted values; blank lines and
    comments are skipped."""
    values = {}
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == '"' and val[-1] == '"':
            val = val[1:-1].replace('\\"', '"')
        if key:
            values[key] = val
    return values


def azd_env_values(prefix):
    """Read the azd environment's values (Bicep outputs included) as a dict.
    Returns {} when the environment does not exist yet -- callers treat that
    as "not deployed", never as an error."""
    try:
        cp = run(["azd", "env", "get-values", "-e", prefix], cwd=str(REPO_ROOT))
    except (AzCliError, FileNotFoundError):
        return {}
    return parse_env_values(cp.stdout)


def azd_env_exists(prefix):
    """True if the azd environment `prefix` already exists (find-before-create
    -- `azd env new` errors on an existing name)."""
    try:
        cp = run(["azd", "env", "list", "--output", "json"], cwd=str(REPO_ROOT))
        entries = json.loads(cp.stdout or "[]")
    except (AzCliError, FileNotFoundError, ValueError):
        return False
    return any(e.get("Name") == prefix for e in entries if isinstance(e, dict))


def hydrate_config(cfg, env):
    """Overlay `azd env get-values` outputs onto a flag-built StackConfig.
    Flags win where they were given (subscription/model); azd outputs fill the
    endpoint fields that only exist after `azd provision`."""
    import dataclasses

    if not env:
        return cfg
    return dataclasses.replace(
        cfg,
        location=env.get("AZURE_LOCATION") or cfg.location,
        subscription_id=cfg.subscription_id or env.get("AZURE_SUBSCRIPTION_ID") or None,
        project_endpoint=env.get("FOUNDRY_PROJECT_ENDPOINT") or cfg.project_endpoint,
        claude_base_url=env.get("CLAUDE_BASE_URL") or cfg.claude_base_url,
        claude_deployment=cfg.claude_deployment or env.get("CLAUDE_MODEL_DEPLOYMENT") or None,
        openai_base_url=env.get("OPENAI_BASE_URL") or cfg.openai_base_url,
        openai_deployment=cfg.openai_deployment or env.get("OPENAI_MODEL_DEPLOYMENT") or None,
        provider=cfg.provider,   # preserve the provider the CLI already resolved
        key_vault_uri=env.get("KEY_VAULT_URI") or cfg.key_vault_uri,
        content_safety_endpoint=env.get("CONTENT_SAFETY_ENDPOINT") or cfg.content_safety_endpoint,
    )


_CREDENTIAL = None


def get_credential():
    """Cached DefaultAzureCredential. Locally it resolves to the az-login
    identity; inside the hosted container to the per-agent Entra identity.
    Lazy so importing this module never requires azure-identity at parse time."""
    global _CREDENTIAL
    if _CREDENTIAL is None:
        from azure.identity import DefaultAzureCredential

        _CREDENTIAL = DefaultAzureCredential()
    return _CREDENTIAL
