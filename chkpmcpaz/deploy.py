"""Full-stack deploy (`chkpmcpaz deploy`), the Azure analogue of the AWS
build.py flow -- azd owns the ARM resources (Bicep, incl. the Claude model
deployments), the CLI owns everything data-plane:

  [1] doctor preflight            [6] apply --creds file (optional)
  [2] azd environment + terms     [7] agent image (az acr build, repo-root ctx)
  [3] azd provision (Bicep)       [8] hosted agent (create + route + poll)
  [4] stack outputs               [9] agent identity RBAC
  [5] Key Vault placeholder      [10] status smoke test
      secrets (never clobber real)

Idempotent end to end: azd provision converges, secret seeding never
overwrites a non-placeholder body, the hosted-agent version create is a
platform no-op for identical definitions, and RBAC grants tolerate
already-exists. Partial failures are collected and the exit code is 1 --
a partial deploy is never reported as success.

Claude terms: deploying via Bicep AUTO-ACCEPTS Anthropic's commercial terms
through modelProviderData (organization name, country code, industry are sent
to Anthropic). The first run for an environment prints this before touching
anything, together with the pay-as-you-go requirement (CSP/free-trial/credit
subscriptions are rejected by the Claude deployment).

The ONLY credentials this module writes anywhere are non-real PLACEHOLDERs
(config.CRED_SHAPE), one per-server Key Vault secret. Real creds go in via
`chkpmcpaz creds` (or deploy --creds); values are never logged.
"""

import os

from . import config, ui
from .azutil import (
    REPO_ROOT,
    AzCliError,
    azd_env_exists,
    azd_env_values,
    hydrate_config,
    log,
    run,
    stream,
)
from .config import (
    CHEAPEST_OPENAI_MODEL,
    CRED_SHAPE,
    ENV_GUARDRAIL,
    ENV_GUARDRAIL_PROVIDER,
    ENV_LAKERA_API_KEY,
    ENV_LAKERA_API_URL,
    ENV_LAKERA_PROJECT_ID,
    ENV_MODEL,
    ENV_PROVIDER,
    ENV_SERVERS,
    GUARDRAIL_PROVIDER_LAKERA,
    IMAGE_REPO,
    IMAGE_TAG,
    PROVIDER_ANTHROPIC,
    PROVIDER_AZURE_OPENAI,
    SERVERS,
    lakera_secret_name,
    portal_links,
    resolve_guardrail_provider,
)

DEPLOY_STEPS = [
    "Preflight -- doctor",
    "azd environment",
    "azd provision (Bicep + Claude deployments)",
    "Stack outputs",
    "Key Vault placeholder secrets",
    "Apply credentials (--creds)",
    "Agent image (az acr build)",
    "Hosted agent (create + route traffic)",
    "Agent identity RBAC",
    "Status smoke test",
]

# modelProviderData.industry must be one of these LOWERCASE values -- the
# deployment LRO rejects anything else after minutes, so validate up front.
ALLOWED_INDUSTRIES = ("technology", "finance", "healthcare", "education",
                      "retail", "manufacturing", "government", "media", "other")

TERMS_NOTE = [
    "First deploy for this environment -- please note before continuing:",
    "  * Deploying Claude via Bicep AUTO-ACCEPTS Anthropic's commercial terms",
    "    (Azure Marketplace). The organization name, country code, and industry",
    "    you provide are sent to Anthropic with the deployment (modelProviderData).",
    "  * Claude on Foundry requires a PAY-AS-YOU-GO Azure subscription: CSP,",
    "    free-trial, student, and credit-only subscriptions are rejected when",
    "    the model deployment is created.",
    "  * Claude model deployments bill per token (GlobalStandard); destroy",
    "    deletes them (python3 -m chkpmcpaz destroy).",
]


