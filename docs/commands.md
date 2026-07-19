# Command reference

One CLI, cross-platform: `python3 -m chkpmcpaz …` (or the `chkpmcpaz` console
script). Bare invocation prints the full help and exits 0.

## Global flags

Accepted **before or after** any subcommand (the subcommand position wins on
conflict):

| Flag | Meaning |
|---|---|
| `--subscription <id>` | Azure subscription to operate in. |
| `--location <loc>` | Azure location -- `eastus2` (default) or `swedencentral` only. |
| `--prefix <p>` | Stack prefix (default `chkpmcp`; pattern `^[a-z][a-z0-9-]{0,11}$`). Namespaces every resource for a parallel stack. |
| `--plain` | Force plain line output (no full-screen UI). Also automatic when output is piped, `NO_COLOR` is set, or `CHKP_UI=plain`. |
| `--version` | Print the package version. |

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Success. |
| `1` | Failure -- including a **partial** deploy (never reported as success) and a hosted run whose payload carried `error: true`. |
| `2` | Usage error: missing chat task, unknown server name. |
| `130` | Ctrl-C. Prints: `Interrupted. Every command is idempotent -- re-run it to continue.` |

Credential-shaped failures (expired `az`/`azd` session, unavailable
credentials) never produce a traceback. At any point in any command they print
exactly this and exit 1:

```
Your Azure session has expired or credentials are unavailable.
Log in again (az login, or azd auth login), then re-run the same
command -- every command here is idempotent, so re-running is safe.
  (<ExceptionType>: <message truncated to 160 chars>)
```

Non-credential exceptions still raise -- real bugs are never swallowed.

---

## `deploy`

Stand up the full stack. Idempotent -- re-run after any interruption.

| Flag | Meaning |
|---|---|
| `--provider {auto,anthropic,azure-openai}` | Which model provider to deploy. Default `auto` -- inferred from `--model` (a `gpt-*`/`o[0-9]*`/`*openai*` name → `azure-openai`, else `anthropic`). Env: `CHKP_PROVIDER` (wins over auto-detection). |
| `--model <model>` | Model/deployment to deploy & use; the provider is auto-detected from it -- e.g. `gpt-5-mini` (cheap first-party test) or `claude-sonnet-4-6` (production). Env: `CHKP_MODEL`. Omit for the default Claude deployments. |
| `--servers "<names or all>"` | Which `@chkp` servers to seed secrets for. Default: the 9 default servers. `all` = 11 (excludes `argos-erm`, `harmony-sase`, `workforce-ai`, `quantum-gaia`). Env: `CHKP_SERVERS`. |
| `--creds [file]` | Apply real credentials from a local INI at deploy time (no path → `chkp-credentials.env`). Servers absent from the file get placeholders. |
| `--no-agent` | Skip the container build and hosted-agent creation -- infra + secrets only. |
| `--org <name>` | *Claude path only.* Organization name for the Claude deployment's `modelProviderData` (sent to Anthropic with the terms acceptance). Env: `CLAUDE_ORGANIZATION_NAME`. Country/industry come from `CLAUDE_COUNTRY_CODE` (default `US`) and `CLAUDE_INDUSTRY` (default `technology`, lowercase). Not required (and not used) on the `azure-openai` path -- `gpt-5-mini` is first-party. |
| `--guardrail` | Bake screening into the hosted agent in **enforce** mode (persists `CHKP_GUARDRAIL=enforce` in the azd env, injected into the immutable agent version). For log-only hosted screening set `CHKP_GUARDRAIL=observe` (via `azd env set`) instead of the flag. The persisted value is normalized to `off`/`observe`/`enforce`. |
| `--guardrail-provider {content-safety,lakera}` | Which guardrail engine: Azure AI Content Safety Prompt Shields (default) or the **Check Point AI Guardrail (Lakera Guard)**. For `lakera`, `LAKERA_API_KEY` / `LAKERA_PROJECT_ID` from your environment are stored in the `<prefix>-lakera-guard` Key Vault secret and read by the hosted agent. Persisted as `CHKP_GUARDRAIL_PROVIDER`. |
| `--remote-mcp` | Also stand up the opt-in **remote MCP tier**: each selected `@chkp` server as its own scale-to-zero Azure Container App (streamable-HTTP) behind Entra Easy Auth, so a **second consumer** (Foundry portal agents, Copilot Studio, other MCP clients) can share the tools. Reuses the agent image; forces the image build even with `--no-agent`. The stdio path stays the default. See [remote-mcp](scenarios/remote-mcp.md). |

