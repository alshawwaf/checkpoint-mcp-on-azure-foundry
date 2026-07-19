"""AI guardrail: Azure AI Content Safety Prompt Shields screening.

WHAT THIS IS (read before running):
  - AZURE-NATIVE ONLY. This mirrors the AWS repo's ai-guardrail demo (AgentCore
    Policy + Bedrock Guardrails), which was platform-native and explicitly NOT
    Check Point runtime protection (that is Early Access). Same positioning
    here: Prompt Shields is the platform's prompt-attack detector; Check Point
    is the roadmap signal source for this decision point.
  - PRE-LOOP SCREEN, not per-tool policy. AWS enforced a Cedar policy per
    gateway tool call; Azure has no gateway tier (servers are stdio children),
    so the screen runs on the USER INPUT before the first model call. With
    `chat --guardrail` (or CHKP_GUARDRAIL=1 hosted), a detected attack raises
    GuardrailBlocked and the model is never invoked.
  - REST, not SDK. There is no Python SDK method for shieldPrompt (July 2026);
    the documented path is a bearer-authenticated POST. TLS verification stays
    at the requests default -- ON, always (org policy; unit-tested).

Auth: Entra bearer on the COGNITIVE scope. The endpoint is the AIServices
account endpoint, so the caller (deployer locally, agent identity hosted)
needs `Cognitive Services User` at account scope. Tokens are used in headers
and never logged.
"""

from __future__ import annotations

import requests
from azure.identity import DefaultAzureCredential

from .config import (
    COGNITIVE_SCOPE,
    CONTENT_SAFETY_API_VERSION,
    ENV_CONTENT_SAFETY,
    ENV_GUARDRAIL,
    ENV_GUARDRAIL_PROVIDER,
    ENV_KEY_VAULT_URI,
    ENV_LAKERA_API_KEY,
    ENV_LAKERA_API_URL,
    ENV_LAKERA_PROJECT_ID,
    ENV_PREFIX,
    GUARDRAIL_PROVIDER_LAKERA,
    GUARDRAIL_TEST_INJECTION,
    LAKERA_DEFAULT_URL,
    StackConfig,
    lakera_env,
    lakera_secret_name,
    resolve_guardrail_provider,
)

# Benign probe for `guardrail test` -- an ordinary estate question that a
# healthy detector must let through.
BENIGN_PROMPT = "How many gateways are defined on my Check Point management server?"

# Operating modes (parity with the AWS guardrail's LOG_ONLY vs ENFORCE):
#   off      -- no screening at all.
#   observe  -- screen every prompt and REPORT detections, but never block
#               (the log-only mode the AWS gateway defaulted to on `provision`).
#   enforce  -- screen and BLOCK on detection (AWS `enforce`; the default when
#               `chat --guardrail` is passed).
GUARDRAIL_OFF = "off"
GUARDRAIL_OBSERVE = "observe"
GUARDRAIL_ENFORCE = "enforce"


def resolve_mode(value: str | None, *, flag: bool = False) -> str:
    """Map a CHKP_GUARDRAIL value (and the `--guardrail` flag) to an operating
    mode. Pure/unit-tested. The flag always means enforce; otherwise the env
    value decides: 'observe'/'log'/'log_only' -> observe; '1'/'on'/'enforce'/
    'true'/'yes' -> enforce; anything else (incl. None/'0'/'off') -> off."""
    if flag:
        return GUARDRAIL_ENFORCE
    v = (value or "").strip().lower()
    if v in ("observe", "log", "log_only", "logonly", "log-only"):
        return GUARDRAIL_OBSERVE
    if v in ("1", "on", "enforce", "true", "yes"):
        return GUARDRAIL_ENFORCE
    return GUARDRAIL_OFF


class GuardrailBlocked(RuntimeError):
    """Raised when the guardrail detects a prompt attack in the user input.

    A block is the guardrail SUCCEEDING, not an error -- it carries the engine
    `label` and detector `detail` so callers can present it as a security win.
    """

    def __init__(self, message: str = "", *, label: str = "the guardrail",
                 detail: str = ""):
        self.label = label
        self.detail = detail
        super().__init__(message or (
            f"Prompt blocked by {label} (attack detected)"
            + (f": {detail}." if detail else ".")))


