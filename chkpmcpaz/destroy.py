"""Stack teardown (`chkpmcpaz destroy`), in dependency order:

  hosted agent (data plane) -> Claude model deployments -> azd down --force --purge

A read-only INVENTORY runs first (resource group, hosted agent, Key Vault
secrets incl. soft-deleted, ACR image, Claude deployments) so the confirmation
prompt is honest and a clean subscription short-circuits with
"Nothing to destroy." -- nothing is touched before the y/N (or --yes).

The Claude deployments die with the resource group anyway, but they are
deleted explicitly FIRST for parity with the AWS enable-on-deploy /
revoke-on-destroy behavior (and so the transcript shows the model access
being revoked, not just a resource group vanishing). `azd down --force
--purge` then removes the resource group AND purges the Key Vault + Cognitive
Services soft-deletes, so an immediate redeploy under the same names works.

Idempotent: every step tolerates an already-absent resource, so a partial
teardown can be re-run to completion. Removes ONLY this stack's environment;
other azd environments and unrelated resources are never touched.
"""

import json
import sys

from . import ui
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
from .config import CLAUDE_DEPLOYMENTS


def _reraise_credential(e):
    """Inventory probes treat "can't list" as "not found" -- EXCEPT for
    credential-shaped failures, which must reach cli.main()'s re-auth block
    (an expired login must never masquerade as a clean subscription)."""
    from .cli import _is_credential_error

    if _is_credential_error(e):
        raise e


def inventory(cfg, env):
    """Read-only probe of what a teardown would actually remove. Returns
    human-readable findings (empty list = nothing of this stack exists)."""
    found = []
    rg = env.get("AZURE_RESOURCE_GROUP") or cfg.resource_group()

    rg_exists = False
    try:
        out = run(["az", "group", "exists", "-n", rg]).stdout.strip().lower()
        rg_exists = out == "true"
    except AzCliError as e:
        _reraise_credential(e)
    if rg_exists:
        found.append(f"resource group {rg} (Foundry account/project, Key Vault, "
                     "ACR, monitoring -- everything Bicep provisioned)")

    account = env.get("FOUNDRY_ACCOUNT_NAME")
    if rg_exists and account:
        try:
            deps = json.loads(run(["az", "cognitiveservices", "account", "deployment",
                                   "list", "-n", account, "-g", rg, "-o", "json"]).stdout)
            names = [d.get("name") for d in deps if d.get("name")]
            if names:
                found.append(f"Claude model deployment(s): {', '.join(names)} "
                             "(deleted explicitly -- revoke-on-destroy)")
        except (AzCliError, ValueError) as e:
            _reraise_credential(e)

    try:
        from . import hosting

        status = hosting.hosted_agent_status(cfg, env)
        if status:
            found.append(f"hosted agent {status['name']} ({status['status']})")
    except Exception as e:  # noqa: BLE001 -- inventory stays read-only + tolerant
        _reraise_credential(e)

    vault = env.get("KEY_VAULT_NAME")
    if rg_exists and vault:
        live, deleted = _secret_names(cfg, env)
        if live:
            found.append(f"{len(live)} Key Vault secret(s): {', '.join(live)}")
        if deleted:
            found.append(f"{len(deleted)} soft-deleted secret(s): {', '.join(deleted)}")

    registry = env.get("AZURE_CONTAINER_REGISTRY_NAME")
    if rg_exists and registry:
        try:
            tags = json.loads(run(["az", "acr", "repository", "show-tags",
                                   "--name", registry, "--repository", "chkp-agent",
                                   "-o", "json"]).stdout)
            if tags:
                found.append(f"ACR image {registry}/chkp-agent ({len(tags)} tag(s))")
        except (AzCliError, ValueError) as e:
            _reraise_credential(e)

    if rg_exists:
        # The optional agent bridge (Function + storage) lives in the RG, so
        # `azd down` below removes it with everything else -- surface it so the
        # plan is honest.
        try:
            from . import bridge

            b = bridge.describe(cfg, env)
            if b.get("present"):
                found.append(f"agent bridge Function ({b['url']}) -- removed with the RG")
        except Exception as e:  # noqa: BLE001 -- inventory stays tolerant
            _reraise_credential(e)

    return found


