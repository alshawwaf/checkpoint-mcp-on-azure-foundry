"""chkpmcpaz CLI -- Check Point MCP servers + Claude agent on Microsoft Foundry.

The tool's job: run Check Point @chkp MCP servers as stdio tools for a Claude
security-operations agent -- locally or on a Foundry Hosted Agent -- and wrap
deploy/status/creds/teardown into one idempotent CLI.

    python3 -m chkpmcpaz deploy [--servers "..."] --org "<Your Company>"
    python3 -m chkpmcpaz chat "how many hosts are configured?"
    python3 -m chkpmcpaz status                         # read-only re-check
    python3 -m chkpmcpaz doctor                         # local preflight
    python3 -m chkpmcpaz refresh                        # restart the hosted agent
                                                        # so it re-reads secrets
    python3 -m chkpmcpaz destroy [--yes]

Optional Prompt Shields demo (Azure AI Content Safety -- NOT Check Point
runtime protection, which is Early Access; talk to Check Point):

    python3 -m chkpmcpaz guardrail test
    python3 -m chkpmcpaz chat --guardrail "..."

Global options (accepted before OR after the subcommand):
  --subscription  Azure subscription id (default: your az/azd default)
  --location      Azure region (default eastus2; swedencentral also supported
                  -- the only two regions with BOTH Foundry Hosted Agents and
                  Claude starter-kit accounts)
  --prefix        namespace a second stack in the same subscription
"""

import argparse
import dataclasses
import os
import sys

from . import __version__
from .azutil import log
from .config import (
    DEFAULT_ACTOR,
    DEFAULT_LOCATION,
    DEFAULT_PREFIX,
    ENV_GUARDRAIL,
    ENV_MODEL,
    ENV_PROVIDER,
    ENV_SERVERS,
    PROVIDERS,
    SUPPORTED_LOCATIONS,
    StackConfig,
    load_env_file,
    parse_servers,
    resolve_provider,
    validate_prefix,
)

# --provider choices: 'auto' (detect from --model/CHKP_MODEL) plus the two real
# providers. Kept next to the config PROVIDERS tuple so a new provider surfaces
# in the CLI automatically.
_PROVIDER_CHOICES = ["auto", *PROVIDERS]

# Shown in `chat -h` (epilog) and when `chat` is run with no task. Ask in
# plain English -- the agent discovers the servers' tools and picks them.
CHAT_EXAMPLES = """\
example questions (the agent chooses the tools):

  inventory & policy
    chkpmcpaz chat "how many hosts are configured, and what access layers exist?"
    chkpmcpaz chat "list the access layers and how many rules each has"
    chkpmcpaz chat "which access rules are unused (zero hits)?"
    chkpmcpaz chat "are there any Any-to-Any rules I should worry about?"

  threat prevention & HTTPS inspection
    chkpmcpaz chat "summarize the threat-prevention posture"
    chkpmcpaz chat "is HTTPS inspection enabled, and what is bypassed?"

  gateways, logs & docs
    chkpmcpaz chat "what gateways and servers are managed, and their HA state?"
    chkpmcpaz chat "show recent dropped connections from the logs"
    chkpmcpaz chat "how do I configure HTTPS inspection on a Quantum gateway?"

  cross-server
    chkpmcpaz chat "give me a security posture summary across policy, threat prevention, and HTTPS inspection"

  hosted runtime (platform-managed conversation history via --session)
    chkpmcpaz chat --runtime hosted --session soc-review "how many hosts are configured?"
    chkpmcpaz chat --runtime hosted --session soc-review "and which of those did we flag last time?"

Add --guardrail to screen the prompt with Azure AI Content Safety Prompt
Shields first; --model <name> to pick a deployment (e.g. gpt-5-mini for the
cheap Azure OpenAI test path, or a Claude deployment; provider auto-detected;
default: auto-select); --runtime hosted to run the same loop on the Foundry
Hosted Agent instead of locally. With placeholder credentials the tool calls
reach your estate and error -- that still proves the chain; set real creds
(chkpmcpaz creds) for real answers.
"""