# Sentinel the hosted container prefixes onto a Responses reply when the
# guardrail blocked the prompt, so the CLI tells a block apart from an ERROR
# (a block is a SUCCESS -- it must never render through the error path).
GUARDRAIL_BLOCK_SENTINEL = "GUARDRAIL_BLOCK: "


def active_engine_name(env=None) -> str:
    """Human-readable name of the guardrail engine that WILL screen -- used for
    the 'screening...' progress line shown BEFORE the (network) call so the wait
    never looks frozen."""
    import os

    e = os.environ if env is None else env
    if resolve_guardrail_provider(e.get(ENV_GUARDRAIL_PROVIDER)) == GUARDRAIL_PROVIDER_LAKERA:
        return "Check Point AI Guardrail (Lakera)"
    return "Azure AI Content Safety Prompt Shields"


def blocked_lines(message: str) -> list[str]:
    """One concise line shown when the guardrail blocks a prompt -- a block is a
    security win (rendered green with a ✓ upstream), not an error. `message` is
    the 'Prompt blocked by <engine> (attack detected)...' string."""
    msg = (message or "").strip() or "Prompt blocked by the guardrail (attack detected)."
    if msg.startswith("GuardrailBlocked: "):     # unwrap a legacy container prefix
        msg = msg[len("GuardrailBlocked: "):]
    return [f"🛡 {msg}"]


def screen_input(endpoint: str, user_prompt: str, documents: list[str] | None = None,
                 *, credential=None, timeout: float = 10.0) -> bool:
    """POST {endpoint}/contentsafety/text:shieldPrompt and return True iff an
    attack was detected in the prompt or any document. Raises on HTTP/auth
    failure -- an unreachable detector must never silently pass traffic."""
    if not endpoint:
        raise ValueError(f"content safety endpoint is required (is {ENV_CONTENT_SAFETY} set?)")
    cred = credential or DefaultAzureCredential()
    token = cred.get_token(COGNITIVE_SCOPE).token
    resp = requests.post(
        endpoint.rstrip("/") + "/contentsafety/text:shieldPrompt",
        params={"api-version": CONTENT_SAFETY_API_VERSION},
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"},
        json={"userPrompt": str(user_prompt),
              "documents": [str(d) for d in (documents or [])]},
        timeout=timeout,
    )
    resp.raise_for_status()
    body = resp.json()
    if bool((body.get("userPromptAnalysis") or {}).get("attackDetected")):
        return True
    return any(bool((d or {}).get("attackDetected"))
               for d in body.get("documentsAnalysis") or [])


def lakera_screen(text: str, api_key: str, project_id: str | None,
                  url: str | None = None, *, timeout: float = 10.0) -> tuple[bool, list[str]]:
    """Screen `text` with the Check Point AI Guardrail (Lakera Guard) API and
    return (flagged, detectors). One POST to the Guard endpoint; `flagged` is the
    verdict and `breakdown` names which detectors fired. Raises on HTTP/auth
    failure -- a broken detector must never silently pass traffic. TLS
    verification stays at the requests default (ON, always -- org policy). The
    api key/token is only sent in the Authorization header, never logged."""
    if not api_key:
        raise ValueError(f"{ENV_LAKERA_API_KEY} is required for the 'lakera' guardrail provider")
    resp = requests.post(
        (url or LAKERA_DEFAULT_URL),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"messages": [{"role": "user", "content": str(text)}],
              "project_id": project_id, "breakdown": True},
        timeout=timeout,
    )
    resp.raise_for_status()
    body = resp.json()
    flagged = bool(body.get("flagged"))
    detectors = [str(item.get("detector_type", "")).split("/")[-1].replace("_", " ")
                 for item in (body.get("breakdown") or []) if item.get("detected")]
    return flagged, detectors


