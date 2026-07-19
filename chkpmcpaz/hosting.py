"""Foundry Hosted Agent lifecycle: create/version/route, RBAC grants for the
per-agent Entra identity, Responses-endpoint invoke, status, refresh, delete.

This is the Azure analogue of the AWS repo's chkpmcpaws/hosting.py -- the hosted
agent is DATA PLANE (the azd `microsoft.foundry` provider cannot express it,
and the per-agent identity only exists after the first version is created),
so the CLI drives it imperatively via `azure-ai-projects`, mirroring the
imperative boto3 style of the AWS original.

`azure-ai-projects` is an OPTIONAL extra: import is lazy with a friendly
install hint, so `chat --runtime local`, `status`, and the test suite never
need it. The SDK is preview (2.x); object attribute access is defensive
(`_dig`) so a renamed field degrades to a clear error, not an AttributeError
traceback.

Fail-fast + honest error reporting (parity with AWS commit 8dd198f):
  * chat_hosted() checks the agent EXISTS before invoking -- an absent agent
    aborts in seconds with a deploy hint and never starts a build.
  * An `error: true` payload inside a successful invoke exits 1 ("the runtime
    is up; this is a data-path issue"), never a green done on a failed run.
  * A transport-level failure exits 1 and points at the unaffected local
    runtime.
"""

import json
import os
import time

from . import config
from .azutil import AzCliError, azd_env_values, get_credential, log, run
from .config import (
    AGENT_PROTOCOL,
    ENV_CLAUDE_BASE_URL,
    ENV_CLAUDE_DEPLOYMENT,
    ENV_CONTENT_SAFETY,
    ENV_GUARDRAIL,
    ENV_KEY_VAULT_URI,
    ENV_MODEL,
    ENV_OPENAI_BASE_URL,
    ENV_OPENAI_DEPLOYMENT,
    ENV_PREFIX,
    ENV_PROVIDER,
    ENV_SERVERS,
    PROVIDER_AZURE_OPENAI,
    image_ref,
    sanitize_id,
)


class MissingExtraError(RuntimeError):
    """azure-ai-projects is not installed (hosted-agent commands need it)."""


def _sdk():
    """Lazy-import azure.ai.projects with a friendly install hint."""
    try:
        import azure.ai.projects as projects  # noqa: F401
        from azure.ai.projects import AIProjectClient

        return AIProjectClient
    except ImportError:
        raise MissingExtraError(
            "the hosted-agent commands need the 'hosting' extra -- install it "
            'with:  pip install "chkpmcpaz[hosting]"'
        ) from None


def _models():
    """The SDK model types used for create_version, resolved defensively (the
    SDK is preview; a missing name raises a clear error naming it)."""
    import azure.ai.projects.models as m

    def need(name):
        obj = getattr(m, name, None)
        if obj is None:
            raise RuntimeError(
                f"azure-ai-projects is too old: missing models.{name} -- "
                'upgrade with:  pip install -U "chkpmcpaz[hosting]"'
            )
        return obj

    return m, need


def _project_client(env):
    AIProjectClient = _sdk()
    endpoint = env.get("FOUNDRY_PROJECT_ENDPOINT")
    if not endpoint:
        raise RuntimeError(
            "FOUNDRY_PROJECT_ENDPOINT is not in the azd environment -- "
            "run `azd provision` (python3 -m chkpmcpaz deploy) first."
        )
    return AIProjectClient(endpoint=endpoint, credential=get_credential(),
                           allow_preview=True)


def _dig(obj, *names):
    """Fetch a nested attribute/key chain from a preview-SDK object or dict.
    Returns None instead of raising when any hop is absent."""
    cur = obj
    for name in names:
        if cur is None:
            return None
        if isinstance(cur, dict):
            cur = cur.get(name)
        else:
            cur = getattr(cur, name, None)
    return cur