def _global_options():
    """Shared options, attachable to the root parser AND every subparser so
    they work in either position (chkpmcpaz --prefix x deploy / chkpmcpaz
    deploy --prefix x). SUPPRESS keeps a subparser's unset default from
    clobbering a value parsed before the subcommand; main() reads them via
    getattr."""
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--subscription",
        default=argparse.SUPPRESS,
        metavar="ID",
        help="Azure subscription id (default: your az/azd default subscription).",
    )
    common.add_argument(
        "--location",
        default=argparse.SUPPRESS,
        help=f"Azure location (default {DEFAULT_LOCATION}; supported: "
        f"{', '.join(SUPPORTED_LOCATIONS)} -- the only regions hosting BOTH "
        "Foundry Hosted Agents and Claude accounts).",
    )
    common.add_argument(
        "--prefix",
        default=argparse.SUPPRESS,
        help="Namespace every resource name for a parallel stack "
        "(lowercase, digits, hyphens; max 12 chars). Default: "
        f"'{DEFAULT_PREFIX}'.",
    )
    common.add_argument(
        "--plain",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Disable the live terminal UI and print plain line logging "
        "(automatic when output is piped, in CI, or with NO_COLOR).",
    )
    common.add_argument(
        "--version",
        action="version",
        version=f"chkpmcpaz {__version__}",
    )
    return common


