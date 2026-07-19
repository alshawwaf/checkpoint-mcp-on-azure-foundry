"""Local preflight (`chkpmcpaz doctor`): check the tools and subscription this
stack needs BEFORE any mutation. Checks only -- creates and changes nothing.

Hard FAILURES (exit 1) are things a deploy cannot survive: python < 3.11,
az/azd missing or not logged in, azd too old, node/npx missing or < 20, an
unsupported location, and a subscription that cannot buy Claude. WARNINGS are
things a deploy tolerates: docker absent (images build remotely via `az acr
build`) and optional extras not installed.

Claude subscription eligibility IS detected (no longer a blind reminder): the
offer type (quotaId), spending limit, and live Anthropic quota are read from
ARM, so an MSDN/Visual Studio, Dev/Test, free, student, sponsored or CSP
subscription -- none of which can purchase the Anthropic Marketplace models --
fails preflight up front instead of erroring minutes into the model LRO.

This eligibility gate is PROVIDER-AWARE: the credit/MSDN FAIL applies only when
the target provider is anthropic. gpt-5-mini is a first-party Azure OpenAI
model (not a Marketplace offer), so the azure-openai provider skips the credit
FAIL and instead just checks the gpt deployment quota -- which is exactly what
lets the cheap test model deploy on the MSDN Dev/Test subscription where Claude
is blocked.

A failed `az account show` is reported as a CHECK FAILURE with re-auth advice
rather than raised: doctor's whole job is diagnosis, so it must finish its
report even when the session is expired.
"""

import json
import re
import sys

from .azutil import AzCliError, have, log, run
from .config import (
    MODEL_PREFERENCE,
    PROVIDER_ANTHROPIC,
    PROVIDER_AZURE_OPENAI,
    SUPPORTED_LOCATIONS,
    preference_for,
)

MIN_PYTHON = (3, 11)
MIN_AZD = (1, 25, 3)
MIN_NODE = 20

# Subscription offer types that CANNOT purchase Anthropic (Marketplace) models,
# matched case-insensitively as substrings of the subscription quotaId. MSDN /
# Visual Studio and Dev/Test are credit-benefit offers; free/student/sponsored
# are credit-only; CSP and Dev/Test also cannot buy third-party Marketplace
# offers. A spending limit of "On" is the same signal (credit-capped).
CREDIT_OFFER_MARKERS = (
    "MSDN", "VisualStudio", "DevTest", "FreeTrial",
    "Student", "Sponsored", "CSP", "MPN",
)

OK, WARN, FAIL = "ok", "warn", "fail"
_GLYPH = {OK: "✓", WARN: "⚠", FAIL: "✗"}


def _say(status, label, detail=""):
    line = f"  {_GLYPH[status]} {label}"
    if detail:
        line += f" -- {detail}"
    log(line)
    return status


def _version_tuple(text):
    """First x.y[.z] version in `text` as an int tuple, or None."""
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text or "")
    if not m:
        return None
    return tuple(int(g or 0) for g in m.groups())


def _offer_is_credit(quota_id, spending_limit):
    """True when a subscription's offer cannot buy Claude: an active spending
    limit (credit cap) or a credit/benefit/Dev-Test/CSP offer type. Pure so it
    is unit-testable without touching az."""
    if (spending_limit or "") == "On":
        return True
    q = (quota_id or "").lower()
    return any(marker.lower() in q for marker in CREDIT_OFFER_MARKERS)


def _az_json(args, timeout=45):
    """Best-effort `az ...` returning parsed JSON, or None on any failure --
    every eligibility probe is advisory and must never crash doctor."""
    try:
        return json.loads(run(args, timeout=timeout).stdout)
    except (AzCliError, ValueError):
        return None