def agent_environment(cfg, env):
    """The hosted agent's environment variables -- the shared contract keys plus
    the ACTIVE provider's endpoint/deployment (never redeclare platform-injected
    FOUNDRY_* vars). Values come from the azd outputs; server list/prefix from
    the stack config. No secrets here: the container reads credentials from Key
    Vault with its agent identity.

    Multi-provider (CONTRACT section 6e): CHKP_PROVIDER is ALWAYS injected, and
    only the selected provider's vars ride along -- a gpt stack carries
    OPENAI_BASE_URL/OPENAI_MODEL_DEPLOYMENT + CHKP_MODEL, a Claude stack carries
    CLAUDE_BASE_URL/CLAUDE_MODEL_DEPLOYMENT. They never both appear."""
    from .config import ENV_GUARDRAIL_PROVIDER, resolve_guardrail_provider
    from .guardrail import resolve_mode

    base = {
        ENV_KEY_VAULT_URI: env.get("KEY_VAULT_URI", cfg.key_vault_uri or ""),
        ENV_CONTENT_SAFETY: env.get("CONTENT_SAFETY_ENDPOINT",
                                    cfg.content_safety_endpoint or ""),
        ENV_SERVERS: ",".join(cfg.servers),
        ENV_PREFIX: cfg.prefix,
        # Prompt Shields screening in the container: honor the CHKP_GUARDRAIL
        # MODE persisted in the azd env at deploy (deploy --guardrail /
        # CHKP_GUARDRAIL=observe|enforce), NOT a hardcoded off. Normalize to the
        # canonical off/observe/enforce (resolve_mode) so `observe` (log-only)
        # reaches the container instead of collapsing to off -- the container
        # reads this same var through the same resolver.
        ENV_GUARDRAIL: resolve_mode(env.get(ENV_GUARDRAIL)),
        # Which guardrail engine screens in the container: content-safety
        # (default) or the Check Point AI Guardrail (lakera). The Lakera key +
        # project id are read from the <prefix>-lakera-guard Key Vault secret via
        # the agent identity -- never baked into this (immutable) env.
        ENV_GUARDRAIL_PROVIDER: resolve_guardrail_provider(env.get(ENV_GUARDRAIL_PROVIDER)),
        ENV_PROVIDER: cfg.provider,
    }
    if cfg.provider == PROVIDER_AZURE_OPENAI:
        base[ENV_OPENAI_BASE_URL] = env.get("OPENAI_BASE_URL", cfg.openai_base_url or "")
        base[ENV_OPENAI_DEPLOYMENT] = env.get("OPENAI_MODEL_DEPLOYMENT",
                                              cfg.openai_deployment or "")
        # Pin the hosted container to the same deployment the CLI chose.
        base[ENV_MODEL] = (cfg.openai_deployment
                           or env.get("OPENAI_MODEL_DEPLOYMENT") or "")
    else:
        base[ENV_CLAUDE_BASE_URL] = env.get("CLAUDE_BASE_URL", cfg.claude_base_url or "")
        base[ENV_CLAUDE_DEPLOYMENT] = env.get("CLAUDE_MODEL_DEPLOYMENT",
                                              cfg.claude_deployment or "")
    return base


def _current_version_image(project, name):
    """The container image ref the agent's ACTIVE version pins (a digest known
    to exist in the registry), or None if there is no active version / the
    lookup fails. Used as a robust fallback for a refresh that has no
    freshly-built digest, so it never pins an unresolvable :tag. Best-effort:
    any error degrades to the caller's :tag fallback."""
    try:
        for v in project.agents.list_versions(agent_name=name):
            status = str(_dig(v, "status") or "").lower().rsplit(".", 1)[-1]
            if status == "active":
                img = _dig(v, "definition", "container_configuration", "image")
                if img:
                    return str(img)
    except Exception:  # noqa: BLE001 -- fallback only; caller degrades to :tag
        pass
    return None


