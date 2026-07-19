"""HTTPS bridge for the hosted agent -- `chkpmcpaz bridge {provision,show,destroy}`.

The Foundry Hosted Agent's Responses endpoint is already an authenticated HTTPS
front door, but it demands an ENTRA ID OAuth2 token (minted via
DefaultAzureCredential on the ai.azure.com scope) plus a Foundry data-plane
role. A non-Azure caller -- Microsoft Teams via Power Automate, n8n, a webhook,
a curl script on a laptop -- cannot mint that; it would have to `az login` or
carry a service principal. This bridge removes exactly that friction, the same
way the AWS repo's Lambda + API Gateway bridge did:

    POST {url}/api/invoke   Authorization: Bearer <token>   {"prompt": "..."}

It is one Azure Function (Consumption, Linux/Python). The handler verifies the
bearer token (constant-time) against a value stored ONLY in Key Vault (never in
code, config, or logs -- org policy), then invokes the hosted agent with the
Function's own managed identity and returns {"result","usage","model","error"}.
Rotating the token = write a new value to the Key Vault secret; the handler
re-reads it within TOKEN_TTL seconds.

Design/parity notes:
  * A Function App gives HTTPS + a stable hostname on its own, so unlike AWS
    there is no separate API Gateway to stand up.
  * Auth (org policy: every endpoint authenticated): the FIRST thing the
    handler does is the constant-time token check; no token -> 401. TLS is
    always on (Functions serve HTTPS; the handler never disables verification).
  * LIVE-DERIVED: built against the documented az/Functions/azure-ai-projects
    surface but validate the exact agent-invoke RBAC on first run (see the
    grant comments) -- mirrors the AWS bridge's live-derived posture.
"""

import hashlib
import io
import json
import os
import secrets as pysecrets
import tempfile
import zipfile

from .azutil import AzCliError, log, run
from .config import AI_SCOPE

# The Function reads the bearer token from Key Vault at most this often (the
# rotation window): write a new secret value and the handler picks it up.
TOKEN_TTL = 300


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested -- no Azure/network)
# ---------------------------------------------------------------------------

def _suffix(cfg, env):
    """A short, deterministic suffix for globally-unique resource names, from
    the subscription id + prefix (stable across re-runs so provision is
    idempotent). Falls back to the prefix alone when no subscription is known."""
    sub = env.get("AZURE_SUBSCRIPTION_ID") or cfg.subscription_id or ""
    return hashlib.sha1(f"{sub}:{cfg.prefix}".encode()).hexdigest()[:8]


def bridge_names(cfg, env):
    """Deterministic resource names for the bridge. Function app + storage names
    must be globally unique and charset-limited; both fold in _suffix."""
    suf = _suffix(cfg, env)
    prefix_alnum = cfg.prefix.replace("-", "")
    return {
        "function_app": f"func-{cfg.prefix}-bridge-{suf}"[:60],
        # storage: 3-24 lowercase alphanumeric
        "storage": f"st{prefix_alnum}br{suf}"[:24].lower(),
        "token_secret": f"{cfg.prefix}-bridge-token",
        "url": f"https://func-{cfg.prefix}-bridge-{suf}.azurewebsites.net/api/invoke"[:200],
    }


def new_token():
    """A fresh URL-safe bearer token (256 bits). Never stored anywhere but Key
    Vault."""
    return pysecrets.token_urlsafe(32)


def session_id(session):
    """Sanitize an optional caller session id to a safe conversation id."""
    base = "chkpmcp-bridge-" + (session or "default")
    return "".join(c if (c.isalnum() or c in "-_") else "-" for c in base)[:100]