def run_deploy(cfg, creds_file=None, include_agent=True, org=None, guardrail=False,
               model=None, remote_mcp=False, guardrail_provider=None):
    """Provision the whole stack. Returns 0, or 1 on partial failure (never
    reports a partial deploy as success), or 2 on a usage problem (missing
    --org / invalid industry) before anything is touched.

    Provider-aware (CONTRACT section 6d): the Anthropic terms attestation
    (org/country/industry) gates ONLY the anthropic (Claude) path. A first-party
    gpt-5-mini deploy (azure-openai) is not a Marketplace purchase -- it needs
    no --org and skips the terms note."""
    existing = azd_env_values(cfg.prefix)
    # The hosted agent's Prompt Shields screening MODE (off/observe/enforce),
    # resolved once and persisted into the azd env so the hosted agent version --
    # and later refresh/creds re-versions -- carry the operator's choice instead
    # of the previous always-off hardcode. The --guardrail flag forces enforce;
    # otherwise CHKP_GUARDRAIL (this shell, else the value already persisted)
    # decides -- crucially preserving `observe` (log-only screening), which the
    # old boolean collapsed to off. Provider-neutral.
    from .guardrail import resolve_mode
    guardrail_mode = resolve_mode(
        os.environ.get(ENV_GUARDRAIL) or existing.get(ENV_GUARDRAIL),
        flag=bool(guardrail))
    # Which guardrail engine screens: the platform Prompt Shields (default) or
    # the Check Point AI Guardrail (Lakera). Resolved once (flag > this shell >
    # persisted) and persisted so later commands + the hosted container agree.
    guardrail_provider_sel = resolve_guardrail_provider(
        guardrail_provider or os.environ.get(ENV_GUARDRAIL_PROVIDER)
        or existing.get(ENV_GUARDRAIL_PROVIDER))
    # Anthropic commercial-terms attestation -- ONLY for the Claude path. A
    # missing organization name is a usage error, not a deploy failure. For the
    # azure-openai path org stays empty (Bicep tolerates it) and nothing is sent
    # to Anthropic.
    country = "US"
    industry = "technology"
    if cfg.provider == PROVIDER_ANTHROPIC:
        # Precedence: the --org kwarg, then CLAUDE_ORGANIZATION_NAME (env), then
        # the value persisted in the azd env from a previous deploy.
        org = (org or os.environ.get("CLAUDE_ORGANIZATION_NAME")
               or existing.get("CLAUDE_ORGANIZATION_NAME") or "").strip()
        if not org:
            log("Claude on Foundry needs an organization name for Anthropic's")
            log("commercial terms (sent to Anthropic via modelProviderData). Provide")
            log("it once -- it is remembered in the azd environment afterwards:")
            log('  python3 -m chkpmcpaz deploy --org "<Your Company>"')
            log('or: export CLAUDE_ORGANIZATION_NAME="<Your Company>"')
            return 2
        country = (os.environ.get("CLAUDE_COUNTRY_CODE")
                   or existing.get("CLAUDE_COUNTRY_CODE") or "US").strip().upper()
        industry = (os.environ.get("CLAUDE_INDUSTRY")
                    or existing.get("CLAUDE_INDUSTRY") or "technology").strip().lower()
        if industry not in ALLOWED_INDUSTRIES:
            log(f"Invalid CLAUDE_INDUSTRY {industry!r} -- must be one of (lowercase): "
                + ", ".join(ALLOWED_INDUSTRIES))
            return 2
    else:
        # azure-openai (gpt-5-mini) is first-party, not a Marketplace purchase:
        # no --org required, no terms sent. org stays empty (Bicep's
        # claudeOrganizationName defaults to '').
        org = ""

    # The remote MCP tier reuses the agent image, so the image must build even
    # when --no-agent skips the hosted agent itself.
    need_image = include_agent or remote_mcp
    steps = list(DEPLOY_STEPS)
    if not creds_file:
        steps.remove("Apply credentials (--creds)")
    if not need_image:
        steps.remove("Agent image (az acr build)")
    if not include_agent:
        steps.remove("Hosted agent (create + route traffic)")
        steps.remove("Agent identity RBAC")
    if remote_mcp:
        steps.insert(steps.index("Status smoke test"),
                     "Remote MCP tier (Container Apps + Easy Auth)")

    rep = ui.StepUI("deploy", "DEPLOY", steps, cfg.location)
    ui.activate(rep)
    try:
        rc, ok, summary = _deploy(cfg, rep, org, country, industry,
                                  creds_file=creds_file, include_agent=include_agent,
                                  guardrail_mode=guardrail_mode, model=model,
                                  remote_mcp=remote_mcp,
                                  guardrail_provider=guardrail_provider_sel)
    except BaseException:
        rep.fail_current()
        ui.deactivate()
        rep.close(ok=False, summary=["Deploy aborted -- details above and in the log file.",
                                     "Every step is idempotent: re-run deploy to continue."])
        raise
    ui.deactivate()
    rep.close(ok=ok, summary=summary)
    return rc