def _create_version(cfg, env, extra_env=None):
    """Create (or no-op onto) an agent version and route 100% of traffic to
    it, then poll to `active`. Returns the version id. Shared by deploy and
    refresh -- refresh passes a changed env var so the otherwise-identical
    request mints a NEW immutable version (identical requests are no-ops)."""
    m, need = _models()
    project = _project_client(env)
    name = cfg.agent_name()
    protocol, protocol_version = AGENT_PROTOCOL

    environment = agent_environment(cfg, env)
    if extra_env:
        environment.update(extra_env)

    # Digest-pin the image when deploy captured one (env IMAGE_DIGEST) so a
    # rebuilt image rolls out. With NO fresh digest (a standalone `creds apply`,
    # or a refresh that runs before this deploy's image build) reuse the image
    # the CURRENTLY DEPLOYED version pins -- a digest known to exist in the
    # registry -- rather than the fixed :tag, which may not resolve (ACR
    # retention / untagged manifests) and would fail the pull with ImageError.
    img = image_ref(env["AZURE_CONTAINER_REGISTRY_ENDPOINT"],
                    digest=env.get("IMAGE_DIGEST"))
    if not env.get("IMAGE_DIGEST"):
        current = _current_version_image(project, name)
        if current:
            img = current
    definition = need("HostedAgentDefinition")(
        protocol_versions=[need("ProtocolVersionRecord")(
            protocol=protocol, version=protocol_version)],
        cpu="1",
        memory="2Gi",
        container_configuration=need("ContainerConfiguration")(image=img),
        environment_variables=environment,
    )
    version_obj = project.agents.create_version(agent_name=name, definition=definition)
    version = str(_dig(version_obj, "version") or _dig(version_obj, "name") or "1")
    log(f"  agent version {version} created (image {img})")

    # Route 100% of traffic to this version (fixed-ratio selector).
    project.agents.update_details(
        agent_name=name,
        agent_endpoint=need("AgentEndpointConfig")(
            version_selector=need("VersionSelector")(
                version_selection_rules=[need("FixedRatioVersionSelectionRule")(
                    agent_version=version, traffic_percentage=100)]),
            protocol_configuration=need("ProtocolConfiguration")(
                responses=need("ResponsesProtocolConfiguration")()),
        ),
    )
    log(f"  100% traffic routed to version {version}")

    # Poll creating -> active (creating|active|failed|deleting|deleted).
    for attempt in range(80):
        v = project.agents.get_version(agent_name=name, agent_version=version)
        # `status` can be a plain str ("active") or an enum whose str() is the
        # qualified name ("AgentVersionStatus.ACTIVE") -- normalize to the leaf.
        status = str(_dig(v, "status") or "").lower().rsplit(".", 1)[-1]
        if status == "active":
            log(f"  version {version} is active")
            return version
        if status == "failed":
            raise RuntimeError(
                f"hosted agent version {version} entered 'failed' -- check the "
                f"container logs (azd ai agent monitor) and re-run deploy."
            )
        if attempt % 6 == 0:
            log(f"    status={status or 'unknown'} -- waiting...")
        time.sleep(10)
    raise RuntimeError(
        f"hosted agent version {version} did not reach 'active' within the "
        "wait budget -- re-run deploy (idempotent) or check the portal."
    )


def deploy_hosted_agent(cfg, env, *, roll=False):
    """Create the hosted agent version, route traffic, poll active. Returns
    the version id. Idempotent: an identical definition is a platform no-op
    that resolves to the already-active version.

    roll=True forces a FRESH version (a changing marker env var) so the sandboxes
    reboot and re-read the Key Vault secrets even when the image + env are
    otherwise unchanged -- used by `deploy --creds` so just-applied credentials
    actually roll out (the image digest alone may be identical across rebuilds)."""
    extra = ({"CHKP_REFRESH": time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())}
             if roll else None)
    return _create_version(cfg, env, extra_env=extra)


def refresh_hosted_agent(cfg, env):
    """Mint a NEW agent version (identical requests are no-ops, so a changing
    marker env var forces one) so every sandbox restarts and re-reads the Key
    Vault secrets at boot -- the Azure analogue of the AWS `refresh` runtime
    version-bump. Returns the new version id."""
    marker = {"CHKP_REFRESH": time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())}
    return _create_version(cfg, env, extra_env=marker)