def _check_subscription_eligibility(results, account, location,
                                    provider=PROVIDER_ANTHROPIC):
    """Real replacement for the old 'pay-as-you-go' reminder: read the
    subscription offer type + spending limit and the live model quota so an
    ineligible subscription FAILS preflight up front (and thus aborts deploy),
    instead of erroring minutes into the model LRO. Never raises.

    PROVIDER-AWARE: the credit/MSDN FAIL applies ONLY to the anthropic provider
    -- Claude is an Anthropic Marketplace offer that credit/Dev-Test/MSDN
    subscriptions cannot buy. gpt-5-mini is FIRST-PARTY (Azure OpenAI), so the
    azure-openai provider skips that FAIL and instead just checks the gpt
    deployment quota (WARN only when it is actually 0)."""
    if not account or not account.get("id"):
        results.append(_say(WARN, "Model subscription eligibility",
                            "skipped -- not logged in (az login)"))
        return
    sub = account["id"]
    if provider == PROVIDER_AZURE_OPENAI:
        # First-party models are not a Marketplace purchase -- no credit gate.
        results.append(_say(OK, "Model subscription eligibility",
            "gpt-5-mini is first-party (Azure OpenAI) -- MSDN/Dev-Test/credit "
            "subscriptions can deploy it"))
    else:
        pol = (_az_json(["az", "rest", "--method", "get", "--url",
                         f"https://management.azure.com/subscriptions/{sub}"
                         "?api-version=2022-12-01"]) or {}).get("subscriptionPolicies", {})
        quota_id, spending = pol.get("quotaId", ""), pol.get("spendingLimit", "")
        if quota_id and _offer_is_credit(quota_id, spending):
            results.append(_say(FAIL, "Claude subscription eligibility",
                f"offer '{quota_id}'"
                + (", spending limit ON" if spending == "On" else "")
                + " cannot buy Claude -- Anthropic on Foundry needs a pay-as-you-go "
                "or Enterprise Agreement subscription (MSDN/Visual Studio, Dev/Test, "
                "free, student, sponsored and CSP subscriptions are rejected). "
                "Test on gpt-5-mini instead: deploy --model gpt-5-mini"))
        elif quota_id:
            results.append(_say(OK, "Claude subscription eligibility",
                                f"offer '{quota_id}' is billable (pay-as-you-go/EA)"))
        else:
            results.append(_say(WARN, "Claude subscription eligibility",
                "could not read the offer type -- ensure this is a pay-as-you-go or "
                "Enterprise Agreement subscription (not MSDN/Dev-Test/credit)"))

    # -- live model quota in the target region (0 = allocation not granted) ---
    if location not in SUPPORTED_LOCATIONS:
        return
    usage = _az_json(["az", "cognitiveservices", "usage", "list",
                      "--location", location, "--subscription", sub])
    if usage is None:
        return  # transient / permission -- deploy validates quota live anyway
    preference = preference_for(provider)
    limits = {}
    for u in usage:
        name = (u.get("name") or {}).get("value", "")
        for m in preference:
            # live usage names carry the deployment suffix, e.g.
            # '...GlobalStandard.gpt-5-mini' or '....claude-sonnet-4-6'.
            if name.endswith("." + m):
                limits[m] = max(limits.get(m, 0), u.get("limit") or 0)
    if provider == PROVIDER_AZURE_OPENAI:
        # GlobalStandard TPM allocation (the MSDN Dev/Test sub has 2000 in
        # eastus2). WARN only when it is genuinely 0 (allocation not granted).
        if limits and all(v <= 0 for v in limits.values()):
            results.append(_say(WARN, f"gpt-5-mini quota ({location})",
                "0 for " + " + ".join(preference) + " -- request gpt-5-mini "
                "GlobalStandard capacity for this subscription/region "
                "(https://aka.ms/oai/stuquotarequest) before deploy"))
        elif limits:
            results.append(_say(OK, f"gpt-5-mini quota ({location})",
                ", ".join(f"{m}={int(limits.get(m, 0))}" for m in preference)
                + " (GlobalStandard TPM units)"))
    else:
        if limits and all(v <= 0 for v in limits.values()):
            results.append(_say(WARN, f"Claude quota ({location})",
                "0 for " + " + ".join(MODEL_PREFERENCE) + " -- Anthropic quota is "
                "Marketplace-gated and starts at 0; initial allocation must be "
                "requested from Microsoft before deploy can create the models"))
        elif limits:
            results.append(_say(OK, f"Claude quota ({location})",
                ", ".join(f"{m}={int(limits.get(m, 0))}" for m in MODEL_PREFERENCE)))