# The Azure Function handler (v2 programming model). Self-contained: stdlib +
# azure-functions/identity/keyvault/ai-projects (installed via requirements at
# deploy). Kept as a string so provisioning needs no separate build tree. The
# token is read from Key Vault -- NEVER embedded here.
HANDLER_PY = r'''"""chkpmcpaz agent bridge -- bearer-token HTTPS front for the hosted agent."""
import hmac
import json
import os
import time

import azure.functions as func
from azure.identity import DefaultAzureCredential
from azure.keyvault.secrets import SecretClient

app = func.FunctionApp()

KEY_VAULT_URI = os.environ["KEY_VAULT_URI"]
TOKEN_SECRET = os.environ["BRIDGE_TOKEN_SECRET"]
PROJECT_ENDPOINT = os.environ["FOUNDRY_PROJECT_ENDPOINT"]
AGENT_NAME = os.environ["AGENT_NAME"]
TOKEN_TTL = int(os.environ.get("BRIDGE_TOKEN_TTL", "300"))

_cred = DefaultAzureCredential()
_token = {"value": None, "at": 0.0}


def _bearer_token():
    now = time.time()
    if _token["value"] is None or now - _token["at"] > TOKEN_TTL:
        sc = SecretClient(vault_url=KEY_VAULT_URI, credential=_cred)
        raw = sc.get_secret(TOKEN_SECRET).value or "{}"
        try:
            _token["value"] = json.loads(raw).get("token")
        except ValueError:
            _token["value"] = raw
        _token["at"] = now
    return _token["value"]


def _session_id(session):
    base = "chkpmcp-bridge-" + (session or "default")
    return "".join(c if (c.isalnum() or c in "-_") else "-" for c in base)[:100]


def _extract_text(resp):
    text = getattr(resp, "output_text", None)
    if text:
        return str(text)
    parts = []
    for item in (getattr(resp, "output", None) or []):
        for content in (getattr(item, "content", None) or []):
            t = getattr(content, "text", None)
            if t:
                parts.append(str(t))
    return "\n".join(parts)


@app.route(route="invoke", methods=["POST"], auth_level=func.AuthLevel.ANONYMOUS)
def invoke(req: func.HttpRequest) -> func.HttpResponse:
    # Auth first: constant-time compare of the bearer token (org policy --
    # every endpoint authenticated). Accept Authorization: Bearer or X-Bridge-Token.
    token = _bearer_token() or ""
    auth = req.headers.get("Authorization") or ""
    candidates = [req.headers.get("X-Bridge-Token") or ""]
    if auth.startswith("Bearer "):
        candidates.append(auth[7:])
    if not token or not any(c and hmac.compare_digest(c, token) for c in candidates):
        return func.HttpResponse(
            json.dumps({"error": "unauthorized (send Authorization: Bearer <token>)"}),
            status_code=401, mimetype="application/json")

    try:
        payload = req.get_json()
    except ValueError:
        return func.HttpResponse(json.dumps({"error": "body must be JSON"}),
                                 status_code=400, mimetype="application/json")
    prompt = ""
    for key in ("prompt", "task", "text", "question"):
        v = payload.get(key)
        if isinstance(v, str) and v.strip():
            prompt = v.strip()
            break
    if not prompt:
        return func.HttpResponse(
            json.dumps({"error": "no prompt in body (use {\"prompt\": \"...\"})"}),
            status_code=400, mimetype="application/json")

    try:
        from azure.ai.projects import AIProjectClient
        project = AIProjectClient(endpoint=PROJECT_ENDPOINT, credential=_cred,
                                  allow_preview=True)
        client = project.get_openai_client(agent_name=AGENT_NAME)
        kwargs = {"input": prompt}
        if payload.get("session"):
            kwargs["metadata"] = {"session": _session_id(str(payload["session"]))}
        resp = client.responses.create(**kwargs)
    except Exception as e:  # noqa: BLE001 -- surface a clean 502, never a stack
        return func.HttpResponse(
            json.dumps({"error": f"agent invoke failed: {type(e).__name__}"}),
            status_code=502, mimetype="application/json")

    text = _extract_text(resp).strip()
    error = text.startswith("ERROR: ")
    if error:
        text = text[len("ERROR: "):]
    return func.HttpResponse(
        json.dumps({"result": text, "model": AGENT_NAME, "error": error}),
        status_code=200, mimetype="application/json")
'''

