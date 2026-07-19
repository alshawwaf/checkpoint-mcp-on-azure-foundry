"""Remote MCP tier orchestration (`deploy --remote-mcp` / destroy) -- the Azure
analogue of provisioning the AWS AgentCore Gateway + its runtimes + Cognito.

Driven IMPERATIVELY by the CLI (like the hosted agent in hosting.py), because
it runs AFTER the agent image is built and needs an Entra app registration
whose client id only exists once created -- neither expressible in the azd
`azure.yaml` provision:

  [1] Entra app registration for the Easy Auth audience (find-before-create,
      identifier-uri api://<appId>, v2 access tokens so the issuer matches).
  [2] az deployment group create of infra/modules/remote-mcp.bicep -- the
      shared identity + AcrPull/KV-Secrets-User grants, the Container Apps
      environment, and one scale-to-zero Container App per selected server.
  [3] Build the {server,url} endpoint catalog from the deployment outputs and
      persist it (CHKP_REMOTE_MCP) + the audience (CHKP_REMOTE_AUDIENCE) into
      the azd env so the remote MCP client + status read them back.
  [4] Best-effort Foundry project Toolbox registration so PORTAL agents can use
      the same endpoints; a preview-SDK gap degrades to a clear portal note.

Never writes credentials anywhere: the Container Apps read each server's Key
Vault secret at boot via the shared managed identity.
"""

from __future__ import annotations

import json
import os
import tempfile

from . import config
from .azutil import REPO_ROOT, AzCliError, log, run, stream

DEPLOYMENT_NAME = "chkp-remote-mcp"


def _az_json(args: list[str]):
    """Run an `az ... -o json` command and parse stdout (None on empty)."""
    out = run(["az", *args, "-o", "json"]).stdout
    return json.loads(out) if out and out.strip() else None


def _azd_set(prefix: str, key: str, value: str) -> None:
    run(["azd", "env", "set", key, value, "-e", prefix], cwd=str(REPO_ROOT))


# ---------------------------------------------------------------------------
# 1. Entra app registration (the Easy Auth audience -- analogue of the AWS
#    gateway's Cognito resource server)
# ---------------------------------------------------------------------------

def ensure_app_registration(prefix: str) -> str:
    """Find-or-create the gateway app registration and return its client id.
    Idempotent by display name. Sets identifier-uri api://<appId> and requests
    v2 access tokens so the token issuer matches the Easy Auth v2 issuer."""
    name = config.remote_app_registration_name(prefix)
    found = _az_json(["ad", "app", "list", "--display-name", name,
                      "--query", "[].appId"]) or []
    if found:
        app_id = str(found[0])
        log(f"  Entra app registration '{name}' exists ({app_id})")
    else:
        created = _az_json(["ad", "app", "create", "--display-name", name,
                            "--sign-in-audience", "AzureADMyOrg"])
        app_id = str(created["appId"])
        log(f"  Entra app registration '{name}' created ({app_id})")
    # Set the api://<appId> identifier-uri on its own -- a token can then be
    # requested for this audience (the Easy Auth resource).
    run(["az", "ad", "app", "update", "--id", app_id,
         "--identifier-uris", f"api://{app_id}"])
    # Request v2 access tokens so the token issuer matches the Easy Auth v2
    # issuer. A freshly-created app has no populated `api` node, so the generic
    # `--set api.requestedAccessTokenVersion=2` fails ("Couldn't find 'api'");
    # PATCH the Graph object directly by its object id instead. Best-effort: a
    # Graph hiccup must not fail the whole tier (the identifier-uri above is the
    # critical part) -- surface a manual fallback and continue.
    try:
        obj_id = run(["az", "ad", "app", "show", "--id", app_id,
                      "--query", "id", "-o", "tsv"]).stdout.strip()
        if obj_id:
            run(["az", "rest", "--method", "PATCH",
                 "--url", f"https://graph.microsoft.com/v1.0/applications/{obj_id}",
                 "--headers", "Content-Type=application/json",
                 "--body", '{"api":{"requestedAccessTokenVersion":2}}'])
    except AzCliError as e:
        log(f"  note: could not set requestedAccessTokenVersion=2 on '{name}' "
            f"({str(e)[:100]}). If remote MCP auth 401s, set it once in the "
            f"portal (App registrations → {name} → Manifest → requestedAccessTokenVersion: 2).")
    return app_id


