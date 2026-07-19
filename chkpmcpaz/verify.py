"""Read-only re-verifier (`chkpmcpaz status`): discover the deployed stack by
its fixed names and probe every layer -- azd outputs, the Foundry account and
model deployments (Claude or gpt-5-mini, with a live 8-token probe per preferred deployment), Key
Vault secrets (names + placeholder-or-real flags only, never values), the
agent container image, the hosted agent, Content Safety Prompt Shields, and
the local Node toolchain. Creates and deletes nothing -- safe to run
repeatedly.

Each failing check carries specific remediation text. When called from inside
`deploy` (its smoke-test step) the checks stream through the already-active
UI instead of opening a second one; `--json` prints a machine-readable report
with no UI at all.
"""

import asyncio
import json

from . import ui
from .azutil import AzCliError, azd_env_values, have, hydrate_config, log, run
from .config import (
    IMAGE_REPO,
    IMAGE_TAG,
    PROVIDER_ANTHROPIC,
    PROVIDER_AZURE_OPENAI,
    SERVERS,
    portal_links,
    preference_for,
)

STATUS_CHECKS = [
    "azd environment + outputs",
    "Foundry account + model deployments",
    "Key Vault secrets",
    "Agent image (ACR)",
    "Hosted agent",
    "Content Safety Prompt Shields",
    "Local toolchain (node/npx)",
]

# Opt-in end-to-end proof (`status --tools`, parity with AWS `verify`): spawn
# the @chkp servers over stdio and report per-server tool counts. Off by default
# because it cold-starts npx children (slow, network) -- the AWS gateway had
# them already running, stdio does not.
TOOLS_CHECK_NAME = "MCP tool catalog (@chkp servers)"

# The Bicep outputs every other check depends on (contract section 4.2). The
# model endpoint/deployment pair is provider-specific -- the azure-openai test
# stack emits OPENAI_BASE_URL / OPENAI_MODEL_DEPLOYMENT where the anthropic stack
# emits CLAUDE_BASE_URL / CLAUDE_MODEL_DEPLOYMENT; everything else is shared.
_SHARED_OUTPUTS = (
    "FOUNDRY_PROJECT_ENDPOINT",
    "FOUNDRY_ACCOUNT_NAME",
    "AZURE_RESOURCE_GROUP",
    "KEY_VAULT_URI",
    "KEY_VAULT_NAME",
    "CONTENT_SAFETY_ENDPOINT",
    "AZURE_CONTAINER_REGISTRY_NAME",
)

_MODEL_OUTPUTS = {
    PROVIDER_ANTHROPIC: ("CLAUDE_BASE_URL", "CLAUDE_MODEL_DEPLOYMENT"),
    PROVIDER_AZURE_OPENAI: ("OPENAI_BASE_URL", "OPENAI_MODEL_DEPLOYMENT"),
}


def required_outputs(cfg):
    """The Bicep outputs `status` requires, provider-aware (the model endpoint +
    deployment names differ by provider; the rest is shared)."""
    model = _MODEL_OUTPUTS.get(cfg.provider, _MODEL_OUTPUTS[PROVIDER_ANTHROPIC])
    return _SHARED_OUTPUTS + model


# Back-compat alias: the default (anthropic) required-output set as a flat tuple.
REQUIRED_OUTPUTS = _SHARED_OUTPUTS + _MODEL_OUTPUTS[PROVIDER_ANTHROPIC]


def _reraise_credential(e):
    """Checks swallow ordinary failures into remediation text, but a
    credential-shaped failure must reach cli.main()'s friendly re-auth block."""
    from .cli import _is_credential_error

    if _is_credential_error(e):
        raise e


# ---------------------------------------------------------------------------
# Individual checks. Each returns (ok, detail, fix): ok True/False, or None
# for "not provisioned -- informational" (rendered ⚠, does not fail status).
# ---------------------------------------------------------------------------

def _check_env(cfg, env):
    required = required_outputs(cfg)
    if not env:
        return (False, f"azd environment '{cfg.prefix}' not found (no outputs)",
                "deploy first: python3 -m chkpmcpaz deploy")
    missing = [k for k in required if not env.get(k)]
    if missing:
        return (False, f"outputs missing: {', '.join(missing)}",
                "re-run the provision: python3 -m chkpmcpaz deploy (idempotent)")
    return (True, f"environment '{cfg.prefix}' with all "
            f"{len(required)} required outputs", "")