def hosted_agent_status(cfg, env):
    """Read-only: the hosted agent's name/status/endpoint, or None when it
    (or the whole stack) is absent. Never creates anything."""
    if not env.get("FOUNDRY_PROJECT_ENDPOINT"):
        return None
    from azure.core.exceptions import HttpResponseError, ResourceNotFoundError

    project = _project_client(env)
    name = cfg.agent_name()
    agent = None
    for meth in ("get_agent", "get"):
        fn = getattr(project.agents, meth, None)
        if fn is None:
            continue
        try:
            agent = fn(agent_name=name)
            break
        except ResourceNotFoundError:
            return None
        except HttpResponseError as e:
            if getattr(e, "status_code", None) == 404:
                return None
            raise
        except TypeError:  # preview SDK signature drift: try positional
            try:
                agent = fn(name)
                break
            except ResourceNotFoundError:
                return None
    if agent is None:
        return None
    # The `get` agent object nests the active version under versions.latest;
    # older/preview shapes used a flat status or latest_version -- try all.
    status = (_dig(agent, "versions", "latest", "status")
              or _dig(agent, "status")
              or _dig(agent, "latest_version", "status")
              or _dig(agent, "state")
              or "unknown")
    version = (_dig(agent, "versions", "latest", "version")
               or _dig(agent, "current_version")
               or _dig(agent, "latest_version", "version"))
    endpoint = (f"{env['FOUNDRY_PROJECT_ENDPOINT']}/agents/{name}"
                "/endpoint/protocols/openai/responses")
    # status may be an enum whose str() is "AgentVersionStatus.ACTIVE" -- take
    # the leaf so callers can compare against a plain "active".
    return {"name": name, "status": str(status).lower().rsplit(".", 1)[-1],
            "version": version, "endpoint": endpoint}


def delete_hosted_agent(cfg, env):
    """Delete the hosted agent (data plane). Tolerates an already-absent
    agent -- destroy must be idempotent."""
    if not env.get("FOUNDRY_PROJECT_ENDPOINT"):
        log(f"  hosted agent {cfg.agent_name()}: no project endpoint -- skipping.")
        return
    from azure.core.exceptions import HttpResponseError, ResourceNotFoundError

    project = _project_client(env)
    name = cfg.agent_name()
    for meth in ("delete_agent", "delete"):
        fn = getattr(project.agents, meth, None)
        if fn is None:
            continue
        try:
            fn(agent_name=name)
            log(f"  hosted agent {name} deleted")
            return
        except ResourceNotFoundError:
            log(f"  hosted agent {name} not found -- skipping.")
            return
        except HttpResponseError as e:
            if getattr(e, "status_code", None) == 404:
                log(f"  hosted agent {name} not found -- skipping.")
                return
            raise
    raise RuntimeError("azure-ai-projects exposes no agent delete method -- "
                       'upgrade with:  pip install -U "chkpmcpaz[hosting]"')


# ---------------------------------------------------------------------------
# RBAC for the per-agent Entra identity (only exists AFTER create_version)
# ---------------------------------------------------------------------------

def _agent_principal_id(project, name):
    """The hosted agent's per-agent Entra identity (ServicePrincipal). Lives
    on the agent/version object; field name is preview-fluid, so try the
    documented paths before giving up with the manual command."""
    candidates = []
    for meth in ("get_agent", "get"):
        fn = getattr(project.agents, meth, None)
        if fn is not None:
            try:
                candidates.append(fn(agent_name=name))
            except Exception:  # noqa: BLE001 -- probing; the raise below is the real error
                pass
    for obj in candidates:
        for path in (("instance_identity", "principal_id"),
                     ("instance_identity", "principalId"),
                     ("identity", "principal_id")):
            pid = _dig(obj, *path)
            if pid:
                return str(pid)
    raise RuntimeError(
        f"could not resolve the agent identity principal id for '{name}' -- "
        "find it with `azd ai agent show` (.instance_identity.principal_id) "
        "and grant 'Cognitive Services User' (account scope) and 'Key Vault "
        "Secrets User' (vault scope) manually, then re-run deploy."
    )