def _build_parser():
    common = _global_options()
    parser = argparse.ArgumentParser(
        prog="chkpmcpaz",
        parents=[common],
        description="Check Point MCP servers + Claude agent on Microsoft "
        "Foundry -- deploy, chat, check status, and tear down.",
    )
    # required=False so a bare `chkpmcpaz` prints full help (handled in main)
    # instead of a terse argparse error.
    sub = parser.add_subparsers(
        dest="cmd",
        required=False,
        metavar="{deploy,chat,status,doctor,refresh,models,creds,guardrail,bridge,destroy}",
        parser_class=argparse.ArgumentParser,
    )

    d = sub.add_parser(
        "deploy",
        parents=[common],
        help="Deploy the stack: azd provision (Bicep infra + Claude model "
        "deployments), Key Vault secrets, agent image, Foundry Hosted Agent",
    )
    d.add_argument(
        "--servers",
        help='Space- or comma-separated @chkp server names (no "@chkp/" prefix, '
        'no "-mcp" suffix), or "all" for every generally-usable server. "all" '
        "excludes argos-erm, harmony-sase, workforce-ai (need real/tenant "
        "creds) and quantum-gaia (interactive elicitation auth); deploy any of "
        f"those explicitly by name. Also honors the {ENV_SERVERS} env var.",
    )
    d.add_argument(
        "--creds",
        nargs="?",
        const="chkp-credentials.env",
        default=None,
        help="Write REAL credentials from this local file (default "
        "chkp-credentials.env) into each server's Key Vault secret at deploy "
        "time, so the agent boots with them -- no separate 'creds apply'. "
        "Servers absent from the file keep placeholders. Values are never logged.",
    )
    d.add_argument(
        "--no-agent",
        action="store_true",
        help="Skip building the container image and creating the Foundry "
        "Hosted Agent. By default deploy hosts it so `chat --runtime hosted` "
        "is instant from the first ask; the local chat works either way.",
    )
    d.add_argument(
        "--model",
        default=None,
        metavar="MODEL",
        help="Model/deployment to deploy & use; the provider is auto-detected "
        "from the name -- e.g. gpt-5-mini (first-party Azure OpenAI, the cheap "
        "test path) or claude-sonnet-4-6 (Claude on Foundry, production). "
        "Default: the provider's preferred deployment.",
    )
    d.add_argument(
        "--provider",
        choices=_PROVIDER_CHOICES,
        default="auto",
        help="Model provider (default auto -- detected from --model/CHKP_MODEL). "
        "anthropic = Claude on Foundry (production); azure-openai = first-party "
        "gpt-5-mini (cheap test; deploys on MSDN/Dev-Test subscriptions where "
        "Claude is blocked). Persisted in the azd env for later commands.",
    )
    d.add_argument(
        "--org",
        default=None,
        metavar="NAME",
        help="Organization name sent to Anthropic with the Claude model "
        "deployments (Bicep modelProviderData -- auto-accepts Anthropic's "
        "commercial terms). Required for the anthropic provider; NOT needed for "
        "gpt-5-mini (first-party). Also honors the CLAUDE_ORGANIZATION_NAME env "
        "var; remembered in the azd environment after the first deploy.",
    )
    d.add_argument(
        "--guardrail",
        action="store_true",
        help="Bake Azure AI Content Safety Prompt Shields screening into the "
        "hosted agent (persists CHKP_GUARDRAIL=enforce in the azd env, so the "
        "agent version screens and BLOCKS every prompt before the model). Also "
        f"honors {ENV_GUARDRAIL}=1. For log-only hosted screening use `azd env "
        "set CHKP_GUARDRAIL observe` instead; either way re-run deploy/refresh.",
    )
    d.add_argument(
        "--remote-mcp",
        action="store_true",
        help="Also stand up the OPT-IN remote MCP tier (the AWS AgentCore-"
        "Gateway analogue): each selected @chkp server as its own scale-to-zero "
        "Azure Container App in streamable-HTTP mode behind Entra Easy Auth, so "
        "a SECOND consumer (Foundry portal agents, Copilot Studio, Claude "
        "Desktop, other MCP clients) can use the same tools -- not only this "
        "agent's stdio children. The stdio path stays the default; `destroy` "
        "tears the tier down.",
    )
    d.add_argument(
        "--guardrail-provider",
        choices=["content-safety", "lakera"],
        default=None,
        help="Which guardrail engine screens prompts: 'content-safety' (Azure AI "
        "Content Safety Prompt Shields, the platform default) or 'lakera' (the "
        "Check Point AI Guardrail / Lakera Guard -- one inline API call, identical "
        "on AWS and Azure). For lakera, set LAKERA_API_KEY / LAKERA_PROJECT_ID in "
        "your environment (stored in the <prefix>-lakera-guard Key Vault secret at "
        "deploy). Persisted as CHKP_GUARDRAIL_PROVIDER; also honors that env var.",
    )

    c = sub.add_parser(
        "chat",
        parents=[common],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        help="Ask the Check Point security-ops agent (Claude on Foundry) a question",
        epilog=CHAT_EXAMPLES,
    )
    c.add_argument(
        "task",
        nargs="?",
        default=None,
        help='Natural-language question about your estate, e.g. '
        '"how many hosts are configured?". Run `chat` with no task to see examples.',
    )
    c.add_argument(
        "--runtime",
        choices=["local", "hosted"],
        default=None,
        help="hosted: send the task to the Foundry Hosted Agent's Responses "
        "endpoint (Entra ID bearer auth). local: run the loop in this process. "
        "Default: hosted when this stack deployed a hosted agent (chat asks "
        "YOUR deployed agent, AWS-parity), local otherwise.",
    )
    c.add_argument(
        "--model",
        default=None,
        metavar="MODEL",
        help="Model/deployment name to use; the provider is auto-detected from "
        "the name -- e.g. gpt-5-mini (Azure OpenAI test) or claude-haiku-4-5 "
        "(Claude). Default: auto-select the best deployment this stack can "
        f"actually call. Also honors the {ENV_MODEL} env var.",
    )
    c.add_argument(
        "--provider",
        choices=_PROVIDER_CHOICES,
        default="auto",
        help="Model provider (default auto -- detected from --model/CHKP_MODEL). "
        "Set explicitly to override detection; otherwise a deployed stack's own "
        "provider is used.",
    )
    c.add_argument(
        "--session",
        default=None,
        metavar="ID",
        help="Conversation id. With --runtime hosted the platform keeps the "
        "conversation history, so follow-up questions recall earlier turns. "
        "Omit for a stateless one-shot.",
    )
    c.add_argument(
        "--actor",
        default=None,
        metavar="ID",
        help=f"Whose session to use (default: {DEFAULT_ACTOR}).",
    )
    c.add_argument(
        "--guardrail",
        action="store_true",
        help="Screen the prompt with Azure AI Content Safety Prompt Shields "
        "BEFORE any model call; a detected attack blocks the run.",
    )
    c.add_argument(
        "--servers",
        help="Override which @chkp servers the LOCAL runtime spawns for this "
        "run (names or 'all'). The hosted runtime always uses the set it was "
        "deployed with.",
    )

    s = sub.add_parser(
        "status",
        parents=[common],
        help="Read-only health check: azd outputs, Foundry account, Claude "
        "deployments (with a live probe), Key Vault secrets, image, hosted "
        "agent, Prompt Shields, local toolchain",
    )
    s.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Print the check results as JSON instead of the live UI.",
    )
    s.add_argument(
        "--tools",
        action="store_true",
        dest="with_tools",
        help="Also spawn the @chkp servers and report per-server tool counts "
        "(end-to-end proof the tool catalog resolves). Slower -- it cold-starts "
        "npx children -- and needs the 'mcp' extra; off by default.",
    )

    doc = sub.add_parser(
        "doctor",
        parents=[common],
        help="Local preflight: az, azd, docker, node, python3 versions and "
        "subscription readiness -- checks only, changes nothing",
    )
    # Provider-aware: doctor's subscription-eligibility gate differs per provider
    # (Claude needs a billable sub; gpt-5-mini is first-party). Accept the same
    # --provider/--model as deploy/chat so a standalone `doctor` can be pointed at
    # the gpt path explicitly; absent a flag it falls back to the deployed stack's
    # persisted CHKP_PROVIDER (see the persisted re-read in _main).
    doc.add_argument(
        "--model",
        default=None,
        metavar="MODEL",
        help="Model/deployment to preflight for; the provider is auto-detected "
        "from the name -- e.g. gpt-5-mini (Azure OpenAI test) or claude-sonnet-4-6 "
        "(Claude). Also honors the CHKP_MODEL env var.",
    )
    doc.add_argument(
        "--provider",
        choices=_PROVIDER_CHOICES,
        default="auto",
        help="Provider to preflight for (default auto -- detected from "
        "--model/CHKP_MODEL, else the deployed stack's persisted provider). "
        "anthropic gates on Claude eligibility; azure-openai on gpt-5-mini quota.",
    )

    sub.add_parser(
        "refresh",
        parents=[common],
        help="Bump the hosted agent to a new version so its sandboxes restart "
        "and re-read the Key Vault secrets -- run after changing credentials",
    )

    mo = sub.add_parser(
        "models",
        parents=[common],
        help="Manage the active provider's preferred model deployments "
        "(Claude, or gpt-5-mini on an azure-openai stack): status (read-only "
        "presence + live probe), enable (ensure they exist -- deploy owns "
        "creation), disable (remove the deployments this stack created)",
    )
    mo.add_argument(
        "action",
        choices=["status", "enable", "disable"],
        help="status (per-model presence + callable-now probe) | enable (ensure "
        "the deployments exist; Bicep-owned, so re-run deploy if missing) | "
        "disable (delete the active provider's deployments this stack created)",
    )

    cr = sub.add_parser(
        "creds",
        parents=[common],
        help="Manage Check Point credentials from a local gitignored .env file: "
        "write the per-server Key Vault secrets and refresh the hosted agent",
    )
    cr.add_argument(
        "action",
        choices=["template", "apply"],
        help="template (write a starter creds file for your deployed servers) | "
        "apply (write the secrets from the file + refresh the hosted agent)",
    )
    cr.add_argument(
        "--file",
        default=None,
        help="Credentials file path (default chkp-credentials.env).",
    )

    g = sub.add_parser(
        "guardrail",
        parents=[common],
        help="[optional demo] Azure AI Content Safety Prompt Shields "
        "-- NOT Check Point runtime protection (that integration is Early Access)",
        description="OPTIONAL DEMO -- Prompt Shields screening on the deployed "
        "AIServices account (no extra infra). `test` sends a benign prompt and "
        "then a prompt-injection payload and reports allow/deny per case. This "
        "is NOT Check Point runtime protection: that integration is Early "
        "Access -- contact Check Point to join.",
    )
    g.add_argument(
        "action",
        choices=["provision", "enforce", "test", "verify", "destroy"],
        help="test (benign + injection prompts, report the shieldPrompt "
        "decisions) | verify (read-only: shieldPrompt reachable) | provision / "
        "enforce / destroy (Prompt Shields ships with the account -- these "
        "report state and steer the CHKP_GUARDRAIL mode: off/observe/enforce)",
    )
    g.add_argument(
        "--enforce",
        action="store_true",
        help="With 'provision': also point at how to flip screening to ENFORCE "
        "(block on detection) for the hosted agent.",
    )

    b = sub.add_parser(
        "bridge",
        parents=[common],
        help="HTTPS front door for the hosted agent: a bearer-token Azure "
        "Function any client can call (Teams via Power Automate, n8n, curl) "
        "without an Entra token. The token lives only in Key Vault.",
    )
    b.add_argument(
        "action",
        choices=["provision", "show", "destroy"],
        help="provision (create/refresh the Function + token) | show (print the "
        "URL + curl example) | destroy (remove the Function + storage)",
    )
    b.add_argument(
        "--reveal-token",
        action="store_true",
        help="With show: also print the bearer token (otherwise only the "
        "az keyvault command to fetch it).",
    )

    t = sub.add_parser(
        "destroy",
        parents=[common],
        help="Destroy the stack: hosted agent (data plane), Claude model "
        "deployments, then azd down --force --purge",
    )
    t.add_argument(
        "--force-delete-secret",
        action="store_true",
        help="Also purge soft-deleted Key Vault secrets if the vault survives "
        "(default: Key Vault soft delete keeps them recoverable for 7 days).",
    )
    t.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Skip the confirmation prompt (required when not running in a terminal).",
    )
    return parser