def _check_foundry(cfg, env):
    account, rg = env["FOUNDRY_ACCOUNT_NAME"], env["AZURE_RESOURCE_GROUP"]
    try:
        show = json.loads(run(["az", "cognitiveservices", "account", "show",
                               "-n", account, "-g", rg, "-o", "json"]).stdout)
    except (AzCliError, ValueError) as e:
        _reraise_credential(e)
        return (False, f"account {account} not reachable ({str(e)[:120]})",
                "re-run the provision: python3 -m chkpmcpaz deploy")
    state = (show.get("properties") or {}).get("provisioningState", "unknown")
    log(f"  [foundry] account {account} provisioningState={state}")

    try:
        deps = json.loads(run(["az", "cognitiveservices", "account", "deployment",
                               "list", "-n", account, "-g", rg, "-o", "json"]).stdout)
        dep_names = {d.get("name") for d in deps}
    except (AzCliError, ValueError) as e:
        _reraise_credential(e)
        dep_names = set()
    preference = preference_for(cfg.provider)
    for dep in preference:
        mark = "✓" if dep in dep_names else "✗"
        log(f"  [model] {mark} deployment {dep}"
            + ("" if dep in dep_names else " MISSING"))

    callable_models = _probe_models(cfg, [d for d in preference if d in dep_names])
    if state == "Succeeded" and callable_models:
        return (True, f"account Succeeded; callable deployments: "
                f"{', '.join(callable_models)}", "")
    if not dep_names:
        return (False, "no model deployments found on the account",
                "re-run the provision: python3 -m chkpmcpaz deploy")
    return (False, f"account {state}; no deployment answered the probe",
            "grant 'Cognitive Services User' at account scope (RBAC "
            "propagation can take up to 30 min) and check deployment capacity")


def _probe_models(cfg, deployments):
    """8-token probe per deployment (mirrors the AWS capability probe): a
    deployment that answers is callable RIGHT NOW with this identity. Uses the
    ACTIVE provider's client + probe, so it works for both the Claude and the
    Azure OpenAI (gpt-5-mini) test path."""
    if not deployments or not cfg.model_base_url:
        return []
    from .providers import get_provider

    provider = get_provider(cfg.provider)
    try:
        client = provider.make_client(cfg)
    except Exception as e:  # noqa: BLE001 -- probe is best-effort
        _reraise_credential(e)
        log(f"  [model] could not build the probe client: {type(e).__name__}: {str(e)[:120]}")
        return []
    callable_models = provider.callable_deployments(client, deployments)
    for dep in deployments:
        if dep in callable_models:
            log(f"  [model] ✓ {dep} answered the 8-token probe")
        else:
            log(f"  [model] ✗ {dep} did not answer the 8-token probe")
    return callable_models


def _check_keyvault(cfg, env):
    from . import keyvault

    vault_uri = env["KEY_VAULT_URI"]
    servers = [s for s in cfg.servers if SERVERS[s].creds]
    if "quantum-gaia" not in servers:
        servers = servers + ["quantum-gaia"]  # agent-side elicitation secret
    missing, placeholders, real = [], [], []
    for s in servers:
        name = cfg.secret_name(s)
        try:
            body = keyvault.get_secret_json(vault_uri, name)
        except Exception as e:  # noqa: BLE001
            _reraise_credential(e)
            return (False, f"Key Vault not reachable ({type(e).__name__}: {str(e)[:120]})",
                    "check 'Key Vault Secrets Officer' on the vault; RBAC "
                    "propagation can take up to 30 min")
        if body is None:
            missing.append(name)
            log(f"  [kv] ✗ {name} MISSING")
        elif keyvault.is_placeholder(body):
            placeholders.append(name)
            log(f"  [kv] ✓ {name}  (placeholder)")
        else:
            real.append(name)
            log(f"  [kv] ✓ {name}  (real)")
    if missing:
        return (False, f"missing secret(s): {', '.join(missing)}",
                "re-run the deploy (it re-seeds placeholders): python3 -m chkpmcpaz deploy")
    detail = f"{len(real)} real, {len(placeholders)} placeholder"
    if placeholders:
        detail += " -- placeholder creds prove the chain but return auth errors; " \
                  "set real ones with: python3 -m chkpmcpaz creds apply " \
                  "(or apply a local .env at deploy time: deploy --creds)"
    return (True, detail, "")


def _check_acr(cfg, env):
    registry = env["AZURE_CONTAINER_REGISTRY_NAME"]
    try:
        tags = json.loads(run(["az", "acr", "repository", "show-tags",
                               "--name", registry, "--repository", IMAGE_REPO,
                               "-o", "json"]).stdout)
    except (AzCliError, ValueError) as e:
        _reraise_credential(e)
        return (None, f"image {IMAGE_REPO}:{IMAGE_TAG} not found in {registry} "
                "(deploy --no-agent?); the local runtime is unaffected",
                "re-run without --no-agent to host the agent: python3 -m chkpmcpaz deploy")
    if IMAGE_TAG in tags:
        return (True, f"{registry}/{IMAGE_REPO}:{IMAGE_TAG} present", "")
    return (False, f"repository {IMAGE_REPO} exists but tag {IMAGE_TAG} is missing",
            "re-run the deploy to rebuild the image: python3 -m chkpmcpaz deploy")