def _secret_names(cfg, env):
    """(live, soft-deleted) secret names for this stack -- names only, never
    values. Best-effort: an unreachable vault reports empty lists."""
    vault = env.get("KEY_VAULT_NAME")
    marker = f"{cfg.prefix}-"
    live, deleted = [], []
    try:
        items = json.loads(run(["az", "keyvault", "secret", "list",
                                "--vault-name", vault, "-o", "json"]).stdout)
        live = [i.get("name") for i in items
                if str(i.get("name", "")).startswith(marker)]
    except (AzCliError, ValueError) as e:
        _reraise_credential(e)
    try:
        items = json.loads(run(["az", "keyvault", "secret", "list-deleted",
                                "--vault-name", vault, "-o", "json"]).stdout)
        deleted = [i.get("name") for i in items
                   if str(i.get("name", "")).startswith(marker)]
    except (AzCliError, ValueError) as e:
        _reraise_credential(e)
    return live, deleted


def _confirm(yes):
    if yes:
        return True
    if not sys.stdin.isatty():
        log("Refusing to destroy without confirmation in a non-interactive shell.")
        log("Re-run with --yes to proceed.")
        return False
    prompt = "Proceed and destroy the items above? [y/N] "
    if sys.stdout.isatty():
        prompt = ui.BOLD + "Proceed and destroy the items above?" + ui.RESET + " [y/N] "
    try:
        reply = input(prompt)
    except EOFError:
        return False
    return reply.strip().lower() in ("y", "yes")


def run_destroy(cfg, *, yes=False, force_delete_secret=False):
    """Inventory -> plan -> confirm -> teardown. Returns 0 on success (or a
    clean subscription), 1 on confirmation-refused or partial failure."""
    env = azd_env_values(cfg.prefix)
    cfg = hydrate_config(cfg, env)

    found = inventory(cfg, env)
    if not found:
        # Consistent with the AWS port: no real resources -> a clean one-line
        # no-op, never the destroy-plan window or a "DESTROYED INCOMPLETE"
        # banner. A lingering LOCAL azd environment (e.g. a deploy that failed
        # before provisioning) is harmless -- a redeploy reuses it.
        scope = f"location {cfg.location}" + (f", prefix '{cfg.prefix}'" if cfg.prefix else "")
        log(f"Nothing from this stack found ({scope}) -- nothing to destroy.")
        return 0

    from . import remote_mcp

    has_remote = bool(remote_mcp.remote_status(env))
    notes = [f"stack prefix: {cfg.prefix}",
             "azd down runs with --force --purge: the Key Vault and Cognitive "
             "Services soft-deletes are purged so a redeploy under the same "
             "names works immediately"]
    if has_remote:
        notes.append("remote MCP tier present: its Container Apps + environment go "
                     "with the resource group; the Entra app registration is "
                     "deleted explicitly (revoke-on-destroy)")
    if force_delete_secret:
        notes.append("--force-delete-secret: soft-deleted secrets are purged "
                     "even if the vault survives a partial teardown")
    # Plan + confirmation run in PLAIN text first: a y/N prompt can't live
    # inside a full-screen (alt-screen) takeover.
    ui.render_destroy_plan(cfg.location, [("Azure stack", found)], notes)
    if not _confirm(yes):
        return 1

    steps = [
        "Hosted agent (data plane)",
        "Claude model deployments (revoke)",
    ]
    if has_remote:
        steps.append("Remote MCP tier (Entra app registration)")
    steps.append("azd down --force --purge")
    if force_delete_secret:
        steps.append("Purge soft-deleted secrets")
    rep = ui.StepUI("destroy", "DESTROY", steps, cfg.location)
    rep.set_context(f"prefix {cfg.prefix} · {cfg.location}")
    ui.activate(rep)
    try:
        rc = _destroy(cfg, env, rep, force_delete_secret, has_remote)
    except BaseException:
        rep.fail_current()
        ui.deactivate()
        rep.close(ok=False, summary=["Destroy aborted -- idempotent, safe to re-run. "
                                     "See the log file."])
        raise
    ui.deactivate()
    ok = rc == 0
    summary = [ui.done_banner("DESTROYED", ok=ok) + f"  ·  {cfg.location} · prefix {cfg.prefix}"]
    if not ok:
        summary.append("  Teardown is idempotent -- re-run destroy to finish.")
    rep.close(ok=ok, summary=summary)
    return rc