def seed_secret(vault_uri, name, shape, *, getter=None, setter=None):
    """Seed one placeholder secret, NEVER clobbering a real value: an existing
    body whose values are not placeholders is kept untouched (it may hold the
    customer's real Check Point credentials from a previous `creds apply`).
    Returns True when a placeholder was written, False when the existing real
    value was kept. getter/setter default to the keyvault helpers and are
    injectable for unit tests (which must not touch Azure)."""
    if getter is None or setter is None:
        from . import keyvault

        getter = getter or keyvault.get_secret_json
        setter = setter or keyvault.set_secret_json
    existing = getter(vault_uri, name)
    if existing is not None and not _is_placeholder_body(existing):
        return False
    setter(vault_uri, name, dict(shape))
    return True


def _is_placeholder_body(body):
    """Same semantics as keyvault.is_placeholder, kept local so the seeding
    rule is unit-testable with injected getter/setter alone: True if the body
    is None/empty or ANY value is the placeholder marker."""
    if not body:
        return True
    return any(str(v) == config.PLACEHOLDER_VALUE for v in body.values())


def _seed_lakera_secret(cfg, vault_uri):
    """Store the operator's Lakera key/project (from the environment) in the
    <prefix>-lakera-guard Key Vault secret so the hosted agent can screen with
    the Check Point AI Guardrail. Returns a short status string; never logs the
    key. A missing key is reported (not fatal): local `chat` can still use the
    key from the shell/.env, and the operator can add it to the secret later."""
    from . import keyvault

    api_key = os.environ.get(ENV_LAKERA_API_KEY)
    project_id = os.environ.get(ENV_LAKERA_PROJECT_ID)
    url = os.environ.get(ENV_LAKERA_API_URL)
    name = lakera_secret_name(cfg.prefix)
    # Merge over any existing secret (read-modify-write): a later deploy that
    # only re-exports LAKERA_API_KEY must NOT drop a previously-stored project id
    # or url. Only keys actually provided this run are overwritten.
    try:
        body = dict(keyvault.get_secret_json(vault_uri, name) or {})
    except Exception:  # noqa: BLE001 -- start fresh if the secret can't be read
        body = {}
    if api_key:
        body[ENV_LAKERA_API_KEY] = api_key
    if project_id:
        body[ENV_LAKERA_PROJECT_ID] = project_id
    if url:
        body[ENV_LAKERA_API_URL] = url
    if not body.get(ENV_LAKERA_API_KEY):
        return (f"no {ENV_LAKERA_API_KEY} in the environment or the {name} secret -- "
                "add it before the hosted agent can screen (local chat can still "
                "use it from your shell/.env)")
    keyvault.set_secret_json(vault_uri, name, body)
    return f"key stored/updated in {name} (values not printed)"