def _check_agent(cfg, env):
    from . import hosting

    try:
        status = hosting.hosted_agent_status(cfg, env)
    except hosting.MissingExtraError as e:
        return (None, str(e), "")
    except Exception as e:  # noqa: BLE001
        _reraise_credential(e)
        return (False, f"could not query the hosted agent ({type(e).__name__}: {str(e)[:120]})",
                "check the Foundry project endpoint and your data-plane role "
                "(Foundry Project Manager)")
    if status is None:
        return (None, f"hosted agent '{cfg.agent_name()}' not provisioned "
                "(deploy --no-agent?); local chat unaffected",
                "re-run without --no-agent to host it: python3 -m chkpmcpaz deploy")
    log(f"  [agent] {status['name']} status={status['status']}")
    log(f"  [agent] endpoint = {status['endpoint']}")
    if status["status"] == "active":
        return (True, f"'{status['name']}' active · {status['endpoint']}", "")
    return (False, f"'{status['name']}' status={status['status']}",
            "re-run the deploy (idempotent) or inspect logs: azd ai agent monitor")


def _check_content_safety(cfg, env):
    from . import guardrail

    try:
        detected = guardrail.screen_input(env["CONTENT_SAFETY_ENDPOINT"],
                                          "status probe -- benign text")
    except Exception as e:  # noqa: BLE001
        _reraise_credential(e)
        return (False, f"shieldPrompt not reachable ({type(e).__name__}: {str(e)[:120]})",
                "the caller needs 'Cognitive Services User' at account scope; "
                "RBAC propagation can take up to 30 min")
    return (True, "shieldPrompt reachable "
            f"(benign probe {'flagged?!' if detected else 'passed'})", "")


def _check_toolchain(cfg, env):
    missing = [c for c in ("node", "npx") if not have(c)]
    if missing:
        return (False, f"missing: {', '.join(missing)}",
                "install Node 20+ (spawns the @chkp stdio servers): https://nodejs.org")
    return (True, "node + npx present", "")


def _check_tools(cfg, env):
    """Spawn the configured @chkp servers over stdio, list their tools, and
    report per-server tool counts -- the end-to-end proof (AWS `verify` parity)
    that every server's tools actually resolve. Needs node/npx + the `mcp`
    extra; degrades to informational when either is missing."""
    if not (have("node") and have("npx")):
        return (None, "skipped -- node/npx not present (the tool catalog needs them)",
                "install Node 20+: https://nodejs.org")
    try:
        import mcp  # noqa: F401
    except ImportError:
        return (None, "skipped -- the optional 'mcp' extra is not installed",
                'install it to list the tool catalog:  pip install "chkpmcpaz[mcp]"')

    from . import keyvault
    from .mcp_stdio import ServerPool

    creds = {}
    if cfg.key_vault_uri:
        for s in cfg.servers:
            if not SERVERS[s].creds:
                continue
            try:
                body = keyvault.get_secret_json(cfg.key_vault_uri, cfg.secret_name(s))
            except Exception as e:  # noqa: BLE001 -- placeholder/none still lists tools
                _reraise_credential(e)
                body = None
            if body:
                creds[s] = body

    async def _discover():
        async with ServerPool(list(cfg.servers), creds) as pool:
            return await pool.list_tools()

    try:
        tools = asyncio.run(_discover())
    except Exception as e:  # noqa: BLE001
        _reraise_credential(e)
        return (False, f"tool discovery failed ({type(e).__name__}: {str(e)[:120]})",
                "check node/npx, network (npx downloads the packages), and creds")

    per = {}
    for t in tools:
        per[t.server] = per.get(t.server, 0) + 1
    for s in cfg.servers:
        n = per.get(s, 0)
        log(f"  [tools] {s}: {n} tool(s)" + ("" if n else " -- none resolved"))
    resolved = sum(1 for s in cfg.servers if per.get(s))
    if not tools:
        return (False, "no @chkp tools resolved from any server",
                "every server failed to start -- see the ✗ lines above; check "
                "node/npx, network, and credentials")
    return (True, f"{len(tools)} tools from {resolved}/{len(cfg.servers)} @chkp servers", "")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _remote_summary(env) -> str:
    """One-line description of the opt-in remote MCP tier when it is deployed
    (from the persisted CHKP_REMOTE_MCP catalog), or '' when absent. Purely
    informational -- the tier is optional, so it never affects the status code."""
    try:
        from . import remote_mcp

        remote = remote_mcp.remote_status(env)
    except Exception:  # noqa: BLE001 -- status must never fail on an optional add-on
        return ""
    if not remote:
        return ""
    return (f"{len(remote['endpoints'])} @chkp endpoint(s) behind Entra Easy Auth "
            "(CHKP_MCP_TRANSPORT=remote to consume; also usable by other MCP clients)")