def main(argv=None):
    try:
        return _main(argv)
    except KeyboardInterrupt:
        log("\nInterrupted. Every command is idempotent -- re-run it to continue.")
        return 130
    except Exception as e:  # noqa: BLE001 -- last-resort credential guard
        # Entra tokens are minted lazily (DefaultAzureCredential resolves on
        # the first data-plane call; az/azd fail mid-run when their cached
        # login expires), so a command can outlive its session long after any
        # preflight -- without this guard that would dump a raw traceback.
        if not _is_credential_error(e):
            raise
        log("Your Azure session has expired or credentials are unavailable.")
        log("Log in again (az login, or azd auth login), then re-run the same")
        log("command -- every command here is idempotent, so re-running is safe.")
        log(f"  ({type(e).__name__}: {str(e)[:160]})")
        return 1


def _is_credential_error(exc):
    """True for the credential-expiry/missing shapes Azure raises lazily:
    azure-identity's CredentialUnavailableError, azure-core's
    ClientAuthenticationError, and az/azd subprocess failures whose stderr
    mentions expired/login/reauthenticate. Pure (unit-tested); real bugs
    (ValueError, RuntimeError, ...) return False and re-raise in main()."""
    from .azutil import AzCliError

    try:
        from azure.identity import CredentialUnavailableError
    except ImportError:  # pragma: no cover
        CredentialUnavailableError = ()
    try:
        from azure.core.exceptions import ClientAuthenticationError
    except ImportError:  # pragma: no cover
        ClientAuthenticationError = ()
    if CredentialUnavailableError and isinstance(exc, CredentialUnavailableError):
        return True
    if ClientAuthenticationError and isinstance(exc, ClientAuthenticationError):
        return True
    if isinstance(exc, AzCliError):
        s = str(exc).lower()
        return ("expired" in s or "login" in s or "reauthenticate" in s
                or "credential" in s or "aadsts" in s)
    return False


