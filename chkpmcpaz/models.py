"""`chkpmcpaz models {status,enable,disable}` -- the active provider's preferred
model deployments, exposed as a first-class command (parity with the AWS
`chkpmcpaws models` surface).

PROVIDER-AWARE: the preferred set comes from `preference_for(cfg.provider)`, so
on a Claude (`anthropic`) stack this manages the Claude deployments and on a
`gpt-5-mini` (`azure-openai`) stack it manages the gpt-5-mini deployment --
whichever family the stack was actually deployed with (the CLI resolves
`cfg.provider` from the persisted `CHKP_PROVIDER`).

Unlike AWS Bedrock -- where model access is a per-account AGREEMENT this tool
created and could revoke (with an SSM marker) -- models on Foundry are delivered
as model DEPLOYMENTS owned by the Bicep stack: `deploy` creates them
(enable-on-deploy) and `destroy` / `azd down` removes them with the resource
group (revoke-on-destroy). This module reports and manages that reality with
the same three verbs:

  status   read-only: each preferred deployment's presence + an 8-token probe
           (callable RIGHT NOW with this identity), tagged "(deployed by this
           stack)". The Azure analogue of the AWS availability view.
  enable   ensure the preferred deployments exist. Bicep OWNS creation, so a
           missing deployment is fixed by (re-)running deploy -- reported here,
           never silently provisioned behind the operator's back.
  disable  delete the active provider's preferred deployments this stack created
           (the `disable`/revoke analogue). The rest of the stack is untouched;
           `destroy` remains the way to remove everything.

No secrets flow through this module; it prints deployment NAMES only.
"""

import json

from .azutil import AzCliError, azd_env_values, hydrate_config, log, run
from .config import preference_for


def classify(present: bool, callable_: bool) -> str:
    """Pure (unit-tested): one deployment's state for the status report.
    'callable' wins -- a callable deployment is necessarily present."""
    if callable_:
        return "callable"
    if present:
        return "present (not callable by this identity)"
    return "missing"


def _reraise_credential(e):
    from .cli import _is_credential_error

    if _is_credential_error(e):
        raise e


def _deployment_names(env):
    """The deployment names on the Foundry account, or None if unreadable."""
    account = env.get("FOUNDRY_ACCOUNT_NAME")
    rg = env.get("AZURE_RESOURCE_GROUP")
    if not account or not rg:
        return None
    try:
        deps = json.loads(run(["az", "cognitiveservices", "account", "deployment",
                               "list", "-n", account, "-g", rg, "-o", "json"]).stdout)
    except (AzCliError, ValueError) as e:
        _reraise_credential(e)
        return None
    return {d.get("name") for d in deps if isinstance(d, dict)}


def run_models(cfg, action):
    """Dispatch `models {status,enable,disable}`. Returns the exit code."""
    env = azd_env_values(cfg.prefix)
    cfg = hydrate_config(cfg, env)
    if not env.get("FOUNDRY_ACCOUNT_NAME") or not env.get("AZURE_RESOURCE_GROUP"):
        log(f"No deployed stack found for prefix '{cfg.prefix}'.")
        log("Model deployments are created by deploy: python3 -m chkpmcpaz deploy")
        return 1
    if action == "status":
        return _status(cfg, env)
    if action == "enable":
        return _enable(cfg, env)
    if action == "disable":
        return _disable(cfg, env)
    log(f"unknown models action {action!r}")
    return 2


def _status(cfg, env):
    """Read-only: each preferred deployment's presence + a live 8-token probe."""
    names = _deployment_names(env)
    if names is None:
        log("Could not list the account's model deployments.")
        log("Check the Foundry account + your role: python3 -m chkpmcpaz status")
        return 1
    from .verify import _probe_models  # 8-token probe, shared with `status`

    preference = preference_for(cfg.provider)
    present = [m for m in preference if m in names]
    callable_models = set(_probe_models(cfg, present))
    log(f"Model deployments on {env['FOUNDRY_ACCOUNT_NAME']} "
        "(deployed and owned by this stack):")
    any_callable = False
    for model in preference:
        state = classify(model in names, model in callable_models)
        any_callable = any_callable or (model in callable_models)
        tag = " (deployed by this stack)" if model in names else ""
        log(f"  {model}: {state}{tag}")
    if not any_callable:
        log("None of the preferred deployments answered a probe with this "
            "identity -- grant 'Cognitive Services User' at account scope "
            "(RBAC can take up to 30 min) or re-provision: python3 -m chkpmcpaz deploy")
        return 1
    return 0


def _enable(cfg, env):
    """Ensure the preferred deployments exist. Bicep owns creation, so a missing
    one is reported with the deploy remedy (never silently provisioned here)."""
    names = _deployment_names(env)
    if names is None:
        log("Could not list the account's model deployments.")
        return 1
    preference = preference_for(cfg.provider)
    missing = [m for m in preference if m not in names]
    if not missing:
        log("Preferred model deployments already present (enabled on deploy): "
            + ", ".join(preference))
        return 0
    log("Missing model deployment(s): " + ", ".join(missing))
    log("Model deployments are Bicep-owned -- create/repair them by re-running")
    log("deploy (idempotent):  python3 -m chkpmcpaz deploy")
    return 0


def _disable(cfg, env):
    """Delete the preferred Claude deployments this stack created (revoke). The
    rest of the stack is untouched; `destroy` removes everything."""
    account, rg = env["FOUNDRY_ACCOUNT_NAME"], env["AZURE_RESOURCE_GROUP"]
    names = _deployment_names(env)
    if names is None:
        log("Could not list the account's model deployments.")
        return 1
    present = [m for m in preference_for(cfg.provider) if m in names]
    if not present:
        log("No preferred model deployments to remove -- nothing to do.")
        return 0
    removed = []
    for model in present:
        try:
            run(["az", "cognitiveservices", "account", "deployment", "delete",
                 "-n", account, "-g", rg, "--deployment-name", model])
            log(f"  revoked deployment {model}")
            removed.append(model)
        except AzCliError as e:
            _reraise_credential(e)
            log(f"  could not remove {model}: {str(e)[:160]}")
    log(f"Removed {len(removed)} model deployment(s): {', '.join(removed) or '(none)'}")
    log("Re-create them any time with: python3 -m chkpmcpaz deploy")
    return 0 if removed else 1
