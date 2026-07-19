# DESIGN -- Check Point MCP on Microsoft Foundry

Azure mirror of `checkpoint-mcp-on-aws-agentcore` (`chkpmcpaws` v0.2.0). This
document records the architecture decisions; **CONTRACT.md** is the binding
interface spec the five builder agents (infra, runtime, cli, tests, docs)
follow independently.

## 1. What it is

A demo/reference tool that:

1. Runs **15 of the 18 published Check Point `@chkp/*-mcp` MCP servers** as
   **stdio child processes** (`npx -y @chkp/<server>-mcp@<pin>`) spawned by the
   agent process itself -- locally for `chat --runtime local`, or inside the
   Foundry Hosted Agent container for the hosted runtime. Tools are namespaced
   `<server-no-hyphens>___<tool>` (e.g. `quantummanagement___show_hosts`),
   identical to the AWS gateway namespacing.
2. Ships a **multi-provider security-operations agent** (plain tool-use loop,
   no agent framework), packaged as a **Foundry Hosted Agent** (Responses
   protocol, port 8088, linux/amd64) and also runnable in-process. A provider
   seam (`providers.get_provider(cfg.provider)` → `AnthropicProvider` |
   `AzureOpenAIProvider`) runs the **identical** loop on either **Claude on
   Foundry** via the `AnthropicFoundry` client (Anthropic Messages;
   production, default) or a first-party **Azure OpenAI `gpt-5-mini`** via the
   classic `AzureOpenAI` client (OpenAI Chat Completions; the cheap test
   model, Azure's analog of Amazon Nova on Bedrock). The provider is
   auto-detected from the model name or forced via `--provider` /
   `CHKP_PROVIDER`.
3. Wraps everything in one cross-platform CLI: `python3 -m chkpmcpaz` with
   `deploy / destroy / chat / status / doctor / refresh / models / creds /
   guardrail / bridge`. The Check Point MCP servers above are always present;
   an inline prompt-injection/jailbreak **guardrail is optional** and the
   customer's free choice of engine -- the cloud-native Azure AI Content Safety
   Prompt Shields (default) or the **Check Point AI Guardrail (Lakera Guard)**
   opt-in (`--guardrail-provider lakera` / `CHKP_GUARDRAIL_PROVIDER`), the same
   engine and single API call used on AWS. Neither is forced; customers already
   on Content Safety keep it.

## 2. Key architectural deltas vs. AWS

| Concern | AWS (`chkpmcpaws`) | Azure (`chkpmcpaz`) |
|---|---|---|
| MCP server hosting | 1 container per server on AgentCore Runtime, aggregated behind AgentCore Gateway | stdio child processes inside the agent process/container (no gateway tier) |
| Tool discovery | gateway paginated `tools/list` across targets | per-child `tools/list`, merged + namespaced in `mcp_stdio.py` |
| Inbound auth | Cognito M2M JWT on the gateway | Entra ID everywhere; hosted agent invoked via `azure-ai-projects` + `Foundry Agent Consumer` role |
| Gaia elicitation | NOT relayed by the gateway (calls hang; excluded) | stdio DOES relay elicitation -- the agent-side answerer works; still excluded from default/'all' to mirror the catalog |
| Infra tooling | imperative boto3 (no IaC) | **Bicep + azd** (`azd provision`/`azd down`), CLI orchestrates |
| Model / wire format | one Bedrock **Converse** API abstracts Claude + Nova (two-model switch nearly free) | a real **provider abstraction** -- Claude speaks Anthropic Messages, `gpt-5-mini` speaks OpenAI Chat Completions (different SDKs, tool schemas, streaming deltas) behind one identical loop |
| Model access | Bedrock agreement APIs + SSM marker | Bicep-owned model **deployments** (create on provision, die with the RG on `azd down`). Claude uses `modelProviderData` to auto-accept Anthropic terms (needs pay-as-you-go + Marketplace); `gpt-5-mini` is first-party (`format: 'OpenAI'`, no `modelProviderData`, no `--org`) so it deploys on MSDN/Dev-Test subs where Claude is blocked |
| Prompt cache | Converse `cachePoint` blocks | Anthropic `cache_control: {"type": "ephemeral"}` on system prompt + last tool |
| Guardrail (optional; MCP servers are always present) | default: AgentCore Policy + Bedrock Guardrails (Cedar); opt-in **Check Point AI Guardrail (Lakera Guard)** via `--guardrail-provider lakera` | provider seam, **cloud-native is the default**: Azure AI Content Safety **Prompt Shields** (`shieldPrompt`, screening before the loop; off/observe/enforce) is the default engine; **Check Point AI Guardrail (Lakera Guard)** is a drop-in opt-in (`--guardrail-provider lakera` at deploy / `CHKP_GUARDRAIL_PROVIDER`, Check Point aliases accepted) -- one API call, identical to AWS for one guardrail story across both clouds. Lakera screens local chat and, when baked at deploy, inside the hosted container |
| Memory | AgentCore Memory (semantic store) | Responses-protocol platform-managed history for the hosted runtime (`--session`); local runtime is stateless in v0.1 |
| Bridge | Lambda + API Gateway bearer front | Azure Function bearer-token front (`bridge provision`) for non-Azure callers; token in Key Vault. The Responses endpoint remains the Entra-native door for Azure-aware clients |

## 3. AWS-to-Azure resource mapping

| AWS resource (name) | Azure resource (name) | Notes |
|---|---|---|
| Bedrock (Converse, Claude + Nova) | Foundry account `Microsoft.CognitiveServices/accounts` kind `AIServices` (`chkpmcp-foundry-<token>`) + Claude deployments `claude-sonnet-4-6`, `claude-haiku-4-5`, **or** the first-party `gpt-5-mini` deployment (cheap test) | Claude: `AnthropicFoundry`, base URL `.../anthropic`. `gpt-5-mini`: classic `AzureOpenAI`, `azure_endpoint` = account root (`OPENAI_BASE_URL`), api-version `2024-10-21` |
| AgentCore Runtime `chkp_agent` | Foundry Hosted Agent `chkpmcp-agent` (Responses protocol 2.0.0) | image `chkp-agent:v1` in ACR |
| AgentCore Runtimes per server + Gateway `chkp-mcp-gw` | (none) stdio child processes | see delta table |
| Cognito pool/client/domain | Entra ID (`DefaultAzureCredential`) + RBAC roles | no custom auth tier |
| Secrets Manager `chkp/<server>` | Key Vault secret `chkpmcp-<server>` in `kv-chkpmcp-<token>` | JSON body, same env-var keys; 7-day soft delete |
| ECR + CodeBuild + S3 src bucket | ACR `acrchkpmcp<token>` + `az acr build` (remote build) | base image via `public.ecr.aws` mirror to dodge Docker Hub 429s |
| CloudWatch logs/metrics | Log Analytics `log-chkpmcp-<token>` + App Insights `appi-chkpmcp-<token>` | `APPLICATIONINSIGHTS_CONNECTION_STRING` auto-injected in hosted agents |
| AgentCore Policy + Bedrock Guardrails (default) | Content Safety Prompt Shields on the AIServices account endpoint (default) | `POST {endpoint}/contentsafety/text:shieldPrompt?api-version=2024-09-01`, Entra bearer. The optional **Check Point AI Guardrail (Lakera Guard)** opt-in is cloud-agnostic -- the same `POST` to the Lakera Guard API on AWS and Azure; its key/project id live in a Key Vault secret (`<prefix>-lakera-guard`), not a per-server credential |
| IAM roles (7) | RBAC role assignments (Bicep + post-deploy CLI step for the agent identity) | `Cognitive Services User`, `Key Vault Secrets User/Officer`, `AcrPush`/`AcrPull`, `Foundry Project Manager/User/Agent Consumer` |
| SSM `/chkp/model-access` marker | not needed (deployments are Bicep-owned, destroyed with the stack) | |
| region `us-east-1` | location `eastus2` (or `swedencentral`) | only regions with BOTH hosted agents and Claude accounts |

## 4. Region, identity, and secret-flow decisions

- **Region**: default `eastus2`; `doctor` and `deploy` refuse other locations
  unless in `SUPPORTED_LOCATIONS` (`eastus2`, `swedencentral`).
- **Auth**: everything uses `DefaultAzureCredential`. Inside the hosted
  container it resolves to the per-agent Entra identity; locally to `az login`.
  Claude route scope is `https://ai.azure.com/.default`; the classic
  `AzureOpenAI` client for `gpt-5-mini` uses
  `https://cognitiveservices.azure.com/.default`; Content Safety / KV use their
  own scopes. The agent identity gets `Cognitive Services User` at account
  scope and `Key Vault Secrets User` on the vault -- assigned by the CLI
  post-create (the principal id only exists after the agent version is
  created); on the `azure-openai` path it also gets `Cognitive Services OpenAI
  User` (`5e0bd9bd-7b93-4f28-af87-19fc36ad61bd`) for `gpt-5-mini` inference.
- **Provider abstraction**: `chkpmcpaz.providers` exposes a frozen `Provider`
  Protocol; `get_provider(cfg.provider)` returns a singleton `AnthropicProvider`
  (default, `anthropic`) or `AzureOpenAIProvider` (`azure-openai`). Each maps
  its vendor's native wire format -- tool schema, streaming, `tool_use` vs
  `tool_calls`, usage fields -- onto a shared `TurnResult`, so the agent loop,
  telemetry line, truncation, retries, and printed lines are identical across
  providers. `config.provider_for`/`resolve_provider` are the single source of
  truth for detection (`gpt-*`/`o[0-9]*`/`*openai*` → `azure-openai`, else
  `anthropic`); precedence is explicit `--provider`/`CHKP_PROVIDER` >
  model-name detection > `anthropic`. New azd outputs `OPENAI_BASE_URL`
  (account root) / `OPENAI_MODEL_DEPLOYMENT`, and Bicep gates
  `deployClaudeModels` / `deployOpenAiModel`, keep a `gpt-5-mini`-only stack
  from provisioning any Claude resources.
- **Secrets**: one KV secret per credentialed server, JSON body with the same
  env-var keys as AWS. Placeholder-only at deploy; `creds apply` writes real
  values and never logs them; existing real values are never clobbered by
  re-deploys. Child processes receive the decoded JSON as env vars at spawn
  (per-spawn fetch = `refresh` is implicit for local; hosted `refresh` bumps
  the agent version to restart sandboxes).
- **azd vs SDK split**: `azd provision`/`azd down` own all ARM resources
  (azure.yaml has `infra: {provider: bicep}` and NO services block -- the azd
  `microsoft.foundry` provider cannot express Anthropic-format deployments).
  The hosted agent itself is data-plane, created by the CLI via
  `azure-ai-projects` (`create_version` + traffic routing), mirroring the
  imperative AWS style.
- **Container**: Python 3.12 + Node 20 in one image (the agent spawns `npx`).
  Base pulled from `public.ecr.aws/docker/library/python:3.12-slim` (MCR has no
  docker-library mirror; ECR public avoids Docker Hub anonymous-pull 429s from
  ACR build fleets). linux/amd64 only; port 8088; `/readiness` is auto-served
  by `azure-ai-agentserver-responses` -- never implemented by us.

## 5. Repo layout and ownership

See CONTRACT.md section 2 for the full tree with per-builder ownership. In
short: `infra/` + `azure.yaml` (infra builder), `chkpmcpaz/` runtime modules +
`agent/` + `scripts/` (runtime builder), `chkpmcpaz/` CLI modules (cli
builder), `tests/` + `.github/` (tests builder), `docs/` + `README.md` (docs
builder). `chkpmcpaz/config.py` is architect-owned and already written --
every name, pin, env var, and prompt lives there.

## 6. Security invariants (org policy, must survive every build)

- No API keys/secrets/tokens/credentials hardcoded anywhere, ever; secrets
  only in Key Vault; `.gitignore` blocks `.env`, `*.pem`, `*.key`,
  `chkp-credentials*`, `.azure/`.
- TLS verification never disabled (no `verify=False`, no
  `disableLocalAuth`-style downgrades in client code; unit-tested).
- Secret VALUES never printed or logged -- names and key names only.
- Every endpoint authenticated: hosted agent is Entra-gated by Foundry;
  Content Safety and KV calls are bearer-authenticated; no anonymous surface.
- All user input validated (`validate_prefix`, `parse_servers`,
  `sanitize_id`); no shell=True with user input; no dynamic code execution.
- No GPL dependencies (all pinned deps are MIT/Apache/BSD).