def _main(argv=None):
    # Auto-load a local .env (gitignored) so guardrail creds like LAKERA_API_KEY
    # are picked up without a manual export -- explicit env vars still win.
    load_env_file()
    parser = _build_parser()
    args = parser.parse_args(argv)

    # No subcommand -> show the full help (friendlier than argparse's terse
    # `usage: ... error: the following arguments are required: cmd`).
    if not getattr(args, "cmd", None):
        parser.print_help()
        return 0

    # Global options may live on the root parser or the subparser (SUPPRESS
    # defaults mean the attribute is absent when never passed).
    location = getattr(args, "location", DEFAULT_LOCATION)
    prefix = getattr(args, "prefix", DEFAULT_PREFIX)
    subscription = getattr(args, "subscription", None)
    if getattr(args, "plain", False):
        from . import ui

        ui.FORCE_PLAIN = True

    try:
        validate_prefix(prefix)
        if location not in SUPPORTED_LOCATIONS:
            raise ValueError(
                f"unsupported location {location!r} -- Claude + Foundry Hosted "
                f"Agents co-exist only in: {', '.join(SUPPORTED_LOCATIONS)}"
            )
        spec = getattr(args, "servers", None) or os.environ.get(ENV_SERVERS)
        servers_explicit = bool(spec)
        servers = parse_servers(spec)
    except ValueError as e:
        log(f"Invalid options: {e}")
        return 2

    cfg = StackConfig(
        prefix=prefix,
        location=location,
        subscription_id=subscription,
        servers=tuple(servers),
    )

    # Commands that ACT ON an already-deployed stack (re-version the hosted
    # agent, or verify its secrets) must honor the server set the stack was
    # deployed with -- persisted in the azd env as CHKP_SERVERS -- unless the
    # user explicitly overrode it with --servers/CHKP_SERVERS. Without this,
    # `refresh`/`creds apply`/`status` fall back to the 9 defaults and silently
    # drop or add servers relative to what `deploy --servers ...` provisioned.
    if getattr(args, "cmd", None) in ("status", "refresh", "creds") and not servers_explicit:
        from . import azutil

        persisted = azutil.azd_env_values(prefix).get(ENV_SERVERS)
        if persisted:
            try:
                cfg = StackConfig(
                    prefix=prefix,
                    location=location,
                    subscription_id=subscription,
                    servers=tuple(parse_servers(persisted)),
                )
            except ValueError:
                pass  # a bad persisted value never blocks the command

    # -- provider selection (single source of truth: config.resolve_provider) --
    # An explicit --provider (non-'auto') or CHKP_PROVIDER wins; otherwise the
    # provider is detected from the model name (--model / CHKP_MODEL). This
    # stamps cfg.provider so every downstream path (hydrate_config, agent loop,
    # verify, deploy) uses the right SDK/wire format via get_provider().
    explicit = getattr(args, "provider", None)
    model_hint = getattr(args, "model", None)
    # CHKP_MODEL is an explicit provider signal too: resolve_provider() below
    # detects the family from it (gpt-5-mini -> azure-openai), so it MUST also
    # suppress the persisted-provider re-read -- otherwise a deployed Claude
    # stack's persisted CHKP_PROVIDER=anthropic would clobber the gpt provider
    # the user selected via CHKP_MODEL, sending a gpt deployment down the Claude
    # endpoint/scope. (--model/--provider/CHKP_PROVIDER already count.)
    provider_explicit = (
        explicit not in (None, "auto")
        or bool(model_hint)
        or bool(os.environ.get(ENV_PROVIDER))
        or bool(os.environ.get(ENV_MODEL))
    )
    try:
        resolved = resolve_provider(
            explicit if explicit not in (None, "auto") else os.environ.get(ENV_PROVIDER),
            model_hint or os.environ.get(ENV_MODEL),
        )
    except ValueError as e:
        log(f"Invalid options: {e}")
        return 2

    # Commands that ACT ON an already-deployed stack honor the provider it was
    # deployed with (persisted in the azd env as CHKP_PROVIDER) unless the user
    # explicitly overrode it -- mirroring the CHKP_SERVERS re-read above, so
    # `chat`/`status` against a deployed gpt stack pick gpt-5-mini automatically.
    if not provider_explicit and getattr(args, "cmd", None) in (
            "deploy", "chat", "status", "refresh", "creds", "models", "doctor"):
        from . import azutil

        persisted_provider = azutil.azd_env_values(prefix).get(ENV_PROVIDER)
        if persisted_provider:
            try:
                resolved = resolve_provider(persisted_provider, None)
            except ValueError:
                pass  # a bad persisted value never blocks the command

    cfg = dataclasses.replace(cfg, provider=resolved)

    if args.cmd == "deploy":
        from . import deploy as deploy_mod

        rc = deploy_mod.run_deploy(
            cfg,
            creds_file=getattr(args, "creds", None),
            include_agent=not getattr(args, "no_agent", False),
            org=getattr(args, "org", None),
            guardrail=getattr(args, "guardrail", False),
            model=getattr(args, "model", None),
            remote_mcp=getattr(args, "remote_mcp", False),
            guardrail_provider=getattr(args, "guardrail_provider", None),
        )
        if rc != 0:
            log(f"\nDeploy reported failures (exit {rc}).")
            return rc
        log("\nStatus  : python3 -m chkpmcpaz status")
        log('Ask     : python3 -m chkpmcpaz chat "how many hosts are configured?"')
        return 0

    if args.cmd == "chat":
        if not args.task:
            log("Give the agent a question, e.g.:\n")
            log('  python3 -m chkpmcpaz chat "how many hosts are configured?"\n')
            log(CHAT_EXAMPLES)
            return 2
        return _chat(cfg, args)

    if args.cmd == "status":
        from . import verify

        return verify.run_status(cfg, as_json=getattr(args, "as_json", False),
                                 with_tools=getattr(args, "with_tools", False))

    if args.cmd == "doctor":
        from . import doctor

        return doctor.run_doctor(cfg)

    if args.cmd == "refresh":
        from . import hosting

        return hosting.run_refresh(cfg)

    if args.cmd == "models":
        from . import models

        return models.run_models(cfg, args.action)

    if args.cmd == "creds":
        from . import creds

        if args.action == "template":
            return creds.run_template(cfg, path=args.file)
        return creds.run_apply(cfg, path=args.file)

    if args.cmd == "guardrail":
        from . import azutil, guardrail

        env = azutil.azd_env_values(cfg.prefix)
        cfg = azutil.hydrate_config(cfg, env)
        # test/verify exercise the data path and need a deployed endpoint;
        # provision/enforce/destroy are honest state+guidance reports that work
        # with or without a deployed stack.
        if args.action in ("test", "verify") and not cfg.content_safety_endpoint:
            log(f"No deployed stack found for prefix '{cfg.prefix}' -- the "
                "guardrail screens through the stack's AIServices account.")
            log("Deploy first: python3 -m chkpmcpaz deploy")
            return 1
        return guardrail.run_guardrail(cfg, args.action,
                                       enforce=getattr(args, "enforce", False))

    if args.cmd == "bridge":
        from . import bridge

        return bridge.run_bridge(cfg, args.action,
                                 reveal_token=getattr(args, "reveal_token", False))

    if args.cmd == "destroy":
        from . import destroy as destroy_mod

        return destroy_mod.run_destroy(
            cfg,
            yes=args.yes,
            force_delete_secret=args.force_delete_secret,
        )

    return 2  # unreachable: argparse enforces the choices


