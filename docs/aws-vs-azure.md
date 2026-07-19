# AWS vs Azure -- what changed in the port

This repo (`chkpmcpaz`) is the Azure mirror of
[checkpoint-mcp-on-aws-agentcore](https://github.com/alshawwaf/checkpoint-mcp-on-aws-agentcore)
(`chkpmcpaws`). The agent is the same: identical system prompt (verbatim),
identical server catalog and pins, identical `<server>___<tool>` namespacing,
identical 12-turn loop with 6,000-char tool-result truncation, identical
telemetry line, identical grounded-answer discipline. What changed is the
platform underneath it.

## Architecture deltas

| Concern | AWS (`chkpmcpaws`) | Azure (`chkpmcpaz`) |
|---|---|---|
| MCP server hosting | One container per server on AgentCore Runtime, aggregated behind an AgentCore Gateway (`chkp-mcp-gw`) | **stdio child processes** spawned by the agent itself (`npx -y @chkp/<server>-mcp@<pin>`) -- no gateway tier |
| Tool discovery | Gateway paginated `tools/list` across targets | Per-child `tools/list`, merged + namespaced in `mcp_stdio.py` |
| Inbound auth | Cognito user pool + M2M client + hosted domain (JWT) | **Entra ID everywhere**; the hosted agent's Responses endpoint is Foundry-authenticated |
| Outbound auth to tools | Gateway SigV4 via a service role | Not applicable -- children are local processes of the agent |
| Model | Claude on Amazon Bedrock (Converse/ConverseStream), **Amazon Nova** as the cheap test model | **Multi-provider**: Claude on Foundry (`AnthropicFoundry`, `/anthropic` route, `claude-sonnet-4-6` → `claude-haiku-4-5`) for production, or first-party **Azure OpenAI `gpt-5-mini`** (classic `AzureOpenAI` client, OpenAI Chat Completions) as the cheap test model -- the analog of Nova |
| Model wire format | One Bedrock **Converse** API abstracts Claude and Nova -- the two-model switch is nearly free | A real **provider abstraction** (`providers.get_provider(cfg.provider)` → `AnthropicProvider` \| `AzureOpenAIProvider`) is required: Claude speaks **Anthropic Messages**, `gpt-5-mini` speaks **OpenAI Chat Completions** -- different SDKs, tool schemas, and streaming deltas behind one identical 12-turn loop |
| Model access | Bedrock agreement APIs + SSM marker (`/chkp/model-access`), revoke-on-destroy | Bicep-owned model **deployments** -- created on provision, die with the resource group. Claude uses `modelProviderData` to auto-accept Anthropic's terms (needs a pay-as-you-go sub + Marketplace); **`gpt-5-mini` is first-party** (`format: 'OpenAI'`, no `modelProviderData`, no `--org`), so it deploys on MSDN / Visual Studio Dev-Test subscriptions where Claude is blocked |
| Prompt caching | Converse `cachePoint` blocks (model-family-gated) | Anthropic `cache_control: {"type": "ephemeral"}` on the system prompt + last tool |
| Hosted runtime | AgentCore Runtime `chkp_agent` (HTTP contract, port 8080, ARM64) | Foundry Hosted Agent `chkpmcp-agent` (Responses protocol 2.0.0, port 8088, linux/amd64) |
| Container build | CodeBuild (ARM64) + S3 source + ECR | `az acr build` (remote) + ACR -- no local Docker needed |
| Secrets | Secrets Manager, one secret per server: `chkp/<server>` | Key Vault, one secret per server: `<prefix>-<server>` (KV names allow only `[0-9a-zA-Z-]`) |
| Secret refresh | `refresh` version-bumps every runtime | Local: implicit (secrets re-read at every child spawn). Hosted: `refresh` bumps the agent version to restart sandboxes |
| Guardrail (optional; MCP tools always present) | **Default = cloud-native:** AgentCore Policy + Bedrock Guardrails (Cedar) at a separate gateway demo (`--guardrail-provider gateway`). **Opt-in = Check Point AI Guardrail (Lakera Guard)** via `--guardrail-provider lakera` -- a client-side screen in the CLI, covering chat on either runtime. `guardrail {provision,enforce,test,verify,destroy}` | **Default = cloud-native:** Azure AI Content Safety **Prompt Shields** screening before the loop (`--guardrail-provider content-safety`; ships with the account). **Opt-in = the same Check Point AI Guardrail (Lakera Guard)** via `--guardrail-provider lakera` -- screens inline in local chat and, when baked at deploy, inside the hosted container. `guardrail {provision,enforce,test,verify,destroy}` (provision/enforce/destroy report state), `chat --guardrail`, `CHKP_GUARDRAIL` off/observe/enforce |
| Memory | AgentCore Memory (semantic store, `--session` + `--actor`) | Platform-managed Responses conversation history for the hosted runtime (`--session`); local runtime stateless in v0.1 |
| Bridge (Teams/Postman/curl) | Lambda + API Gateway bearer-token front (`bridge provision`) | **Azure Function** bearer-token front (`bridge provision`) -- a static-token HTTPS door for non-Azure callers; token in Key Vault (the Responses endpoint stays Entra-only for Azure-aware clients) |
| Infra tooling | Imperative boto3 (no IaC) | **Bicep + azd** (`azd provision` / `azd down`); the CLI orchestrates |
| Gaia elicitation | NOT relayed by the gateway -- calls hang; local-only | stdio **does** relay elicitation -- the agent-side answerer works; still excluded from default/`all` to mirror the catalog |
| Region | `us-east-1` default | `eastus2` default, `swedencentral` allowed -- the only two regions with BOTH Hosted Agents and Claude accounts (July 2026) |

The guardrail is optional and the customer's choice. The cloud-native engine
stays the default on each cloud (and differs per cloud -- Bedrock Guardrails on
AWS, Prompt Shields on Azure); Check Point AI Guardrail (Lakera Guard) is a
drop-in `--guardrail-provider lakera` opt-in that is identical on AWS and Azure
(one `POST api.lakera.ai/v2/guard` call) -- so customers already invested in
their cloud's native guardrail keep it, while customers who want one guardrail
story across both clouds pick Lakera. Neither is forced.

## Resource mapping

| AWS resource | Azure resource |
|---|---|
| Bedrock (Converse, Claude + Nova) | Foundry account `<prefix>-foundry-<token>` (kind `AIServices`) + Claude deployments `claude-sonnet-4-6` / `claude-haiku-4-5`, **or** the first-party `gpt-5-mini` deployment (cheap test path) |
| AgentCore Runtime `chkp_agent` | Foundry Hosted Agent `<prefix>-agent` |
| AgentCore Runtimes per server + Gateway `chkp-mcp-gw` + targets | (none) -- stdio child processes inside the agent |
| Cognito pool / client / resource server / domain | Entra ID (`DefaultAzureCredential`) + RBAC role assignments |
| Secrets Manager `chkp/<server>` | Key Vault secret `<prefix>-<server>` in `kv-<prefix>-<token>` |
| ECR `bedrock-agentcore-chkpmcp*` + CodeBuild `chkp-mcp-build*` + S3 `chkp-mcp-src-<account>` | ACR `acr<prefix><token>` + `az acr build` |
| IAM roles (`AgentCoreRuntimeChkpMcp`, `AgentCoreGatewayRole`, `ChkpMcpCodeBuild`, `AgentCoreAgentChkp`, …) | RBAC assignments: `Cognitive Services User`, `Key Vault Secrets Officer/User`, `AcrPush`/`AcrPull`, Foundry Project Manager |
| CloudWatch logs + metrics | Log Analytics `log-<prefix>-<token>` + App Insights `appi-<prefix>-<token>` |
| SSM `/chkp/model-access` marker | Not needed (deployments are Bicep-owned, destroyed with the stack) |
| AgentCore Memory `chkp_mcp_memory` | Platform-managed conversation history (hosted `--session`) |
| Lambda + API Gateway `chkp-agent-bridge` | Azure Function `func-<prefix>-bridge-<hash>` + storage; bearer token in Key Vault (`<prefix>-bridge-token`) |
| AgentCore Policy engine + Cedar policies | Content Safety Prompt Shields (`shieldPrompt`, api-version `2024-09-01`) |
| Check Point AI Guardrail (Lakera Guard) -- opt-in `--guardrail-provider lakera` | Same Check Point AI Guardrail (Lakera Guard) -- opt-in `--guardrail-provider lakera` (identical `POST api.lakera.ai/v2/guard` call on both clouds; key/project id in the Key Vault secret `<prefix>-lakera-guard`) |

## Command mapping

The CLI verbs are now identical across both ports (`deploy / chat / status / doctor / refresh / creds / models / bridge / guardrail / destroy`); the rows below note only where flags or behavior differ. On AWS the former `agent` / `verify` verbs remain as hidden, deprecated aliases.

| AWS | Azure | Notes |
|---|---|---|
| `python3 -m chkpmcpaws deploy` | `python3 -m chkpmcpaz deploy` | Azure adds `--org` (Anthropic terms attestation, Claude path only), `--provider {auto,anthropic,azure-openai}`, and `--model` (e.g. `gpt-5-mini` for the cheap test path); drops `--no-model-access` (no agreements to manage). |
| `chkpmcpaws chat "…"` | `chkpmcpaz chat "…"` | Verb now identical (`agent` remains a hidden, deprecated AWS alias); same flags shape, plus `--provider` / `--model` to run the identical loop on `gpt-5-mini` instead of Claude. `--runtime agentcore` → `--runtime hosted`. |
| `chkpmcpaws status` | `chkpmcpaz status` | Verb now identical (`verify` remains a hidden, deprecated AWS alias); same read-only philosophy, `--json` added on Azure. |
| `chkpmcpaws models {enable,status,disable}` | `chkpmcpaz models {status,enable,disable}` | Claude deployments are Bicep-owned: `status` probes, `enable` ensures they exist (re-run deploy if missing), `disable` deletes the ones this stack created. |
| `chkpmcpaws bridge {provision,show,destroy}` | `chkpmcpaz bridge {provision,show,destroy}` | Azure Function bearer-token front door for non-Azure callers (the Responses endpoint stays Entra-only). Token in Key Vault. |
| `chkpmcpaws guardrail {provision,enforce,test,verify,destroy}` | `chkpmcpaz guardrail {provision,enforce,test,verify,destroy}` | Prompt Shields ships with the AIServices account, so provision/enforce/destroy report state + steer the CHKP_GUARDRAIL mode (off/observe/enforce); test + verify exercise the data path. The guardrail is optional; the cloud-native engine is the default (`gateway` on AWS, `content-safety` on Azure). Both ports also accept `--guardrail-provider lakera` (or `CHKP_GUARDRAIL_PROVIDER=lakera`) to swap in Check Point AI Guardrail (Lakera Guard) -- one API call, identical on both clouds. |
| `chkpmcpaws refresh` | `chkpmcpaz refresh` | Same purpose; hosted-only on Azure (local re-reads per spawn). |
| `chkpmcpaws creds {template,apply}` | `chkpmcpaz creds {template,apply}` | Identical workflow and file format. |
| `chkpmcpaws destroy` | `chkpmcpaz destroy` | Same plan-confirm-teardown shape; Azure adds nothing to scope (`--tools-only`/`--guardrail-only` gone with the gateway). |
| `--region` / `--profile` | `--location` / `--subscription` | Platform-native equivalents. `--prefix` and `--plain` unchanged. |
| `chkpmcpaws doctor` | `chkpmcpaz doctor` | Both ports now ship `doctor`; the AWS check set differs (no `az`/`azd`/node -- boto3 identity + AgentCore-readiness + region), because the AWS MCP servers and image build run remotely. |

## Cheap test model: Amazon Nova ↔ `gpt-5-mini`

Both ports let you run the **identical** `@chkp` tool loop on a cheaper model
for everyday testing, then switch to Claude for production.

| | Cheap test model | Production model |
|---|---|---|
| **AWS (`chkpmcpaws`)** | Amazon Nova (Bedrock) | Claude (Bedrock) |
| **Azure (`chkpmcpaz`)** | `gpt-5-mini` (first-party Azure OpenAI) | Claude on Foundry |

The difference is what it takes to wire the second model in:

- **AWS gets it nearly free.** Bedrock's single **Converse** API speaks to both
  Claude and Nova with one request/response shape, so switching models is a
  model-id change.
- **Azure needs a real provider abstraction.** Claude on Foundry uses the
  **Anthropic Messages** API (`AnthropicFoundry`, `input_schema` tool shape,
  `tool_use` blocks, `cache_control`); `gpt-5-mini` uses **OpenAI Chat
  Completions** (`AzureOpenAI`, `function`/`parameters` tool shape,
  `tool_calls` with a `tool_call_id`, streamed `arguments` string fragments).
  `providers.get_provider(cfg.provider)` returns `AnthropicProvider` or
  `AzureOpenAIProvider`; each speaks its vendor's native wire format behind one
  identical loop. The provider is **auto-detected** from the model name
  (`gpt-*` / `o[0-9]*` / anything containing `openai` → `azure-openai`, else
  `anthropic`) or forced with `--provider {auto,anthropic,azure-openai}` /
  `CHKP_PROVIDER`; the default is `anthropic` (production).

Why `gpt-5-mini` is the right analog of Nova: it is a **first-party** Azure
OpenAI model (not a third-party Marketplace offer), so -- like Nova on Bedrock
-- it deploys with no separate terms attestation and **no `--org`**, and it
works on an **MSDN / Visual Studio Dev-Test subscription** (region `eastus2`,
`gpt-5-mini` `GlobalStandard` quota 2000) where Claude is blocked. Usage is
covered by Visual Studio subscription credits -- effectively free for testing.
See **[Test cheaply without Claude](../README.md#test-cheaply-without-claude-like-aws-nova)**
and the [quickstart](scenarios/quickstart.md#cheap-test-path-gpt-5-mini).

## Behavior kept identical (on purpose)

- The **system prompt**, verbatim, including all five grounding rules.
- Max 12 turns, 2048 max tokens, 6,000-char tool results, 1,000-char tool
  descriptions, streaming with `assistant  ` prefixes and `→ tool` / `✓ ok` /
  `✗ error` lines.
- The telemetry line: `tokens  … in · … out · … cache-read · …% of input from cache`.
- Fail-fast hosted preflight and honest in-agent error reporting (exit 1 on
  `error: true`, never a green "done" on a failed run).
- The friendly expired-credentials message + "every command is idempotent"
  reassurance (AWS commit `02b8c28`), re-worded for `az login` / `azd auth login`.
- Placeholder-first secrets, never-clobber-real-values, values never logged.
- The prompt-injection test payload used by `guardrail test`, verbatim.
- Pure-logic test suite + CI matrix on Python 3.11/3.12/3.13.

## Secret recovery: one honest difference

On AWS, `destroy` schedules each `chkp/<server>` secret with a 7-day recovery
window, and a rebuild inside the window restores real credentials
automatically. On Azure, *individually deleted* secrets get the vault's 7-day
soft delete (and deploy recovers a soft-deleted secret before writing), but a
full `destroy` **purges the vault** (`azd down --force --purge`) so redeploys
under the same names work -- meaning real credentials do NOT survive a full
destroy/redeploy round-trip. Keep your gitignored `chkp-credentials.env` and
re-apply with `deploy --creds` or `creds apply`.

## Remote / streamable-HTTP MCP endpoints -- opt-in `deploy --remote-mcp`

By default this port runs the `@chkp` servers as **stdio children** of the agent
(zero extra infra/cost, lowest latency) -- but then **only this agent can use
the tools**. When you need a **second consumer** (Foundry portal agents, Copilot
Studio, Claude Desktop, or any other MCP client), `deploy --remote-mcp` stands
up a remote tier **alongside** -- not replacing -- the stdio path. It is the
Azure analogue of the AWS **AgentCore Gateway**:

1. Each selected `@chkp` server runs as its own **Azure Container App** in the
   packages' native streamable-HTTP transport (`--transport http`), reusing the
   SAME agent image with a different command (`python -m chkpmcpaz.remote_server`)
   -- no second image is built. **Scale-to-zero** keeps idle cost ~0.
2. Every endpoint is fronted by **Entra Easy Auth** (`Return401` for anything
   without a valid token for the gateway app-registration audience) -- org
   policy: every endpoint authenticated. Credentials still come from **Key Vault
   via a managed identity** (AcrPull + Key Vault Secrets User); no secrets in the
   containers.
3. The endpoints are surfaced for the **Foundry project Toolbox** so portal-built
   agents can share the same tools. (APIM's AI-gateway MCP support is the heavier
   variant if org-wide governance / rate-limiting is ever required.)

CLI surface (built): `deploy --remote-mcp` provisions the app registration + the
Container Apps + role grants and persists the endpoint catalog; `destroy` tears
the tier down (the Container Apps go with the resource group; the Entra app
registration is deleted explicitly). Our own agent can consume the remote tier
too -- `CHKP_MCP_TRANSPORT=remote python3 -m chkpmcpaz chat "..."` -- which is the
same code path (`chkpmcpaz.mcp_remote.RemoteServerPool`) any other MCP client
uses. Full walkthrough: **[docs/scenarios/remote-mcp.md](scenarios/remote-mcp.md)**.

| Aspect | Stdio (default) | Remote (`--remote-mcp`) |
|---|---|---|
| Where servers run | child processes of the agent | one Container App per server |
| Who can use the tools | only this agent | this agent **+ any Entra-authed MCP client** |
| Auth surface | none (in-process) | Entra Easy Auth per endpoint |
| Idle cost | none | ~0 (scale-to-zero) |
| Credentials | Key Vault -> agent -> child env | Key Vault -> app managed identity -> server env |
| AWS analogue | (none -- AWS always uses the gateway) | AgentCore Gateway + per-server runtimes |