REQUIREMENTS_TXT = (
    "azure-functions\n"
    "azure-identity>=1.19\n"
    "azure-keyvault-secrets>=4.8\n"
    "azure-ai-projects>=2.1.0\n"
)

HOST_JSON = json.dumps({
    "version": "2.0",
    "extensionBundle": {
        "id": "Microsoft.Azure.Functions.ExtensionBundle",
        "version": "[4.*, 5.0.0)",
    },
}, indent=2)


def _zip_bytes():
    """A deployable function package: function_app.py + requirements.txt +
    host.json (remote Oryx build installs the requirements)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("function_app.py", HANDLER_PY)
        zf.writestr("requirements.txt", REQUIREMENTS_TXT)
        zf.writestr("host.json", HOST_JSON)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Provision / show / destroy (imperative az, mirroring hosting.py)
# ---------------------------------------------------------------------------

def _require_stack(env):
    for key in ("FOUNDRY_PROJECT_ENDPOINT", "KEY_VAULT_URI", "KEY_VAULT_NAME",
                "AZURE_RESOURCE_GROUP", "FOUNDRY_ACCOUNT_NAME"):
        if not env.get(key):
            return key
    return None


def provision(cfg, env):
    """Create/refresh the bridge: storage account, Function app (system
    identity), RBAC, the token secret, and the deployed handler. Idempotent."""
    missing = _require_stack(env)
    if missing:
        log(f"No deployed stack for prefix '{cfg.prefix}' (missing {missing}).")
        log("Deploy first: python3 -m chkpmcpaz deploy")
        return 1

    # The hosted agent must exist -- the bridge just fronts it.
    from . import hosting

    try:
        status = hosting.hosted_agent_status(cfg, env)
    except hosting.MissingExtraError as e:
        log(str(e))
        return 1
    if status is None:
        log(f"No hosted agent '{cfg.agent_name()}' found -- deploy the agent first:")
        log("  python3 -m chkpmcpaz deploy")
        return 1

    names = bridge_names(cfg, env)
    rg = env["AZURE_RESOURCE_GROUP"]
    location = cfg.location or env.get("AZURE_LOCATION") or "eastus2"
    storage, app_name = names["storage"], names["function_app"]

    # --- storage account (required by Functions) -----------------------------
    log(f"[storage] {storage}")
    try:
        run(["az", "storage", "account", "create", "-n", storage, "-g", rg,
             "-l", location, "--sku", "Standard_LRS", "--kind", "StorageV2",
             "--allow-blob-public-access", "false", "--min-tls-version", "TLS1_2",
             "-o", "none"])
    except AzCliError as e:
        _reraise_credential(e)
        if "already" not in str(e).lower() and "exists" not in str(e).lower():
            raise

    # --- Function app (Consumption, Linux, Python, system identity) ----------
    log(f"[function] {app_name}")
    try:
        run(["az", "functionapp", "create", "-n", app_name, "-g", rg,
             "--storage-account", storage,
             "--consumption-plan-location", location,
             "--os-type", "Linux", "--runtime", "python",
             "--runtime-version", "3.11", "--functions-version", "4",
             "--assign-identity", "[system]", "-o", "none"])
    except AzCliError as e:
        _reraise_credential(e)
        if "already" not in str(e).lower() and "exists" not in str(e).lower():
            raise

    # --- token secret (create once; keep the existing token on re-runs) -------
    from . import keyvault

    vault_uri = env["KEY_VAULT_URI"]
    existing = None
    try:
        existing = keyvault.get_secret_json(vault_uri, names["token_secret"])
    except Exception as e:  # noqa: BLE001
        _reraise_credential(e)
    if existing and existing.get("token"):
        log(f"[secret] {names['token_secret']} exists -- token unchanged")
    else:
        keyvault.set_secret_json(vault_uri, names["token_secret"], {"token": new_token()})
        log(f"[secret] {names['token_secret']} created (holds the bearer token)")

    # --- app settings --------------------------------------------------------
    settings = {
        "KEY_VAULT_URI": vault_uri,
        "BRIDGE_TOKEN_SECRET": names["token_secret"],
        "FOUNDRY_PROJECT_ENDPOINT": env["FOUNDRY_PROJECT_ENDPOINT"],
        "AGENT_NAME": cfg.agent_name(),
        "BRIDGE_TOKEN_TTL": str(TOKEN_TTL),
        "SCM_DO_BUILD_DURING_DEPLOYMENT": "true",
        "ENABLE_ORYX_BUILD": "true",
    }
    run(["az", "functionapp", "config", "appsettings", "set", "-n", app_name, "-g", rg,
         "--settings", *[f"{k}={v}" for k, v in settings.items()], "-o", "none"])

    # --- RBAC for the Function's managed identity ----------------------------
    # It reads the token from Key Vault (Key Vault Secrets User) and invokes the
    # hosted agent (Cognitive Services User at ACCOUNT scope -- the same grant
    # the agent identity gets in hosting.grant_agent_identity; if your tenant
    # requires a project data-plane role to invoke, add it the same way).
    try:
        pid = json.loads(run(["az", "functionapp", "identity", "show",
                              "-n", app_name, "-g", rg, "-o", "json"]).stdout).get("principalId")
    except (AzCliError, ValueError) as e:
        _reraise_credential(e)
        pid = None
    if pid:
        sub = (env.get("AZURE_SUBSCRIPTION_ID") or cfg.subscription_id
               or json.loads(run(["az", "account", "show", "-o", "json"]).stdout)["id"])
        account, vault = env["FOUNDRY_ACCOUNT_NAME"], env["KEY_VAULT_NAME"]
        grants = (
            ("Key Vault Secrets User",
             f"/subscriptions/{sub}/resourceGroups/{rg}/providers/"
             f"Microsoft.KeyVault/vaults/{vault}"),
            ("Cognitive Services User",
             f"/subscriptions/{sub}/resourceGroups/{rg}/providers/"
             f"Microsoft.CognitiveServices/accounts/{account}"),
        )
        for role, scope in grants:
            try:
                run(["az", "role", "assignment", "create",
                     "--assignee-object-id", pid,
                     "--assignee-principal-type", "ServicePrincipal",
                     "--role", role, "--scope", scope, "-o", "none"])
                log(f"[rbac] granted '{role}' to the bridge identity")
            except AzCliError as e:
                s = str(e).lower()
                if "exists" in s or "conflict" in s:
                    log(f"[rbac] '{role}' already granted -- skipping")
                else:
                    _reraise_credential(e)
                    raise

    # --- deploy the handler (zip; remote Oryx build installs requirements) ---
    log("[deploy] packaging + zip-deploying the handler...")
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as fh:
        fh.write(_zip_bytes())
        zip_path = fh.name
    try:
        run(["az", "functionapp", "deployment", "source", "config-zip",
             "-n", app_name, "-g", rg, "--src", zip_path, "-o", "none"], timeout=600)
    finally:
        try:
            os.unlink(zip_path)
        except OSError:
            pass

    log("")
    log("Bridge is up. Every call needs the bearer token:")
    _print_usage(cfg, env, names)
    return 0


def _print_usage(cfg, env, names):
    log(f"  URL   : {names['url']}")
    log(f"  Token : Key Vault secret '{names['token_secret']}' (JSON key 'token'):")
    log(f"          az keyvault secret show --vault-name {env['KEY_VAULT_NAME']} "
        f"--name {names['token_secret']} --query value -o tsv")
    log("  curl  : TOKEN=$(az keyvault secret show --vault-name "
        f"{env['KEY_VAULT_NAME']} --name {names['token_secret']} --query value -o tsv "
        "| python3 -c 'import sys,json;print(json.load(sys.stdin)[\"token\"])')")
    log(f"          curl -s -X POST '{names['url']}' -H \"Authorization: Bearer $TOKEN\" "
        "-H 'Content-Type: application/json' "
        "-d '{\"prompt\": \"how many hosts are configured?\"}'")
    log("  Body  : {\"prompt\": \"...\", \"session\": \"optional\"}")
    log("  Header: Authorization: Bearer <token>   (X-Bridge-Token works too)")


def show(cfg, env, reveal=False):
    """Print the bridge URL + usage (and the token itself with --reveal-token)."""
    names = bridge_names(cfg, env)
    rg = env.get("AZURE_RESOURCE_GROUP")
    if not rg:
        log("No deployed stack -- deploy first: python3 -m chkpmcpaz deploy")
        return 1
    try:
        run(["az", "functionapp", "show", "-n", names["function_app"], "-g", rg, "-o", "none"])
    except AzCliError as e:
        _reraise_credential(e)
        log("Bridge not provisioned. Run: python3 -m chkpmcpaz bridge provision")
        return 1
    _print_usage(cfg, env, names)
    if reveal and env.get("KEY_VAULT_URI"):
        from . import keyvault

        try:
            body = keyvault.get_secret_json(env["KEY_VAULT_URI"], names["token_secret"])
            if body and body.get("token"):
                log(f"  Bearer: {body['token']}")
        except Exception as e:  # noqa: BLE001
            _reraise_credential(e)
    return 0


def destroy(cfg, env):
    """Remove the bridge (Function app + storage). The token secret follows the
    vault's soft-delete like every other secret. Idempotent."""
    names = bridge_names(cfg, env)
    rg = env.get("AZURE_RESOURCE_GROUP")
    if not rg:
        log("Nothing to destroy (no deployed stack).")
        return 0
    removed = []
    for kind, name in (("functionapp", names["function_app"]),
                       ("storage account", names["storage"])):
        cmd = (["az", "functionapp", "delete", "-n", name, "-g", rg]
               if kind == "functionapp"
               else ["az", "storage", "account", "delete", "-n", name, "-g", rg, "--yes"])
        try:
            run(cmd + ["-o", "none"])
            removed.append(f"{kind} {name}")
        except AzCliError as e:
            _reraise_credential(e)  # only credential errors bubble up
    if removed:
        log("[bridge] removed: " + ", ".join(removed))
    else:
        log("[bridge] nothing to remove.")
    return 0