# ---------------------------------------------------------------------------
# 2-3. Deploy the Bicep module + read back the endpoint catalog
# ---------------------------------------------------------------------------

def _resolve_log_analytics(rg: str, prefix: str) -> str:
    """The stack's Log Analytics workspace name (log-<prefix>-<token>), read from
    the RG so no extra Bicep output is needed. Prefers the stack-prefixed one."""
    names = _az_json(["resource", "list", "-g", rg,
                      "--resource-type", "Microsoft.OperationalInsights/workspaces",
                      "--query", "[].name"]) or []
    for n in names:
        if str(n).startswith(f"log-{prefix}-"):
            return str(n)
    if names:
        return str(names[0])
    raise RuntimeError(
        f"no Log Analytics workspace in {rg} -- deploy the base stack first "
        "(python3 -m chkpmcpaz deploy).")


def deployment_parameters(cfg, env, app_id: str, la_name: str, image: str,
                          descriptors: list[dict]) -> dict:
    """Assemble the ARM parameters object for remote-mcp.bicep. Pure (no Azure
    calls) so the plumbing is unit-testable."""
    values = {
        "location": cfg.location,
        "tags": {"project": config.TAGS["project"], "stack": cfg.prefix},
        "logAnalyticsName": la_name,
        "registryName": env["AZURE_CONTAINER_REGISTRY_NAME"],
        "registryLoginServer": env["AZURE_CONTAINER_REGISTRY_ENDPOINT"],
        "agentImage": image,
        "keyVaultName": env["KEY_VAULT_NAME"],
        "keyVaultUri": env["KEY_VAULT_URI"],
        "containerEnvName": config.container_env_name(cfg.prefix),
        "identityName": config.remote_identity_name(cfg.prefix),
        "audienceClientId": app_id,
        "servers": descriptors,
    }
    return {
        "$schema": ("https://schema.management.azure.com/schemas/2019-04-01/"
                    "deploymentParameters.json#"),
        "contentVersion": "1.0.0.0",
        "parameters": {k: {"value": v} for k, v in values.items()},
    }


def endpoints_from_outputs(outputs) -> list[dict]:
    """Turn the Bicep `endpoints` output ([{server,appName,fqdn}]) into the
    persisted {server,url} catalog. Pure and tolerant of a missing output."""
    raw = ((outputs or {}).get("endpoints") or {}).get("value") or []
    catalog = []
    for e in raw:
        fqdn = e.get("fqdn")
        if e.get("server") and fqdn:
            catalog.append({"server": str(e["server"]),
                            "url": config.remote_endpoint_url(str(fqdn))})
    return catalog


def _deploy_group(rg: str, params: dict) -> dict:
    """az deployment group create with an ARM params file (arrays/objects via a
    file avoid CLI quoting hazards). Streams progress, then reads the outputs."""
    fd, path = tempfile.mkstemp(suffix=".json", prefix="chkp-remote-mcp-")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(params, fh)
        stream(["az", "deployment", "group", "create", "-g", rg, "-n", DEPLOYMENT_NAME,
                "-f", "infra/modules/remote-mcp.bicep", "-p", f"@{path}"],
               cwd=str(REPO_ROOT))
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    shown = run(["az", "deployment", "group", "show", "-g", rg, "-n", DEPLOYMENT_NAME,
                 "--query", "properties.outputs", "-o", "json"]).stdout
    return json.loads(shown) if shown and shown.strip() else {}


# ---------------------------------------------------------------------------
# 4. Foundry Toolbox registration (best-effort; portal fallback)
# ---------------------------------------------------------------------------