def run_doctor(cfg):
    """Run every local check; return 0 when no HARD failure was found."""
    results = []
    log("chkpmcpaz doctor -- local preflight (nothing is created or changed)\n")

    # -- python ---------------------------------------------------------------
    py = sys.version_info
    if py >= MIN_PYTHON:
        results.append(_say(OK, f"python {py.major}.{py.minor}.{py.micro}",
                            f">= {'.'.join(map(str, MIN_PYTHON))} required"))
    else:
        results.append(_say(FAIL, f"python {py.major}.{py.minor}.{py.micro}",
                            f"3.11+ required -- upgrade and re-run with python3"))

    # -- az CLI + login state ---------------------------------------------------
    account = None
    if not have("az"):
        results.append(_say(FAIL, "az not found",
                            "install: https://aka.ms/azure-cli, then: az login"))
    else:
        try:
            account = json.loads(run(["az", "account", "show", "-o", "json"]).stdout)
            state = account.get("state", "")
            detail = (f"logged in -- subscription '{account.get('name')}' "
                      f"({account.get('id')}), state {state or 'unknown'}")
            if state and state != "Enabled":
                results.append(_say(FAIL, "az subscription", detail
                                    + " -- an Enabled subscription is required"))
            else:
                results.append(_say(OK, "az", detail))
        except (AzCliError, ValueError) as e:
            results.append(_say(FAIL, "az login",
                                f"`az account show` failed ({str(e)[:120]}) -- "
                                "log in again: az login"))

    # -- azd ------------------------------------------------------------------
    if not have("azd"):
        results.append(_say(FAIL, "azd not found",
                            "install: https://aka.ms/azd, then: azd auth login"))
    else:
        try:
            ver = _version_tuple(run(["azd", "version"]).stdout)
        except AzCliError:
            ver = None
        if ver and ver >= MIN_AZD:
            results.append(_say(OK, f"azd {'.'.join(map(str, ver))}",
                                f">= {'.'.join(map(str, MIN_AZD))} required"))
        else:
            shown = ".".join(map(str, ver)) if ver else "unknown version"
            results.append(_say(FAIL, f"azd {shown}",
                                f"{'.'.join(map(str, MIN_AZD))}+ required -- "
                                "upgrade: https://aka.ms/azd"))

    # -- node / npx (the @chkp servers are npm packages run via npx) -----------
    if not have("node"):
        results.append(_say(FAIL, "node not found",
                            "Node 20+ is required to spawn the @chkp MCP "
                            "servers -- install: https://nodejs.org"))
    else:
        try:
            nver = _version_tuple(run(["node", "--version"]).stdout)
        except AzCliError:
            nver = None
        if nver and nver[0] >= MIN_NODE:
            results.append(_say(OK, f"node v{'.'.join(map(str, nver))}",
                                f">= {MIN_NODE} required"))
        else:
            results.append(_say(FAIL, f"node {'v' + '.'.join(map(str, nver)) if nver else '(version unknown)'}",
                                f"Node {MIN_NODE}+ required"))
    if have("npx"):
        results.append(_say(OK, "npx", "present"))
    else:
        results.append(_say(FAIL, "npx not found",
                            "ships with Node/npm -- reinstall Node 20+"))

    # -- docker (optional: images build remotely on ACR) ------------------------
    if have("docker"):
        results.append(_say(OK, "docker", "present (only needed for local "
                            "container testing; deploy builds remotely)"))
    else:
        results.append(_say(WARN, "docker not found",
                            "optional -- the agent image builds remotely via "
                            "`az acr build`; install Docker only to test the "
                            "container locally"))

    # -- location ----------------------------------------------------------------
    if cfg.location in SUPPORTED_LOCATIONS:
        results.append(_say(OK, f"location {cfg.location}",
                            "hosts both Foundry Hosted Agents and Claude accounts"))
    else:
        results.append(_say(FAIL, f"location {cfg.location}",
                            f"unsupported -- use one of: {', '.join(SUPPORTED_LOCATIONS)}"))

    # -- subscription eligibility + live model quota (real ARM checks) ---------
    # Provider-aware: the credit/MSDN FAIL fires only for the anthropic provider
    # (Claude is a Marketplace offer); azure-openai (gpt-5-mini) is first-party.
    _check_subscription_eligibility(results, account, cfg.location,
                                    provider=cfg.provider)

    # -- optional extras ----------------------------------------------------------
    for module, extra, why in (
        ("mcp", "mcp", "local chat spawns the @chkp servers over stdio"),
        ("azure.ai.projects", "hosting", "deploy/refresh of the Foundry Hosted Agent"),
    ):
        try:
            __import__(module)
            results.append(_say(OK, f"{module} importable", why))
        except ImportError:
            results.append(_say(WARN, f"{module} not installed",
                                f'{why} -- install: pip install "chkpmcpaz[{extra}]"'))

    # -- org policy reminders -------------------------------------------------
    log("\norg policy reminders:")
    log("  * secrets live in Azure Key Vault only -- never in code, env files")
    log("    you commit, or command lines (chkp-credentials.env is gitignored)")
    log("  * TLS verification is always on; this tool never disables it")
    log("  * every endpoint is Entra-authenticated; there is no anonymous surface")

    fails = sum(1 for r in results if r == FAIL)
    warns = sum(1 for r in results if r == WARN)
    if fails:
        log(f"\ndoctor: {fails} check(s) FAILED, {warns} warning(s) -- fix the "
            "failures above before deploying.")
        return 1
    log(f"\ndoctor: all checks passed ({warns} warning(s)). Ready to deploy:")
    if cfg.provider == PROVIDER_AZURE_OPENAI:
        # First-party gpt-5-mini test path -- no Anthropic org/terms needed.
        log("  python3 -m chkpmcpaz deploy --model gpt-5-mini")
    else:
        log('  python3 -m chkpmcpaz deploy --org "<Your Company>"')
    return 0