def describe(cfg, env):
    """For status/inventory: {'present': bool, 'url': str|None}."""
    names = bridge_names(cfg, env)
    rg = env.get("AZURE_RESOURCE_GROUP")
    if not rg:
        return {"present": False, "url": None}
    try:
        run(["az", "functionapp", "show", "-n", names["function_app"], "-g", rg, "-o", "none"])
    except AzCliError:
        return {"present": False, "url": None}
    return {"present": True, "url": names["url"]}


def run_bridge(cfg, action, *, reveal_token=False):
    """Dispatch `bridge {provision,show,destroy}`. Returns the exit code."""
    from .azutil import azd_env_values, hydrate_config

    env = azd_env_values(cfg.prefix)
    cfg = hydrate_config(cfg, env)
    if action == "provision":
        return provision(cfg, env)
    if action == "show":
        return show(cfg, env, reveal=reveal_token)
    if action == "destroy":
        return destroy(cfg, env)
    log(f"unknown bridge action {action!r}")
    return 2


def _reraise_credential(e):
    from .cli import _is_credential_error

    if _is_credential_error(e):
        raise e


# The AI scope the Function's identity mints tokens on to reach the /anthropic
# route (kept here so the handler and the CLI agree on the scope).
BRIDGE_AI_SCOPE = AI_SCOPE