The chosen **provider/model**, the deployed **server set**, and the
**guardrail mode** are persisted in the azd environment (`CHKP_PROVIDER`,
`CHKP_MODEL`, `CHKP_SERVERS`, `CHKP_GUARDRAIL`), so later `chat` / `refresh` /
`creds apply` / `status` operate on what was actually deployed -- acting on a
deployed `gpt-5-mini` stack picks `gpt-5-mini` automatically.

### Cheap `gpt-5-mini` test deploy

`--model gpt-5-mini` (or `--provider azure-openai`) deploys the **first-party**
Azure OpenAI test path instead of Claude -- the Azure analog of Amazon Nova on
Bedrock. No `--org` is required. Point at a Dev-Test subscription:

```
python3 -m chkpmcpaz deploy --model gpt-5-mini --subscription <msdn-sub-id>
```

This provisions **only** the `gpt-5-mini` deployment (Bicep
`deployOpenAiModel=true`, `deployClaudeModels=false`), outputs `OPENAI_BASE_URL`
/ `OPENAI_MODEL_DEPLOYMENT`, and additionally grants the hosted-agent identity
`Cognitive Services OpenAI User` for inference. `gpt-5-mini` is first-party
(`format: 'OpenAI'`, version `2025-08-07`, `GlobalStandard`) -- no Marketplace
offer, no `modelProviderData`, no terms attestation -- so it deploys on
MSDN / Dev-Test subscriptions (region `eastus2`, `gpt-5-mini` quota 2000)
where Claude is blocked. See the
[quickstart cheap test path](scenarios/quickstart.md#cheap-test-path-gpt-5-mini).

Step order: doctor preflight (fatal failures abort before any mutation) →
`azd env new` + `azd env set` (including `CHKP_PROVIDER`/`CHKP_MODEL` and the
Bicep model gates -- `DEPLOY_CLAUDE_MODELS`/`DEPLOY_OPENAI_MODEL`) →
`azd provision --no-prompt` (Bicep infra including the model deployment(s) for
the chosen provider) → read outputs → seed per-server **placeholder** Key Vault
secrets (never overwriting real values; also the `<prefix>-quantum-gaia`
agent-side secret) → optional `--creds` apply → `az acr build` of
`chkp-agent:v1` (remote build, repo-root context) → hosted agent create + 100%
traffic routing + poll to `active` → grant the agent identity `Cognitive
Services User` (account) and `Key Vault Secrets User` (vault) -- **plus
`Cognitive Services OpenAI User` on the `azure-openai` path** → smoke test via
`status`. Any failed step makes the whole command exit 1 with the failure list
-- partial success is never reported as success.

## `destroy`

| Flag | Meaning |
|---|---|
| `--yes` / `-y` | Skip the confirmation prompt (required when stdin is not a TTY). |
| `--force-delete-secret` | Additionally purge soft-deleted secrets if the vault survives. |

Runs a **read-only inventory** first (resource group, hosted agent, Key Vault
secrets including soft-deleted, ACR images, Claude deployments), prints the
plan, asks `y/N`, then: delete the hosted agent (data plane) → `azd down
--force --purge` (purges Key Vault and Cognitive Services soft-deletes so an
immediate redeploy works) → report. On a clean subscription: `Nothing to
destroy.` Safe to re-run.

## `chat "<task>"`

Run the security-ops agent. With no task: exit 2 plus a catalog of example
questions.

| Flag | Meaning |
|---|---|
| `--runtime {local,hosted}` | `local` (default): the loop in your process, `@chkp` servers spawned locally. `hosted`: invoke the Foundry Hosted Agent's Responses endpoint. |
| `--provider {auto,anthropic,azure-openai}` | Which model provider to use. Default `auto` -- inferred from `--model` (or the persisted stack's `CHKP_PROVIDER`). Env: `CHKP_PROVIDER`. |
| `--model <model>` | Force a deployment name -- a Claude name (`claude-sonnet-4-6`, `claude-haiku-4-5`) or `gpt-5-mini`; the provider is auto-detected from the name. Default: auto-select the first deployment your identity can call (Sonnet preferred on the Claude path). Env: `CHKP_MODEL`. |
| `--session <id>` | Conversation continuity for the hosted runtime (platform-managed history). The local runtime is stateless in this version. |
| `--actor <id>` | Actor id (default `chkp-analyst`); sanitized to `[a-zA-Z0-9_-]`. |
| `--guardrail` | Screen the input **before** any model call; a detected attack is blocked and nothing reaches the model. Engine set by `--guardrail-provider` / `CHKP_GUARDRAIL_PROVIDER`: `content-safety` (default) or `lakera` (Check Point AI Guardrail). |
| `--guardrail-provider {content-safety,lakera}` | Which guardrail engine screens: Azure AI Content Safety Prompt Shields (default) or the **Check Point AI Guardrail (Lakera Guard)** — one inline `POST api.lakera.ai/v2/guard`, identical on AWS and Azure. For `lakera`, set `LAKERA_API_KEY` / `LAKERA_PROJECT_ID`. Also honors `CHKP_GUARDRAIL_PROVIDER`. |
| `--servers` | Restrict which servers this run spawns (local runtime). |

Set **`CHKP_MCP_TRANSPORT=remote`** to drive the run over the remote MCP tier
(`deploy --remote-mcp`) — the same agent loop, but tools are reached over the
Container Apps' Entra-authenticated streamable-HTTP endpoints instead of local
stdio children. Falls back to stdio with a note when no remote tier is deployed.

Loop behavior: max 12 turns; streamed output prefixed `assistant  `; per-tool
lines `→ tool <name> <args>` then `✓ ok` / `✗ error` with a preview; tool
results truncated to 6,000 chars and fed back to the model; transient model
errors retried 3 times with backoff; a mid-stream failure prints
`… model stream error — retrying …`; budget exhaustion prints
`stopped after 12 turns (turn budget reached)`. Every run ends with one
telemetry line:

```
tokens  12,345 in · 890 out · 11,200 cache-read · 91% of input from cache
```

Hosted-runtime honesty (mirrors the AWS behavior exactly):

- **Fail-fast preflight**: if the hosted agent does not exist, the command
  exits 1 in seconds with
  `No hosted agent '<name>' found -- deploy first: python3 -m chkpmcpaz deploy`
  and never starts a build.
- **In-agent errors are honored**: a successful HTTPS invoke whose payload
  says `error: true` prints `Hosted agent could not complete the task:` plus
  the message, notes that the runtime is up and this is a data-path issue
  (`python3 -m chkpmcpaz status`), and exits 1 -- never a green "done" on a
  failed run. A transport-level failure also exits 1 and points at the
  unaffected local runtime: `python3 -m chkpmcpaz chat "<task>"`.

## `status`

Read-only health of every component; safe to run repeatedly. `--json` for
machine output. Checks, each with specific remediation text: azd env +
outputs present (the required outputs are provider-aware -- `OPENAI_BASE_URL` /
`OPENAI_MODEL_DEPLOYMENT` on an `azure-openai` stack in place of the `CLAUDE_*`
pair) · Foundry account/project reachable · the active provider's model
deployments exist + an 8-token probe per preference entry (prints which models
are callable) · Key Vault reachable, per-server secret present with a
placeholder-or-real flag (names only, never values) · ACR image
`chkp-agent:v1` present · hosted agent exists and `active`, endpoint printed ·
Content Safety `shieldPrompt` reachable · local Node/npx present. Exit 1 if
anything fails.

`--tools` adds an end-to-end tool-catalog check: it spawns the configured
`@chkp` servers over stdio and reports per-server tool counts (proof the tools
actually resolve). Off by default because it cold-starts `npx` children
(slower, network) and needs the `mcp` extra.

## `models`

Manage the **active provider's** preferred model deployments (Bicep-owned --
created on `deploy`, removed on `destroy`). The command is **provider-aware**:
it reads the deployed stack's persisted `CHKP_PROVIDER`, so on a Claude
(`anthropic`) stack it manages the Claude deployments and on a `gpt-5-mini`
(`azure-openai`) stack it manages the `gpt-5-mini` deployment.

| Action | Meaning |
|---|---|
| `status` | Read-only: each preferred deployment's presence + a live 8-token probe (which are callable now), tagged "(deployed by this stack)". |
| `enable` | Ensure the preferred deployments exist. Bicep owns creation, so a missing one is reported with the `deploy` remedy -- never silently provisioned. |
| `disable` | Delete the preferred deployments this stack created (the revoke analogue). The rest of the stack is untouched. |

The preferred set follows the active provider: `claude-sonnet-4-6` /
`claude-haiku-4-5` on an `anthropic` stack, `gpt-5-mini` on an `azure-openai`
stack. So on a `gpt-5-mini` stack, `models status` reports and `models
disable` revokes the `gpt-5-mini` deployment -- not Claude.

## `doctor`

Local-only preflight (no Azure mutations): Python ≥ 3.11 · `az` installed and
logged in · `azd` ≥ 1.25.3 · `node`/`npx` ≥ 20 · location in the supported
set · model subscription eligibility · live model quota · the `mcp` and
`azure-ai-projects` extras importable (with `pip install "chkpmcpaz[mcp]"`-style
hints) · org-policy reminders. Warnings and hard failures are clearly
separated; exit 1 only on hard failures.

The eligibility and quota checks are **provider-aware**. On the `anthropic`
provider, `doctor` FAILs a subscription that cannot deploy Claude (CSP /
free-trial / credit-only) and reports the Claude `GlobalStandard` quota. On the
`azure-openai` provider it **skips** the credit-offer check entirely -- reporting
`gpt-5-mini is first-party (Azure OpenAI) -- MSDN/Dev-Test/credit
subscriptions can deploy it` -- and reports the `gpt-5-mini` `GlobalStandard`
quota (2000 in `eastus2`) as OK. `deploy` runs this preflight with the provider
it resolved from `--provider`/`--model`; to run `doctor` standalone against the
gpt path, set `CHKP_MODEL=gpt-5-mini` (or `CHKP_PROVIDER=azure-openai`) in the
environment.

## `refresh`

Bump the hosted agent's version so its sandboxes restart and re-read Key
Vault secrets. The local runtime never needs this -- it re-reads secrets at
every child spawn. `creds apply` triggers a refresh automatically.

## `creds template` / `creds apply`

| Flag | Meaning |
|---|---|
| `--file <path>` | The local INI file (default `chkp-credentials.env`, gitignored). |

`template` writes a starter file -- one `[<server>]` section per **deployed**
server with that server's credential keys as placeholders, plus a
`[quantum-gaia]` section -- and refuses to overwrite an existing file.
`apply` parses the INI (case-preserving, no `%` interpolation), skips unknown
/ empty / still-placeholder sections with printed reasons, writes each
section to its Key Vault secret, then triggers `refresh`. Values are never
printed.

## `guardrail {provision,enforce,test,verify,destroy}`

Azure AI Content Safety Prompt Shields. It ships with the deployed AIServices
account -- there is no separate policy gateway -- so the lifecycle verbs report
state and steer the screening mode; `test` and `verify` exercise the data path.

| Action | Meaning |
|---|---|
| `test` | Benign prompt then the canned injection payload; prints allow/deny per case. Exit 0 **iff** the benign prompt passes AND the injection is detected. |
| `verify` | Read-only: a single benign `shieldPrompt` call proves the endpoint answers and the caller holds the account-scope role. |
| `provision` | State report -- nothing to stand up. With `--enforce`, points at how to turn blocking on. |
| `enforce` | Explains the modes (off / observe / enforce) and how to enable ENFORCE for the hosted or local runtime. |
| `destroy` | State report -- Prompt Shields goes away with the stack (`destroy`). |

Screening modes (`CHKP_GUARDRAIL`, or `chat --guardrail` = enforce): `off`
(unset/`0`), `observe` (screen + report, never block), `enforce`
(`1`/`on`/`enforce`; block on detection). See
[the guardrail scenario](scenarios/guardrail.md).

## `bridge {provision,show,destroy}`

An optional bearer-token HTTPS front door for the hosted agent, so non-Azure
callers (Microsoft Teams via Power Automate, n8n, curl, webhooks) can invoke it
without minting an Entra token. It is one Azure Function (Consumption,
Linux/Python) that verifies a static token and forwards to the hosted agent
with its own managed identity. The token lives ONLY in a Key Vault secret
(`<prefix>-bridge-token`) -- never in code, config, or logs.

| Action | Flag | Meaning |
|---|---|---|
| `provision` | | Create/refresh the storage account, Function app (system identity + RBAC), the token secret, and the deployed handler. Idempotent. |
| `show` | `--reveal-token` | Print the URL + a curl example (and, with the flag, the bearer token itself). |
| `destroy` | | Remove the Function app + storage. |

Call it with `Authorization: Bearer <token>` and a JSON body
`{"prompt": "...", "session": "optional"}`. The Azure-aware path (Entra token
on the Responses endpoint) is unchanged and needs no bridge. See
[invoke from anywhere](scenarios/invoke-from-anywhere.md).

---

## Environment variables

| Var | Meaning |
|---|---|
| `CHKP_SERVERS` | Server list (same as `--servers`). |
| `CHKP_PREFIX` | Stack prefix. |
| `CHKP_PROVIDER` | Force the provider: `anthropic` (Claude, default) or `azure-openai` (`gpt-5-mini`). Same as `--provider`; wins over the model-name auto-detection. |
| `CHKP_MODEL` | Force a deployment name -- a Claude name (`claude-sonnet-4-6`) or `gpt-5-mini` (overrides the active provider's `*_MODEL_DEPLOYMENT`). The provider is inferred from the name unless `CHKP_PROVIDER` is set. |
| `CHKP_GUARDRAIL` | Screening mode: `1`/`enforce` (block), `observe` (screen + report, never block), unset/`0` (off). Honored by local `chat`; persisted at deploy (`deploy --guardrail` / `azd env set`) for the hosted agent. |
| `CHKP_GUARDRAIL_PROVIDER` | Guardrail engine: `content-safety` (default) or `lakera` (Check Point AI Guardrail). Persisted at deploy; also set per-run for `chat`. |
| `LAKERA_API_KEY` / `LAKERA_PROJECT_ID` | Check Point AI Guardrail (Lakera Guard) credentials, used when the provider is `lakera`. Every command auto-loads a gitignored local `.env` (explicit exports still win), so dropping them there is enough for `chat`; deploy also reads them from the environment and stores them in the `<prefix>-lakera-guard` Key Vault secret for the hosted agent. The older `LAKERA_GUARD_*` names are accepted as aliases. Optional `LAKERA_API_URL` overrides the Guard endpoint (default `https://api.lakera.ai/v2/guard`). |
| `CHKP_LOG_DIR` | Log dir override (default `~/.chkpmcpaz/logs/`). |
| `CHKP_UI` | `plain` \| `tui`. |
| `CLAUDE_ORGANIZATION_NAME`, `CLAUDE_COUNTRY_CODE`, `CLAUDE_INDUSTRY` | Anthropic terms attestation values, set into the azd env by `deploy`. |
| `CLAUDE_BASE_URL`, `CLAUDE_MODEL_DEPLOYMENT` | Claude path: the `/anthropic` route and deployment name (azd outputs; injected into the hosted container). |
| `OPENAI_BASE_URL`, `OPENAI_MODEL_DEPLOYMENT` | `gpt-5-mini` path: the Foundry account root (the classic `AzureOpenAI` client appends `/openai`) and the deployment name (azd outputs). Injected instead of the `CLAUDE_*` pair on an `azure-openai` stack. |
| `KEY_VAULT_URI`, `CONTENT_SAFETY_ENDPOINT` | Secret store and Prompt Shields endpoint (azd outputs). |
| `FOUNDRY_PROJECT_ENDPOINT` | Platform-injected inside the hosted container -- the `FOUNDRY_*` prefix is reserved, never set it yourself. |
| `AZURE_SUBSCRIPTION_ID`, `AZURE_LOCATION`, `AZURE_ENV_NAME` | Standard azd values. |
| `GAIA_GATEWAY_IP`, `GAIA_PORT`, `GAIA_USER`, `GAIA_PASSWORD`, `GAIA_ADDRESS` | Local env override for the quantum-gaia elicitation answerer. |