def _resolve_runtime(explicit, env):
    """Where `chat` runs when --runtime is not given: hosted when THIS stack
    deployed a hosted agent (deploy persists CHKP_AGENT_HOSTED) and the stack
    outputs are present -- so a plain `chat "..."` asks YOUR deployed agent,
    exactly like the AWS app -- otherwise local. An explicit flag always wins.
    Pure so it is unit-testable."""
    if explicit:
        return explicit
    if (env.get("CHKP_AGENT_HOSTED") == "true"
            and env.get("FOUNDRY_PROJECT_ENDPOINT")):
        return "hosted"
    return "local"


def _chat(cfg, args):
    """The `chat` command: hydrate the stack endpoints, then run the agent
    loop locally or invoke the Foundry Hosted Agent (fail-fast + honest error
    reporting, mirroring the AWS repo's commit 8dd198f)."""
    from . import azutil

    env = azutil.azd_env_values(cfg.prefix)
    cfg = azutil.hydrate_config(cfg, env)

    runtime = _resolve_runtime(args.runtime, env)
    if runtime == "hosted" and not args.runtime:
        log("runtime: hosted -- this stack deployed a hosted agent "
            "(use --runtime local to run the loop in this process)")

    if runtime == "hosted":
        from . import hosting

        if args.servers:
            log("note: --servers is a local-runtime option; the hosted agent "
                "uses the server set it was deployed with.")
        if args.guardrail:
            log("note: --guardrail screening is a local-runtime feature; the "
                "hosted agent screens per its CHKP_GUARDRAIL deploy setting.")
        return hosting.chat_hosted(cfg, env, args.task, session=args.session)

    if not cfg.model_base_url:
        log(f"No deployed stack found for prefix '{cfg.prefix}' (no azd "
            "environment outputs).")
        log("Deploy first: python3 -m chkpmcpaz deploy")
        return 1

    from . import agent, guardrail

    # Guardrail mode for THIS run: the --guardrail flag forces enforce; failing
    # that, the CHKP_GUARDRAIL env var decides (observe = screen-and-report but
    # never block; enforce = block on detection). Enforce screening runs inside
    # run_task (guardrail=True); observe is handled here so run_task never
    # blocks on it.
    mode = guardrail.resolve_mode(os.environ.get(ENV_GUARDRAIL), flag=args.guardrail)
    if mode == guardrail.GUARDRAIL_OBSERVE:
        detected = label = None
        try:
            # Provider-aware (matches enforce + the hosted observe path): reports
            # with the SAME engine CHKP_GUARDRAIL_PROVIDER selects, not a hardcoded one.
            detected, label, _ = guardrail.screen_prompt(cfg, args.task)
        except Exception as e:  # noqa: BLE001 -- observe must never fail the run
            if _is_credential_error(e):
                raise
            log(f"guardrail (observe): screening unavailable "
                f"({type(e).__name__}); continuing without a verdict")
        if detected is True:
            log(f"guardrail (observe · {label}): attack DETECTED -- would block in "
                "enforce mode; continuing (log-only)")
        elif detected is False:
            log(f"guardrail (observe · {label}): screening passed")

    model = args.model or os.environ.get(ENV_MODEL) or None
    try:
        result = agent.run_task(
            args.task,
            cfg,
            model=model,
            guardrail=(mode == guardrail.GUARDRAIL_ENFORCE),
            session=args.session,
            actor=args.actor or DEFAULT_ACTOR,
        )
    except agent.GuardrailBlocked as gb:
        # A block is the guardrail doing its job -- present it as a security win
        # (green, uses the REAL engine label from the exception, not a hardcoded
        # one), and exit 0: the tool worked exactly as intended.
        from . import ui

        for line in guardrail.blocked_lines(str(gb)):
            log(ui.err(line))       # red = blocked/deny (firewall-style); still exit 0
        return 0
    except agent.ModelUnavailable as e:
        _model_denied_help(cfg, str(e))
        return 1
    except Exception as e:  # noqa: BLE001 -- classify model-access denials; re-raise the rest
        api = agent.first_api_error(e)
        if agent.is_model_access_error(e) and not _is_credential_error(api):
            _model_denied_help(
                cfg, f"deployment {model or cfg.configured_deployment or '(auto)'!r} "
                f"was refused ({type(api).__name__})")
            return 1
        raise
    return 1 if result.error else 0


def _model_denied_help(cfg, reason):
    """Print actionable next-best-model guidance when a model deployment is
    denied: which deployments this identity CAN call right now (re-probed) plus
    the models/deploy hints. Provider-aware -- probes the ACTIVE provider's
    deployments. Mirrors the AWS `_model_denied` guidance."""
    from . import providers

    log(f"No callable deployment: {reason}")
    can = []
    try:
        prov = providers.get_provider(cfg.provider)
        can = prov.callable_deployments(prov.make_client(cfg))
    except Exception as e:  # noqa: BLE001 -- guidance is best-effort
        if _is_credential_error(e):
            raise
    if can:
        log("Deployment(s) this identity CAN call right now: " + ", ".join(can))
        log(f'Retry with one: python3 -m chkpmcpaz chat --model {can[0]} "<task>"')
    else:
        log("No preferred deployment answered a probe with this identity.")
    log("Full model-access report: python3 -m chkpmcpaz models status")
    log("Provision/repair the deployments: python3 -m chkpmcpaz deploy")


if __name__ == "__main__":
    sys.exit(main())