def register_toolbox(cfg, env, catalog: list[dict]) -> list[str]:
    """Best-effort: surface the endpoints for the Foundry project Toolbox so
    PORTAL agents (and other MCP clients) can use them too. The azure-ai-projects
    Toolbox/connections surface is preview and version-fluid, so rather than
    guess a payload schema, this reports the live endpoints and how to add them.
    The endpoints are already usable by any Entra-authenticated MCP client."""
    if not catalog:
        return []
    notes = ["Foundry Toolbox: the endpoints below are live and Entra-authenticated.",
             "  Add them to the project Toolbox in the portal (or via `az`) so",
             "  portal-built agents can share the same tools:"]
    for ep in catalog:
        notes.append(f"    {ep['server']}: {ep['url']}")
    return notes


# ---------------------------------------------------------------------------
# provision / teardown / status
# ---------------------------------------------------------------------------

def provision(cfg, env, *, agent_image: str | None = None, servers=None) -> dict:
    """Stand up the remote MCP tier. Returns
    {catalog, audience, app_id, notes, failures}. Raises only on a hard
    misconfiguration; per-endpoint issues surface as live Container App state."""
    prefix = cfg.prefix
    rg = env.get("AZURE_RESOURCE_GROUP") or cfg.resource_group()
    servers = list(servers or cfg.servers)
    descriptors = config.remote_server_descriptors(servers, prefix)
    image = agent_image or config.image_ref(
        env["AZURE_CONTAINER_REGISTRY_ENDPOINT"], digest=env.get("IMAGE_DIGEST"))

    app_id = ensure_app_registration(prefix)
    la_name = _resolve_log_analytics(rg, prefix)
    params = deployment_parameters(cfg, env, app_id, la_name, image, descriptors)
    log(f"  az deployment group create -> {len(descriptors)} Container App(s) "
        f"(scale-to-zero, Easy Auth) in {rg}")
    outputs = _deploy_group(rg, params)
    catalog = endpoints_from_outputs(outputs)
    audience = config.remote_audience(app_id)

    _azd_set(prefix, config.ENV_REMOTE_ENDPOINTS, json.dumps(catalog))
    _azd_set(prefix, config.ENV_REMOTE_AUDIENCE, audience)

    failures = []
    if not catalog:
        failures.append("remote-mcp deployment returned no endpoints "
                        "(check the deployment in the portal)")
    notes = register_toolbox(cfg, env, catalog)
    return {"catalog": catalog, "audience": audience, "app_id": app_id,
            "notes": notes, "failures": failures}


def teardown(cfg, env) -> list[str]:
    """Remove the tier's out-of-RG artifact -- the Entra app registration -- and
    clear the persisted azd env vars. The Container Apps, environment, and
    identity live in the stack RG and are removed by `azd down` in destroy.
    Idempotent: an already-absent registration is fine."""
    notes = []
    name = config.remote_app_registration_name(cfg.prefix)
    try:
        ids = _az_json(["ad", "app", "list", "--display-name", name,
                        "--query", "[].appId"]) or []
        for app_id in ids:
            run(["az", "ad", "app", "delete", "--id", str(app_id)])
            notes.append(f"deleted Entra app registration {name} ({app_id})")
        if not ids:
            notes.append(f"no Entra app registration {name} to delete")
    except AzCliError as e:
        notes.append(f"could not delete app registration {name} ({str(e)[:120]})")
    for key in (config.ENV_REMOTE_ENDPOINTS, config.ENV_REMOTE_AUDIENCE):
        try:
            _azd_set(cfg.prefix, key, "")
        except AzCliError:
            pass
    return notes


def remote_status(env) -> dict | None:
    """Read-only view of the deployed remote tier from the persisted azd env:
    {audience, endpoints:[{server,url}]}, or None when the tier is absent."""
    from .mcp_remote import parse_endpoints

    catalog = parse_endpoints(env.get(config.ENV_REMOTE_ENDPOINTS))
    if not catalog:
        return None
    return {"audience": env.get(config.ENV_REMOTE_AUDIENCE) or "", "endpoints": catalog}