def run_status(cfg, *, as_json=False, with_tools=False):
    """Read-only health check. Returns 0 when every check passes (checks for
    optional pieces that were deliberately skipped -- --no-agent -- report as
    informational, not failures). with_tools adds the (slower, npx-spawning)
    MCP tool-catalog check that reports per-server tool counts."""
    env = azd_env_values(cfg.prefix)
    cfg = hydrate_config(cfg, env)

    if as_json:
        results = _collect(cfg, env, quiet=True, with_tools=with_tools)
        ok = all(r["ok"] is not False for r in results)
        from . import remote_mcp

        print(json.dumps({"prefix": cfg.prefix, "location": cfg.location,
                          "ok": ok, "checks": results,
                          "remote_mcp": remote_mcp.remote_status(env)}, indent=2))
        return 0 if ok else 1

    if ui.active() is not None:
        # Nested inside deploy's smoke-test step: stream through the live UI.
        results = _collect(cfg, env, with_tools=with_tools)
        return 0 if all(r["ok"] is not False for r in results) else 1

    steps = list(STATUS_CHECKS) + ([TOOLS_CHECK_NAME] if with_tools else [])
    rep = ui.StepUI("status", "STATUS", steps, cfg.location)
    rep.set_context(f"prefix {cfg.prefix} · {cfg.location}")
    ui.activate(rep)
    try:
        results = _collect(cfg, env, rep=rep, with_tools=with_tools)
    except BaseException:
        rep.fail_current()
        ui.deactivate()
        rep.close(ok=False, summary=["Status aborted -- see the log file."])
        raise
    ui.deactivate()
    ok = all(r["ok"] is not False for r in results)
    summary = [ui.stack_up_banner(ok=ok) + f"  ·  {cfg.location} · prefix {cfg.prefix}"]
    for r in results:
        glyph = "✓" if r["ok"] else ("⚠" if r["ok"] is None else "✗")
        summary.append(f"  {glyph} {r['name']} -- {r['detail']}")
        if r["ok"] is False and r["fix"]:
            summary.append(f"      fix: {r['fix']}")
    remote = _remote_summary(env)
    if remote:
        summary.append(f"  ◈ Remote MCP -- {remote}")
    summary.extend(ui.links_block(portal_links(env), title="Open in the browser:"))
    rep.close(ok=ok, summary=summary)
    return 0 if ok else 1


def _collect(cfg, env, rep=None, quiet=False, with_tools=False):
    """Run the checks in order, honoring the short-circuit: when the azd
    environment/outputs check fails, the Azure-side checks cannot run and are
    reported as skipped (only the local toolchain + tool-catalog checks still
    execute -- the latter needs no Azure resources).

    quiet=True (--json) temporarily routes the whole log() stream into the
    void so nothing pollutes the JSON on stdout."""
    from . import azutil

    checks = [
        ("azd environment + outputs", _check_env),
        ("Foundry account + model deployments", _check_foundry),
        ("Key Vault secrets", _check_keyvault),
        ("Agent image (ACR)", _check_acr),
        ("Hosted agent", _check_agent),
        ("Content Safety Prompt Shields", _check_content_safety),
        ("Local toolchain (node/npx)", _check_toolchain),
    ]
    if with_tools:
        checks.append((TOOLS_CHECK_NAME, _check_tools))
    results = []
    env_ok = True
    if quiet:
        azutil.set_log_sink(lambda msg="": None)
    try:
        for name, fn in checks:
            if rep is not None:
                rep.begin()
            if not env_ok and fn not in (_check_env, _check_toolchain, _check_tools):
                results.append({"name": name, "ok": False,
                                "detail": "skipped -- no stack outputs",
                                "fix": "deploy first: python3 -m chkpmcpaz deploy"})
                if rep is not None:
                    rep.fail_current()
                continue
            ok, detail, fix = fn(cfg, env)
            results.append({"name": name, "ok": ok, "detail": detail, "fix": fix})
            glyph = "✓" if ok else ("⚠" if ok is None else "✗")
            log(f"  {glyph} {name} -- {detail}")
            if ok is False and fix:
                log(f"      fix: {fix}")
            if rep is not None:
                if ok is False:
                    rep.fail_current()
                elif ok is None:
                    rep.warn_current()
            if fn is _check_env and ok is False:
                env_ok = False
    finally:
        if quiet:
            azutil.set_log_sink(None)
    return results