def grant_agent_identity(cfg, env):
    """Grant the per-agent identity what its implicit access does NOT cover:
    `Cognitive Services User` at Foundry-ACCOUNT scope (the /anthropic Claude
    route and Content Safety are account-level, not proxied by the project
    endpoint) and `Key Vault Secrets User` on the vault (per-server creds).
    Tolerates already-existing assignments; RBAC propagation can take minutes."""
    project = _project_client(env)
    pid = _agent_principal_id(project, cfg.agent_name())
    sub = (env.get("AZURE_SUBSCRIPTION_ID") or cfg.subscription_id
           or json.loads(run(["az", "account", "show", "-o", "json"]).stdout)["id"])
    rg = env.get("AZURE_RESOURCE_GROUP") or cfg.resource_group()
    account = env["FOUNDRY_ACCOUNT_NAME"]
    vault = env["KEY_VAULT_NAME"]
    account_scope = (f"/subscriptions/{sub}/resourceGroups/{rg}/providers/"
                     f"Microsoft.CognitiveServices/accounts/{account}")
    scopes = [
        ("Cognitive Services User", account_scope),
        ("Key Vault Secrets User",
         f"/subscriptions/{sub}/resourceGroups/{rg}/providers/"
         f"Microsoft.KeyVault/vaults/{vault}"),
    ]
    # For the first-party gpt path, also grant 'Cognitive Services OpenAI User'
    # at account scope (belt-and-suspenders for OpenAI inference on the
    # AIServices account) -- needed for the e2e hosted gpt run.
    if cfg.provider == PROVIDER_AZURE_OPENAI:
        scopes.append(("5e0bd9bd-7b93-4f28-af87-19fc36ad61bd", account_scope))
    for role, scope in scopes:
        try:
            run(["az", "role", "assignment", "create",
                 "--assignee-object-id", pid,
                 "--assignee-principal-type", "ServicePrincipal",
                 "--role", role,
                 "--scope", scope])
            log(f"  granted '{role}' to agent identity on {scope.rsplit('/', 1)[-1]}")
        except AzCliError as e:
            s = str(e).lower()
            if "already exists" in s or "roleassignmentexists" in s or "conflict" in s:
                log(f"  '{role}' already granted -- skipping.")
            else:
                raise
    log("  (RBAC propagation can take a few minutes; if the first hosted chat "
        "gets 403, wait and retry -- no redeploy needed.)")


# ---------------------------------------------------------------------------
# Invoke (Responses endpoint, Entra bearer via the project OpenAI client)
# ---------------------------------------------------------------------------

def _session_store(session):
    """Local per-session state file holding only the last response id (NOT a
    secret) so follow-up invokes chain platform-managed history."""
    base = os.environ.get(config.ENV_LOG_DIR) or os.path.join(
        os.path.expanduser("~"), ".chkpmcpaz")
    path = os.path.join(base, "sessions")
    os.makedirs(path, exist_ok=True)
    return os.path.join(path, f"{sanitize_id(session)}.json")


def _session_prev_response(session):
    try:
        with open(_session_store(session), encoding="utf-8") as fh:
            return json.load(fh).get("previous_response_id") or None
    except (OSError, ValueError):
        return None


def _session_save(session, response_id):
    if not response_id:
        return
    try:
        with open(_session_store(session), "w", encoding="utf-8") as fh:
            json.dump({"previous_response_id": response_id}, fh)
    except OSError:
        pass  # session continuity is best-effort; the answer already printed


def _extract_text(resp):
    """The response text: the SDK convenience `output_text` first, else walk
    the output items (Responses shape: output[].content[].text)."""
    text = getattr(resp, "output_text", None)
    if text:
        return str(text)
    parts = []
    for item in (_dig(resp, "output") or []):
        for content in (_dig(item, "content") or []):
            t = _dig(content, "text")
            if t:
                parts.append(str(t))
    return "\n".join(parts)