def _deploy(cfg, rep, org, country, industry, creds_file=None, include_agent=True,
            guardrail_mode="off", model=None, remote_mcp=False,
            guardrail_provider="content-safety"):
    failures = []

    # ---------------------------------------------------------------------
    # 1. Doctor preflight -- fatal failures abort before ANY mutation.
    # ---------------------------------------------------------------------
    rep.begin()  # Preflight -- doctor
    rep.set_context(f"prefix {cfg.prefix} · {cfg.location} · {len(cfg.servers)} servers")
    from . import doctor

    if doctor.run_doctor(cfg) != 0:
        rep.fail_current()
        return 1, False, [
            ui.stack_up_banner(ok=False) + f"  ·  {cfg.location}",
            "  Doctor preflight failed -- nothing was created or changed.",
            "  Fix the failures above, then re-run: python3 -m chkpmcpaz deploy",
        ]

    # ---------------------------------------------------------------------
    # 2. azd environment (find-before-create) + the Anthropic terms values.
    # ---------------------------------------------------------------------
    rep.begin()  # azd environment
    first_run = not azd_env_exists(cfg.prefix)
    if first_run:
        # The Anthropic Marketplace terms attestation applies only to the Claude
        # path. A first-party gpt-5-mini deploy sends nothing to Anthropic, so
        # skip the note (it would be misleading).
        if cfg.provider == PROVIDER_ANTHROPIC:
            for line in TERMS_NOTE:
                log(line)
        run(["azd", "env", "new", cfg.prefix, "--no-prompt"], cwd=str(REPO_ROOT))
        log(f"  azd environment '{cfg.prefix}' created")
    else:
        log(f"  azd environment '{cfg.prefix}' exists -- reusing")
    env_sets = {
        "CLAUDE_ORGANIZATION_NAME": org,
        "CLAUDE_COUNTRY_CODE": country,
        "CLAUDE_INDUSTRY": industry,
        "AZURE_LOCATION": cfg.location,
        # Persist the DEPLOYED server set and guardrail mode so `refresh`,
        # `creds apply`, and `status` operate on what was actually deployed
        # (they run without --servers and would otherwise fall back to the 9
        # defaults, silently re-versioning the hosted agent with a different
        # tool set). Neither is a Bicep parameter -- they only round-trip
        # through `azd env get-values`.
        ENV_SERVERS: ",".join(cfg.servers),
        # Persist the resolved MODE string (off/observe/enforce) -- the hosted
        # container and agent_environment both read it back through resolve_mode,
        # so `observe` survives to the container instead of collapsing to off.
        ENV_GUARDRAIL: guardrail_mode,
        # Persist the guardrail ENGINE too (content-safety | lakera) so chat,
        # status, and the hosted container all screen with the same provider.
        ENV_GUARDRAIL_PROVIDER: guardrail_provider,
        # Multi-provider: persist the ACTIVE provider so later commands
        # (chat/status/refresh/creds/models) and the hosted container select the
        # same one -- mirrors the CHKP_SERVERS round-trip. Not a Bicep param.
        ENV_PROVIDER: cfg.provider,
    }
    # Drive the Bicep model-family gate (main.parameters.json maps these azd env
    # vars to the deployClaudeModels/deployOpenAiModel params): a gpt deploy
    # provisions ONLY the first-party gpt-5-mini deployment, a Claude deploy
    # only the Claude models.
    if cfg.provider == PROVIDER_AZURE_OPENAI:
        env_sets["DEPLOY_CLAUDE_MODELS"] = "false"
        env_sets["DEPLOY_OPENAI_MODEL"] = "true"
        env_sets["OPENAI_DEPLOYMENT_NAME"] = model or CHEAPEST_OPENAI_MODEL
    else:
        env_sets["DEPLOY_CLAUDE_MODELS"] = "true"
        env_sets["DEPLOY_OPENAI_MODEL"] = "false"
    # Pin the hosted container to the exact deployment the operator chose with
    # --model (the container reads CHKP_MODEL via StackConfig.from_env()).
    if model:
        env_sets[ENV_MODEL] = model
    # Persist whether THIS deploy hosts the agent so `chat` can default its
    # runtime to hosted ("ask my agent") instead of local -- AWS-parity UX.
    env_sets["CHKP_AGENT_HOSTED"] = "true" if include_agent else "false"
    if cfg.subscription_id:
        env_sets["AZURE_SUBSCRIPTION_ID"] = cfg.subscription_id
    for key, value in env_sets.items():
        run(["azd", "env", "set", key, value, "-e", cfg.prefix], cwd=str(REPO_ROOT))
    log(f"  org={org} country={country} industry={industry} location={cfg.location}")
    log(f"  servers={','.join(cfg.servers)}  guardrail={guardrail_mode}")

    # ---------------------------------------------------------------------
    # 3. azd provision -- Bicep infra incl. the Claude model deployments
    #    (modelProviderData auto-accepts the Anthropic terms; the deployment
    #    LRO can take up to ~20 min while RBAC propagates in parallel).
    # ---------------------------------------------------------------------
    rep.begin()  # azd provision
    log("  azd provision --no-prompt (Foundry account/project, Claude "
        "deployments, Key Vault, ACR, monitoring, RBAC)...")
    stream(["azd", "provision", "--no-prompt", "-e", cfg.prefix], cwd=str(REPO_ROOT))

    # ---------------------------------------------------------------------
    # 4. Stack outputs -- azd copied the Bicep outputs into the environment.
    # ---------------------------------------------------------------------
    rep.begin()  # Stack outputs
    env = azd_env_values(cfg.prefix)
    # Provider-aware model outputs: the azure-openai test stack emits
    # OPENAI_BASE_URL / OPENAI_MODEL_DEPLOYMENT where the Claude stack emits
    # CLAUDE_BASE_URL / CLAUDE_MODEL_DEPLOYMENT (the rest is shared).
    model_outputs = (("OPENAI_BASE_URL", "OPENAI_MODEL_DEPLOYMENT")
                     if cfg.provider == PROVIDER_AZURE_OPENAI
                     else ("CLAUDE_BASE_URL", "CLAUDE_MODEL_DEPLOYMENT"))
    required = ("FOUNDRY_PROJECT_ENDPOINT", *model_outputs,
                "KEY_VAULT_URI", "KEY_VAULT_NAME",
                "CONTENT_SAFETY_ENDPOINT", "AZURE_CONTAINER_REGISTRY_NAME",
                "AZURE_CONTAINER_REGISTRY_ENDPOINT", "AZURE_RESOURCE_GROUP",
                "FOUNDRY_ACCOUNT_NAME")
    missing = [k for k in required if not env.get(k)]
    if missing:
        rep.fail_current()
        return 1, False, [
            ui.stack_up_banner(ok=False) + f"  ·  {cfg.location}",
            f"  Provision outputs missing: {', '.join(missing)}",
            "  Re-run the deploy (idempotent); if it persists, inspect: azd env get-values",
        ]
    cfg = hydrate_config(cfg, env)
    if cfg.provider == PROVIDER_AZURE_OPENAI:
        log(f"  model    : {env['OPENAI_BASE_URL']} (deployment {env['OPENAI_MODEL_DEPLOYMENT']})")
    else:
        log(f"  claude   : {env['CLAUDE_BASE_URL']} (deployment {env['CLAUDE_MODEL_DEPLOYMENT']})")
    log(f"  project  : {env['FOUNDRY_PROJECT_ENDPOINT']}")
    log(f"  key vault: {env['KEY_VAULT_NAME']}")

    # ---------------------------------------------------------------------
    # 5. Placeholder secret PER SERVER that needs credentials, plus the
    #    agent-side quantum-gaia elicitation secret. A server starts and
    #    fails auth CLEANLY on placeholders until real creds are applied.
    # ---------------------------------------------------------------------
    rep.begin()  # Key Vault placeholder secrets
    vault_uri = env["KEY_VAULT_URI"]
    to_seed = [(s, CRED_SHAPE[SERVERS[s].creds]) for s in cfg.servers if SERVERS[s].creds]
    # quantum-gaia's secret is AGENT-side (answers the elicitation login) and
    # is seeded whether or not the server itself was selected for deploy.
    to_seed.append(("quantum-gaia", CRED_SHAPE["gaia"]))
    seen = set()
    for server, shape in to_seed:
        if server in seen:
            continue
        seen.add(server)
        name = cfg.secret_name(server)
        try:
            seeded = seed_secret(vault_uri, name, shape)
        except Exception as e:  # noqa: BLE001 -- record, keep seeding the rest
            _reraise_credential(e)
            log(f"  ✗ {server} -> {name}: {type(e).__name__}: {str(e)[:120]}")
            failures.append(f"secret {name} could not be seeded")
            continue
        log(f"  {server} -> {name}"
            + ("  (placeholder seeded)" if seeded else "  (existing real value kept)"))
    # When the Check Point AI Guardrail (Lakera) is the selected engine, store the
    # operator's key/project in the <prefix>-lakera-guard secret (from the shell/
    # .env) so the hosted agent can screen with it. Never a placeholder.
    if guardrail_provider == GUARDRAIL_PROVIDER_LAKERA:
        try:
            log(f"  guardrail (lakera): {_seed_lakera_secret(cfg, vault_uri)}")
        except Exception as e:  # noqa: BLE001 -- record, keep going
            _reraise_credential(e)
            log(f"  ✗ lakera guardrail secret: {type(e).__name__}: {str(e)[:120]}")
            failures.append("lakera guardrail secret could not be seeded")

    # ---------------------------------------------------------------------
    # 6. Optional: apply REAL credentials from --creds so the hosted agent
    #    boots straight into them. Individual placeholder sections are skipped
    #    (forgiving, like AWS) -- but a file that yields NOTHING is a loud
    #    deploy FAILURE: the operator explicitly asked for creds to be
    #    applied, and silently keeping placeholders sends them to a working
    #    stack that answers every question with a connection error.
    # ---------------------------------------------------------------------
    creds_applied = None  # None = --creds not given; True/False = outcome
    if creds_file:
        rep.begin()  # Apply credentials (--creds)
        if not os.path.exists(creds_file):
            rep.fail_current()
            failures.append(
                f"--creds: {creds_file} not found -- nothing applied "
                "(create it: python3 -m chkpmcpaz creds template, or copy the "
                "identically-formatted chkp-credentials.env from the AWS project)")
            creds_applied = False
        else:
            from . import creds as creds_mod

            # refresh=False: apply the secrets now but DON'T roll the agent here
            # -- the image is not built yet, so a refresh would pin a
            # not-yet-built image and fail. Step 8 rolls the agent (roll=True)
            # after the image build so the just-applied creds boot in.
            rc = creds_mod.apply_file(cfg, creds_file, refresh=False)
            if rc != 0:
                rep.fail_current()
                failures.append(
                    f"--creds: {creds_file} contained only placeholder values -- "
                    "nothing applied (edit it, then re-run deploy --creds, or: "
                    "python3 -m chkpmcpaz creds apply)")
                creds_applied = False
            else:
                creds_applied = True

    agent_endpoint = None
    image_ok = True
    remote_result = None
    need_image = include_agent or remote_mcp
    if need_image:
        # -------------------------------------------------------------------
        # 7. Agent image: remote build on ACR (linux/amd64; base image via the
        #    ECR public mirror to dodge Docker Hub anonymous-pull 429s). Build
        #    context is the REPO ROOT so the image gets chkpmcpaz/. The opt-in
        #    remote MCP tier reuses this SAME image (a different command).
        # -------------------------------------------------------------------
        rep.begin()  # Agent image (az acr build)
        registry = env["AZURE_CONTAINER_REGISTRY_NAME"]
        log(f"  az acr build -> {env['AZURE_CONTAINER_REGISTRY_ENDPOINT']}/"
            f"{IMAGE_REPO}:{IMAGE_TAG} (remote build; no local Docker needed)")
        try:
            stream(["az", "acr", "build", "--registry", registry,
                    "--image", f"{IMAGE_REPO}:{IMAGE_TAG}",
                    "--file", "agent/Dockerfile", "."], cwd=str(REPO_ROOT))
        except AzCliError as e:
            _reraise_credential(e)
            rep.fail_current()
            failures.append(f"agent image build failed ({str(e)[:160]})")
            image_ok = False

        if image_ok:
            # Capture the just-built image DIGEST so the hosted-agent version is
            # digest-pinned. The :tag is fixed ('v1'), so without this a rebuilt
            # image (new content, same tag) would make the version definition
            # identical -> create_version no-ops and the change never rolls out.
            # Best-effort: image_ref() falls back to the :tag if unset.
            try:
                digest = run(["az", "acr", "repository", "show",
                              "--name", registry,
                              "--image", f"{IMAGE_REPO}:{IMAGE_TAG}",
                              "--query", "digest", "-o", "tsv"]).stdout.strip()
                if digest:
                    env["IMAGE_DIGEST"] = digest
            except AzCliError:
                pass  # digest optional -- rollout still works via `refresh`

    if include_agent:
        # -------------------------------------------------------------------
        # 8. Hosted agent: create the version, route 100% traffic, poll active.
        # -------------------------------------------------------------------
        rep.begin()  # Hosted agent (create + route traffic)
        from . import hosting

        version = None
        if not image_ok:
            log("  skipped -- the agent image did not build.")
            rep.fail_current()
        else:
            try:
                # roll when creds were just applied so the sandboxes reboot and
                # re-read the new Key Vault secrets (even if the rebuilt image
                # digest is unchanged).
                version = hosting.deploy_hosted_agent(cfg, env, roll=bool(creds_applied))
                agent_endpoint = (f"{env['FOUNDRY_PROJECT_ENDPOINT']}/agents/"
                                  f"{cfg.agent_name()}/endpoint/protocols/openai/responses")
                log(f"  hosted agent {cfg.agent_name()} v{version} active")
                log(f"  endpoint: {agent_endpoint}")
            except hosting.MissingExtraError as e:
                rep.fail_current()
                failures.append(str(e))
            except Exception as e:  # noqa: BLE001 -- collected, never a partial-success exit 0
                _reraise_credential(e)
                rep.fail_current()
                failures.append(f"hosted agent create failed ({type(e).__name__}: {str(e)[:160]})")

        # -------------------------------------------------------------------
        # 9. RBAC for the per-agent identity (only exists after create): the
        #    /anthropic route + Content Safety need Cognitive Services User at
        #    ACCOUNT scope; the per-server creds need Key Vault Secrets User.
        # -------------------------------------------------------------------
        rep.begin()  # Agent identity RBAC
        if version is None:
            log("  skipped -- the hosted agent was not created.")
            rep.warn_current()
        else:
            try:
                hosting.grant_agent_identity(cfg, env)
            except Exception as e:  # noqa: BLE001
                _reraise_credential(e)
                rep.fail_current()
                failures.append(f"agent identity RBAC failed ({type(e).__name__}: "
                                f"{str(e)[:160]}) -- re-run deploy (idempotent)")

    if remote_mcp:
        # -------------------------------------------------------------------
        # 9b. Remote MCP tier (opt-in --remote-mcp): one scale-to-zero Container
        #     App per server in streamable-HTTP mode behind Entra Easy Auth --
        #     the AWS AgentCore-Gateway analogue. Reuses the agent image and is
        #     deployed imperatively (needs the image + the Easy Auth app id).
        # -------------------------------------------------------------------
        rep.begin()  # Remote MCP tier (Container Apps + Easy Auth)
        if not image_ok:
            log("  skipped -- the agent image did not build.")
            rep.fail_current()
            failures.append("remote MCP tier: agent image did not build")
        else:
            from . import remote_mcp as remote_mod

            try:
                remote_result = remote_mod.provision(cfg, env)
                new_failures = remote_result.get("failures") or []
                failures.extend(new_failures)
                if new_failures:
                    rep.warn_current()
                else:
                    log(f"  {len(remote_result.get('catalog') or [])} remote MCP "
                        "endpoint(s) live behind Entra Easy Auth")
            except Exception as e:  # noqa: BLE001 -- collected, never a partial-success exit 0
                _reraise_credential(e)
                rep.fail_current()
                failures.append(f"remote MCP tier failed ({type(e).__name__}: {str(e)[:160]})")

    # ---------------------------------------------------------------------
    # 10. Smoke: the same read-only checks `chkpmcpaz status` runs, streamed
    #     through this UI. Any failure fails the deploy.
    # ---------------------------------------------------------------------
    rep.begin()  # Status smoke test
    from . import verify

    if verify.run_status(cfg) != 0:
        rep.fail_current()
        failures.append("status smoke test reported failures (python3 -m chkpmcpaz status)")

    ok = not failures
    summary = [ui.stack_up_banner(ok=ok) + f"  ·  {cfg.location} · prefix {cfg.prefix}"]
    if cfg.provider == PROVIDER_AZURE_OPENAI:
        summary.append(f"  Model     : {env['OPENAI_MODEL_DEPLOYMENT']} @ {env['OPENAI_BASE_URL']}")
    else:
        summary.append(f"  Claude    : {env['CLAUDE_MODEL_DEPLOYMENT']} @ {env['CLAUDE_BASE_URL']}")
    if creds_applied is True:
        kv_note = "real creds applied from --creds"
    elif creds_applied is False:
        kv_note = "NO real creds applied -- see the failed step below"
    else:
        kv_note = ("creds unchanged this run -- apply a local .env with: "
                   "deploy --creds  (or: python3 -m chkpmcpaz creds apply)")
    summary.append(f"  Key Vault : {env['KEY_VAULT_NAME']} ({kv_note})")
    summary.append(f"  Agent     : {agent_endpoint or 'not hosted (--no-agent) -- local chat works'}")
    if remote_result and remote_result.get("catalog"):
        summary.append(
            f"  Remote MCP: {len(remote_result['catalog'])} @chkp endpoint(s) behind "
            f"Entra Easy Auth (audience {remote_result.get('audience', '')})")
        for ep in remote_result["catalog"]:
            summary.append(f"                {ep['server']}: {ep['url']}")
        summary.append("                consume: CHKP_MCP_TRANSPORT=remote python3 -m "
                       "chkpmcpaz chat \"...\"  (or add to the Foundry Toolbox)")
    summary.extend(ui.links_block(portal_links(env), title="Open in the browser:"))
    if failures:
        summary.append("")
        summary.append(f"  {len(failures)} step(s) FAILED (deploy is idempotent -- re-run it):")
        for f in failures:
            summary.append(f"    ✗ {f}")
    return (0 if ok else 1), ok, summary


def _reraise_credential(e):
    from .cli import _is_credential_error

    if _is_credential_error(e):
        raise e