def lakera_creds(env) -> tuple[str, str | None, str | None]:
    """(api_key, project_id, url) for the Lakera provider. Prefer the process
    env (local runs + .env); when only KEY_VAULT_URI/CHKP_PREFIX are present (the
    hosted container), hydrate the key + project id from the stack's Key Vault
    secret via the agent's managed identity -- the same pattern the @chkp server
    credentials use. Values are never logged."""
    api_key, project_id, url = lakera_env(env)
    if not api_key and env.get(ENV_KEY_VAULT_URI) and env.get(ENV_PREFIX):
        try:
            from . import keyvault

            body = keyvault.get_secret_json(
                env[ENV_KEY_VAULT_URI], lakera_secret_name(env[ENV_PREFIX])) or {}
            api_key = api_key or body.get(ENV_LAKERA_API_KEY, "")
            project_id = project_id or body.get(ENV_LAKERA_PROJECT_ID) or None
            url = url or body.get(ENV_LAKERA_API_URL) or None
        except Exception:  # noqa: BLE001 -- a KV miss surfaces as the clear "key required" error
            pass
    return api_key, project_id, url


def screen_prompt(cfg: StackConfig, text: str, *, env=None) -> tuple[bool, str, str]:
    """Provider-aware inline prompt screen. Returns (flagged, label, detail),
    dispatching on CHKP_GUARDRAIL_PROVIDER: 'lakera' -> Check Point AI Guardrail
    (Lakera Guard), anything else -> Azure AI Content Safety Prompt Shields
    (the platform default). This is the single hook the agent calls."""
    import os

    e = os.environ if env is None else env
    if resolve_guardrail_provider(e.get(ENV_GUARDRAIL_PROVIDER)) == GUARDRAIL_PROVIDER_LAKERA:
        api_key, project_id, url = lakera_creds(e)
        flagged, detectors = lakera_screen(text, api_key, project_id, url)
        return flagged, "Check Point AI Guardrail", (", ".join(detectors) if detectors else "")
    return screen_input(cfg.content_safety_endpoint or "", text), "Prompt Shields", ""


def _active_provider() -> str:
    import os

    return resolve_guardrail_provider(os.environ.get(ENV_GUARDRAIL_PROVIDER))


def run_guardrail_test(cfg: StackConfig) -> int:
    """`guardrail test`: drive a benign prompt and a prompt-injection payload
    through the ACTIVE guardrail provider (Check Point AI Guardrail / Lakera, or
    Prompt Shields) and report allow/deny per case. Returns 0 iff the benign
    prompt passes AND the injection is detected."""
    is_lakera = _active_provider() == GUARDRAIL_PROVIDER_LAKERA
    if not is_lakera and not cfg.content_safety_endpoint:
        print(f"{ENV_CONTENT_SAFETY} is not set -- deploy the stack first:  "
              "python3 -m chkpmcpaz deploy")
        return 1
    label_txt = "Check Point AI Guardrail (Lakera)" if is_lakera else "Prompt Shields"
    print(f"{label_txt} guardrail test")
    cases = (
        ("benign prompt   ", BENIGN_PROMPT, False),
        ("prompt injection", GUARDRAIL_TEST_INJECTION, True),
    )
    ok = True
    try:
        for label, text, expect_detected in cases:
            flagged, _lbl, detail = screen_prompt(cfg, text)
            verdict = (f"deny (attack detected{': ' + detail if detail else ''})"
                       if flagged else "allow")
            expected = "expected" if flagged == expect_detected else "UNEXPECTED"
            print(f"  {label}: {verdict}  -- {expected}")
            ok = ok and (flagged == expect_detected)
    except Exception as e:  # noqa: BLE001 -- report a clean failure, no traceback
        print(f"  guardrail unreachable ({type(e).__name__}: {str(e)[:160]})")
        return 1
    print("guardrail test " + ("passed" if ok else "FAILED"))
    return 0 if ok else 1