def invoke_hosted_agent(cfg, env, task, *, session=None):
    """POST the task to the hosted agent's Responses endpoint (Entra bearer
    token via DefaultAzureCredential inside the project OpenAI client).
    Returns {"result": <text>, "error": <bool>} -- text the container prefixed
    with 'ERROR: ' means the agent inside could not complete (error=True)."""
    project = _project_client(env)
    client = project.get_openai_client(agent_name=cfg.agent_name())
    kwargs = {"input": task}
    if session:
        prev = _session_prev_response(session)
        if prev:
            kwargs["previous_response_id"] = prev
    resp = client.responses.create(**kwargs)
    if session:
        _session_save(session, getattr(resp, "id", None))
    text = _extract_text(resp).strip()
    from .guardrail import GUARDRAIL_BLOCK_SENTINEL

    if text.startswith(GUARDRAIL_BLOCK_SENTINEL):
        # A block tagged by the container (new image) -- a SUCCESS, not an error.
        return {"result": text[len(GUARDRAIL_BLOCK_SENTINEL):], "blocked": True,
                "error": False}
    if text.startswith("ERROR: "):
        body = text[len("ERROR: "):]
        if _looks_like_guardrail_block(body):
            # Legacy container (pre-sentinel): recognise a guardrail block by its
            # signature so it still renders as a win without redeploying.
            return {"result": body, "blocked": True, "error": False}
        return {"result": body, "error": True}
    return {"result": text, "error": False}


def _looks_like_guardrail_block(msg: str) -> bool:
    """True when a hosted error body is actually a guardrail block (from a
    pre-sentinel container image), so the CLI can still render it as a win."""
    s = (msg or "").lower()
    return "guardrailblocked" in s or ("blocked by" in s and "attack detected" in s)


# ---------------------------------------------------------------------------
# CLI entry points (chat --runtime hosted / refresh)
# ---------------------------------------------------------------------------

def chat_hosted(cfg, env, task, *, session=None):
    """`chat --runtime hosted`: fail-fast preflight, invoke, honest exit code.
    Calls the module-level status/invoke functions so tests can stub them."""
    name = cfg.agent_name()
    try:
        status = hosted_agent_status(cfg, env)
    except MissingExtraError as e:
        log(str(e))
        return 1
    if status is None:
        # Fail fast in seconds -- never start a build or wait on a sandbox
        # that has nothing to serve (parity with AWS 8dd198f).
        log(f"No hosted agent '{name}' found -- deploy first: python3 -m chkpmcpaz deploy")
        return 1

    try:
        result = invoke_hosted_agent(cfg, env, task, session=session)
    except Exception as e:  # noqa: BLE001 -- transport failures get a clean exit
        from .cli import _is_credential_error

        if _is_credential_error(e):
            raise  # main() prints the friendly re-auth block
        log(f"Hosted invoke failed: {type(e).__name__}: {str(e)[:200]}")
        log("The local runtime is unaffected:")
        log(f'  python3 -m chkpmcpaz chat "{task}"')
        return 1

    if result.get("blocked"):
        # The guardrail blocked the prompt inside the hosted agent -- a SUCCESS,
        # not a failure. Render it as a green security win and exit 0.
        from . import guardrail, ui

        for line in guardrail.blocked_lines(str(result.get("result") or "")):
            log(ui.err(line))       # red = blocked/deny (firewall-style); still exit 0
        return 0

    if result.get("error"):
        # HTTP success but the agent inside failed -- honor the error flag
        # instead of reporting a green done on a failed run.
        log("Hosted agent could not complete the task:")
        log("  " + str(result.get("result") or "unknown error"))
        log("The runtime is up; this is a data-path issue -- python3 -m chkpmcpaz status")
        return 1

    log("assistant  " + str(result.get("result", "")).strip())
    log("done (hosted)")
    return 0


def run_refresh(cfg):
    """The `refresh` command: bump the hosted agent version so sandboxes
    restart and re-read the Key Vault secrets. A missing agent is not an
    error -- the local runtime re-reads secrets on every run anyway."""
    env = azd_env_values(cfg.prefix)
    name = cfg.agent_name()
    try:
        status = hosted_agent_status(cfg, env)
    except MissingExtraError as e:
        log(str(e))
        log("Hosted agent (if any) not refreshed; the local runtime re-reads "
            "Key Vault secrets on every run.")
        return 0
    if status is None:
        log(f"No hosted agent '{name}' found -- nothing to refresh.")
        log("(The local runtime re-reads Key Vault secrets on every run; the "
            "hosted agent re-reads them when its sandboxes restart.)")
        return 0
    version = refresh_hosted_agent(cfg, env)
    log(f"Hosted agent '{name}' refreshed -> version {version} (sandboxes "
        "restart and re-read the Key Vault secrets).")
    return 0