def _destroy(cfg, env, rep, force_delete_secret, has_remote=False):
    failures = []

    # ---------------------------------------------------------------------
    # 1. Hosted agent -- data plane, unknown to Bicep, so azd down would
    #    leave it orphaned on the (about-to-die) project. Delete it first.
    # ---------------------------------------------------------------------
    rep.begin()  # Hosted agent (data plane)
    try:
        from . import hosting

        hosting.delete_hosted_agent(cfg, env)
    except Exception as e:  # noqa: BLE001 -- the RG deletion below still removes the project
        _reraise_credential(e)
        log(f"  (hosted agent delete skipped: {type(e).__name__}: {str(e)[:120]})")
        rep.warn_current()
    # Clear the hosted marker so a later `chat` defaults back to local
    # (best-effort: the azd env may already be gone).
    try:
        run(["azd", "env", "set", "CHKP_AGENT_HOSTED", "false", "-e", cfg.prefix],
            cwd=str(REPO_ROOT))
    except AzCliError:
        pass

    # ---------------------------------------------------------------------
    # 2. Claude model deployments -- revoke-on-destroy parity with AWS. The
    #    RG deletion would take them anyway; deleting them first makes the
    #    revocation explicit and keeps the transcript honest.
    # ---------------------------------------------------------------------
    rep.begin()  # Claude model deployments (revoke)
    account = env.get("FOUNDRY_ACCOUNT_NAME")
    rg = env.get("AZURE_RESOURCE_GROUP") or cfg.resource_group()
    if account:
        for deployment, _model, _ver in CLAUDE_DEPLOYMENTS:
            try:
                run(["az", "cognitiveservices", "account", "deployment", "delete",
                     "-n", account, "-g", rg, "--deployment-name", deployment])
                log(f"  revoked Claude model deployment {deployment}")
            except AzCliError as e:
                _reraise_credential(e)
                log(f"  {deployment}: not found or already deleted -- skipping.")
    else:
        log("  no Foundry account in the azd outputs -- skipping.")

    # ---------------------------------------------------------------------
    # 2b. Remote MCP tier (only when present): the Container Apps, environment,
    #     and identity live in the RG and go with `azd down`; the Entra app
    #     registration is a TENANT object and is deleted explicitly here.
    # ---------------------------------------------------------------------
    if has_remote:
        rep.begin()  # Remote MCP tier (Entra app registration)
        try:
            from . import remote_mcp

            for note in remote_mcp.teardown(cfg, env):
                log(f"  {note}")
        except Exception as e:  # noqa: BLE001 -- the RG deletion still removes the apps
            _reraise_credential(e)
            log(f"  (remote MCP teardown skipped: {type(e).__name__}: {str(e)[:120]})")
            rep.warn_current()

    # ---------------------------------------------------------------------
    # 3. azd down -- deletes rg-<prefix> and PURGES the Key Vault + Cognitive
    #    Services soft-deletes (without --purge a redeploy under the same
    #    names collides with the soft-deleted originals).
    # ---------------------------------------------------------------------
    rep.begin()  # azd down --force --purge
    if azd_env_exists(cfg.prefix):
        try:
            stream(["azd", "down", "--force", "--purge", "-e", cfg.prefix],
                   cwd=str(REPO_ROOT))
        except AzCliError as e:
            _reraise_credential(e)
            rep.fail_current()
            failures.append(f"azd down failed ({str(e)[:160]})")
    else:
        log(f"  azd environment '{cfg.prefix}' not found -- skipping azd down.")

    # ---------------------------------------------------------------------
    # 4. Optional: purge soft-deleted secrets if the vault survived (e.g. a
    #    partial azd down). Names only; a purged vault makes this a no-op.
    # ---------------------------------------------------------------------
    if force_delete_secret:
        rep.begin()  # Purge soft-deleted secrets
        vault = env.get("KEY_VAULT_NAME")
        purged = 0
        if vault:
            try:
                run(["az", "keyvault", "show", "-n", vault])
                _live, deleted = _secret_names(cfg, env)
                for name in deleted:
                    try:
                        run(["az", "keyvault", "secret", "purge",
                             "--vault-name", vault, "-n", name])
                        log(f"  purged {name}")
                        purged += 1
                    except AzCliError as e:
                        _reraise_credential(e)
                        log(f"  {name}: purge skipped ({str(e)[:80]})")
            except AzCliError as e:
                _reraise_credential(e)
                log("  vault already gone (purged with the stack) -- nothing to purge.")
        if not purged:
            log("  no soft-deleted secrets to purge.")

    if failures:
        for f in failures:
            log(f"  ✗ {f}")
        return 1
    return 0