def run_guardrail_verify(cfg: StackConfig) -> int:
    """`guardrail verify`: read-only reachability of the Prompt Shields data
    path (the Azure analogue of the AWS `guardrail verify` tool list through
    the guardrail gateway -- here a single benign shieldPrompt call that proves
    the endpoint answers and the caller holds the account-scope role). Returns
    0 iff shieldPrompt is reachable."""
    is_lakera = _active_provider() == GUARDRAIL_PROVIDER_LAKERA
    if not is_lakera and not cfg.content_safety_endpoint:
        print(f"{ENV_CONTENT_SAFETY} is not set -- deploy the stack first:  "
              "python3 -m chkpmcpaz deploy")
        return 1
    try:
        detected, label, _ = screen_prompt(cfg, BENIGN_PROMPT)
    except Exception as e:  # noqa: BLE001 -- report a clean failure, no traceback
        print(f"{'Lakera Guard' if is_lakera else 'shieldPrompt'} not reachable "
              f"({type(e).__name__}: {str(e)[:160]})")
        if not is_lakera:
            print("The caller needs 'Cognitive Services User' at account scope; "
                  "RBAC propagation can take up to 30 min after a fresh deploy.")
        else:
            print("Check LAKERA_API_KEY / LAKERA_PROJECT_ID (env or the "
                  "<prefix>-lakera-guard Key Vault secret).")
        return 1
    print(f"{label} reachable (benign probe {'flagged?!' if detected else 'passed'})")
    return 0 if not detected else 1


# The current CHKP_GUARDRAIL persisted mode, for the provision/enforce reports.
def _current_mode(cfg: StackConfig) -> str:
    import os

    from .azutil import azd_env_values

    persisted = azd_env_values(cfg.prefix).get(ENV_GUARDRAIL)
    return resolve_mode(os.environ.get(ENV_GUARDRAIL) or persisted)


def run_guardrail(cfg: StackConfig, action: str, *, enforce: bool = False) -> int:
    """Dispatch the `guardrail {provision,enforce,test,verify,destroy}` actions.

    Prompt Shields is an INLINE pre-model screen that ships with the deployed
    AIServices account -- there is no separate policy gateway to stand up or
    tear down (unlike the AWS AgentCore-Policy demo). So provision/enforce/
    destroy are honest state reports that steer the operator to the real knob
    (the CHKP_GUARDRAIL mode: off / observe / enforce), while test and verify
    exercise the data path."""
    if action == "test":
        return run_guardrail_test(cfg)
    if action == "verify":
        return run_guardrail_verify(cfg)

    mode = _current_mode(cfg)
    if action == "provision":
        print("Prompt Shields needs no provisioning -- it ships with the stack's")
        print("AIServices account (the same endpoint `status` and `guardrail test`")
        print("already reach). There is no separate policy gateway to stand up.")
        print(f"Current screening mode (CHKP_GUARDRAIL): {mode}.")
        if enforce and mode != GUARDRAIL_ENFORCE:
            print("Requested --enforce: turn blocking on for the hosted agent with")
            print("  python3 -m chkpmcpaz deploy --guardrail    (persists CHKP_GUARDRAIL=1)")
            print("or per local run with  python3 -m chkpmcpaz chat --guardrail \"...\"")
        return 0
    if action == "enforce":
        print("Screening modes (set CHKP_GUARDRAIL, or use the flags below):")
        print("  off      -- no screening")
        print("  observe  -- screen every prompt and report attacks, never block (log-only)")
        print("  enforce  -- screen and BLOCK on a detected attack")
        print(f"Current mode: {mode}.")
        print("Enable ENFORCE:")
        print("  hosted : python3 -m chkpmcpaz deploy --guardrail   (bakes CHKP_GUARDRAIL=1 in)")
        print("  local  : python3 -m chkpmcpaz chat --guardrail \"...\"  (or CHKP_GUARDRAIL=1)")
        print("Log-only: export CHKP_GUARDRAIL=observe.")
        return 0
    if action == "destroy":
        print("Nothing to destroy -- Prompt Shields is part of the AIServices account,")
        print("not a separate resource. It goes away when the stack does:")
        print("  python3 -m chkpmcpaz destroy")
        print("To simply stop screening now: unset CHKP_GUARDRAIL (or set it to 0) and,")
        print("for the hosted agent, re-run  python3 -m chkpmcpaz deploy  (or refresh).")
        return 0
    print(f"unknown guardrail action {action!r}")
    return 2
