# Azure resource inventory

Everything `deploy` creates, with the exact names, so teardown (and you)
always know what exists. All names derive from the prefix (default
`chkpmcp`); `<token>` is Bicep's
`uniqueString(subscription().id, environmentName)`. Every resource is tagged
`project=chkp-mcp-foundry`, `stack=<prefix>`, and `azd-env-name=<prefix>`.

The split: **`azd provision` / `azd down` own all ARM resources** (Bicep,
`infra/`); the **hosted agent is data-plane**, created by the CLI via
`azure-ai-projects` after provisioning (the azd `microsoft.foundry` provider
cannot express Anthropic-format model deployments, so there is no `services:`
block in `azure.yaml`).

## ARM resources (Bicep)

| Resource | Type @ API version | Name | Notes |
|---|---|---|---|
| Resource group | — | `rg-<prefix>` → `rg-chkpmcp` | `targetScope = subscription` in `main.bicep`. |
| Foundry account | `Microsoft.CognitiveServices/accounts@2025-10-01-preview` | `<prefix>-foundry-<token>` | kind `AIServices`, sku `S0`, SystemAssigned identity, custom subdomain = account name, `allowProjectManagement: true`. |
| Foundry project | `accounts/projects@2025-10-01-preview` | `<prefix>-project` | SystemAssigned identity; required for Claude and hosts the agent. |
| Claude deployment (primary) | `accounts/deployments@2025-10-01-preview` | `claude-sonnet-4-6` | *Claude path (`deployClaudeModels=true`, default).* `GlobalStandard`, capacity `claudeCapacity` (default 25 = 25K TPM), `model.format: 'Anthropic'`, required `modelProviderData` (org/country/industry -- **auto-accepts Anthropic's commercial terms**), RAI policy `Microsoft.DefaultV2`. |
| Claude deployment (fallback) | `accounts/deployments@2025-10-01-preview` | `claude-haiku-4-5` | Same shape; skipped when `deployFallbackModel=false`. Deployments are serialized with `dependsOn` (409 avoidance). |
| `gpt-5-mini` deployment | `accounts/deployments@2025-10-01-preview` | `gpt-5-mini` | *Cheap test path (`deployOpenAiModel=true`, `deployClaudeModels=false`).* First-party `GlobalStandard`, capacity `openAiCapacity` (default 50 = 50K TPM), `model.format: 'OpenAI'`, version `openAiModelVersion` (`2025-08-07`), **no `modelProviderData`** (not a Marketplace offer, no `--org`), RAI policy `Microsoft.DefaultV2`. Deploys on MSDN / Dev-Test subscriptions where Claude is blocked. |
| Key Vault | `Microsoft.KeyVault/vaults@2024-11-01` | `kv-<prefix>-<token>` (trimmed to 24 chars) | RBAC authorization, soft delete on, 7-day retention, sku standard. `publicNetworkAccess` = `keyVaultPublicNetworkAccess` param (default `Enabled` so the CLI can seed secrets from the operator machine; set `Disabled` -- `networkAcls.defaultAction: Deny`, `bypass: AzureServices` -- when the CLI runs from inside Azure). |
| Container registry | `Microsoft.ContainerRegistry/registries@2025-11-01` | `acr<prefix-no-hyphens><token>` (alnum, trimmed to 50) | sku Basic, admin user **disabled**, `azureADAuthenticationAsArmPolicy: enabled` (Entra-only auth). |
| Log Analytics workspace | `Microsoft.OperationalInsights/workspaces@2025-07-01` | `log-<prefix>-<token>` | |
| Application Insights | `Microsoft.Insights/components@2020-02-02` | `appi-<prefix>-<token>` | Linked to the workspace; its connection string is auto-injected into hosted-agent sandboxes. |
| Role assignments | `Microsoft.Authorization/roleAssignments@2022-04-01` | deterministic `guid(...)` names | See the RBAC table below. |

## Data-plane resources (created by the CLI)

| Resource | Name | Notes |
|---|---|---|
| Key Vault secret per credentialed server | `<prefix>-<server>` → `chkpmcp-quantum-management` | JSON body of the server's credential shape; placeholder values at deploy; real values never clobbered. |
| Gaia agent-side secret | `<prefix>-quantum-gaia` | Answers quantum-gaia's interactive elicitation; never injected into any child process. |
| Check Point AI Guardrail (Lakera) secret | `<prefix>-lakera-guard` | Stack-level Key Vault secret (JSON `LAKERA_API_KEY` / `LAKERA_PROJECT_ID`) for the **optional** Check Point AI Guardrail (Lakera Guard) engine. Seeded/merged at deploy from the operator's env (missing key is non-fatal; real values never clobbered). Read by the hosted agent only when `CHKP_GUARDRAIL_PROVIDER=lakera`; the default guardrail engine stays Azure AI Content Safety Prompt Shields. |
| Agent container image | `<acr login server>/chkp-agent:v1` | Built remotely: `az acr build --file agent/Dockerfile .` (repo-root context; Python 3.12 + Node 20, linux/amd64, port 8088). |
| Hosted agent | `<prefix>-agent` → `chkpmcp-agent` | Foundry project data plane; protocol `responses` `2.0.0`; cpu `1`, memory `2Gi`; env vars `CHKP_PROVIDER`, `KEY_VAULT_URI`, `CONTENT_SAFETY_ENDPOINT`, `CHKP_SERVERS`, `CHKP_PREFIX`, `CHKP_GUARDRAIL`, `CHKP_GUARDRAIL_PROVIDER`, plus the **active provider's** endpoint pair -- `CLAUDE_BASE_URL` + `CLAUDE_MODEL_DEPLOYMENT` (Claude), or `OPENAI_BASE_URL` + `OPENAI_MODEL_DEPLOYMENT` + `CHKP_MODEL` (`gpt-5-mini`). `CHKP_PROVIDER`/`CHKP_SERVERS`/`CHKP_GUARDRAIL`/`CHKP_GUARDRAIL_PROVIDER` carry the deployed provider + server set + guardrail mode + guardrail engine (`content-safety` default, `lakera` = the optional Check Point AI Guardrail; all persisted in the azd env). Versions are immutable; `refresh` bumps the version to restart sandboxes. |

### Optional: the agent bridge (`bridge provision`)

Only when you run `bridge provision` (a bearer-token HTTPS front door for
non-Azure callers). All in `rg-<prefix>`, so `destroy` removes them.

| Resource | Name | Notes |
|---|---|---|
| Function app | `func-<prefix>-bridge-<hash>` | Consumption, Linux/Python, SystemAssigned identity; validates the bearer token and forwards to the hosted agent. |
| Storage account | `st<prefix><hash>` (≤24 alnum) | Required by the Function runtime. |
| Bridge token secret | `<prefix>-bridge-token` | Key Vault secret holding the static bearer token (JSON `{"token": …}`). Never in code/logs. |

The Function's identity is granted `Key Vault Secrets User` (read the token) and
`Cognitive Services User` at account scope (invoke the agent) by `bridge
provision`.

### Optional: the remote MCP tier (`deploy --remote-mcp`)

Provisioned imperatively by the CLI (`chkpmcpaz/remote_mcp.py`) via
`az deployment group create` of `infra/modules/remote-mcp.bicep` — the
AgentCore-Gateway analogue. See [remote-mcp](scenarios/remote-mcp.md).

| Resource | Type @ API version | Name | Notes |
|---|---|---|---|
| Entra app registration | (Microsoft Graph) | `<prefix>-mcp-gateway` | The Easy Auth **audience** (`api://<clientId>`, v2 access tokens). A tenant object, not in the RG — `destroy` deletes it explicitly. |
| User-assigned identity | `Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31` | `id-<prefix>-mcp` | Shared by every remote app; granted **AcrPull** (registry) + **Key Vault Secrets User** (vault). |
| Container Apps environment | `Microsoft.App/managedEnvironments@2024-03-01` | `cae-<prefix>` | Logs wired to the stack's Log Analytics workspace. |
| Container App per server | `Microsoft.App/containerApps@2024-03-01` | `<prefix>-mcp-<server>` (≤32) | Runs the **agent image** with command `python -m chkpmcpaz.remote_server` → `npx -y @chkp/<server>-mcp@<pin> --transport http`. Ingress `:8000`, external, `allowInsecure: false`; **scale-to-zero** (`minReplicas: 0`). |
| Easy Auth config | `Microsoft.App/containerApps/authConfigs@2024-03-01` | `<app>/current` | Entra provider, `unauthenticatedClientAction: Return401`, `allowedAudiences: [api://<clientId>]`. |
| Role assignments | `Microsoft.Authorization/roleAssignments@2022-04-01` | `guid(...)` | AcrPull on the registry + Key Vault Secrets User on the vault, to the shared identity. |

Persisted into the azd env: `CHKP_REMOTE_MCP` (the `[{server,url}]` catalog) and
`CHKP_REMOTE_AUDIENCE` (`api://<clientId>`). The Container Apps + environment +
identity live in the resource group and go with `azd down`.

## RBAC role assignments

Assigned by Bicep at provision time (declared **before** the model
deployments complete so RBAC propagates during the LRO). Each assignment sets
`principalType` -- forwarded from `main.bicep` (`AZURE_PRINCIPAL_TYPE`, default
`User`; CI under a service principal sets `ServicePrincipal`):

| Principal | Role (id) | Scope |
|---|---|---|
| Deploying user (`principalId`, skipped when empty) | `Cognitive Services User` (`a97b65f3-24c7-4388-baec-2e87135dc908`) | Foundry account |
| Deploying user | `Key Vault Secrets Officer` (`b86a8fe4-44ce-4948-aee5-eccb2c155cd7`) | Key Vault |
| Deploying user | `AcrPush` (`8311e382-0749-4cb8-b61a-304f252e45ec`) | Container registry |
| Deploying user | Foundry Project Manager (data-plane agent create) | Foundry account |
| Project managed identity | `AcrPull` (`7f951dda-4ed3-4680-a7ca-43fe172d538d`) | Container registry (image pull) |

Assigned by the **CLI after the hosted agent exists** (its per-agent Entra
identity -- `instance_identity.principal_id` -- only exists after the version
is created; the agent identity has **no implicit access** to the
account-level `/anthropic` route or the vault):

| Principal | Role (id) | Scope |
|---|---|---|
| Hosted-agent identity | `Cognitive Services User` (`a97b65f3-24c7-4388-baec-2e87135dc908`) | Foundry account (Claude route + Content Safety) |
| Hosted-agent identity | `Key Vault Secrets User` (`4633458b-17de-408a-b874-0445c86b69e6`) | Key Vault (read server secrets) |
| Hosted-agent identity (`azure-openai` path only) | `Cognitive Services OpenAI User` (`5e0bd9bd-7b93-4f28-af87-19fc36ad61bd`) | Foundry account (`gpt-5-mini` inference) |

Already-exists is tolerated on re-runs. RBAC propagation can take up to 30
minutes -- a fresh deploy that fails with 403s usually just needs time.

## Bicep outputs (azd env values)

`azd provision` writes these into the azd environment; the CLI reads them via
`azd env get-values`:

| Output | Shape |
|---|---|
| `AZURE_LOCATION` | `eastus2` \| `swedencentral` |
| `AZURE_TENANT_ID` | tenant guid |
| `AZURE_RESOURCE_GROUP` | `rg-<prefix>` |
| `FOUNDRY_ACCOUNT_NAME` / `FOUNDRY_PROJECT_NAME` | account / project names |
| `FOUNDRY_PROJECT_ENDPOINT` | `https://<acct>.services.ai.azure.com/api/projects/<project>` |
| `CLAUDE_BASE_URL` | `https://<acct>.services.ai.azure.com/anthropic` (empty when Claude isn't deployed) |
| `CLAUDE_MODEL_DEPLOYMENT` / `CLAUDE_FALLBACK_DEPLOYMENT` | `claude-sonnet-4-6` / `claude-haiku-4-5` (or empty) |
| `OPENAI_BASE_URL` | `https://<acct>.services.ai.azure.com` -- the account **root** (the classic `AzureOpenAI` client appends its own `/openai` route); empty unless `gpt-5-mini` is deployed |
| `OPENAI_MODEL_DEPLOYMENT` | `gpt-5-mini` (or empty) |
| `KEY_VAULT_NAME` / `KEY_VAULT_URI` | vault name / `https://<kv>.vault.azure.net/` |
| `AZURE_CONTAINER_REGISTRY_NAME` / `AZURE_CONTAINER_REGISTRY_ENDPOINT` | registry name / `<name>.azurecr.io` |
| `CONTENT_SAFETY_ENDPOINT` | `https://<acct>.cognitiveservices.azure.com/` |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | App Insights connection string |

## Endpoint URL shapes

| Endpoint | Shape |
|---|---|
| Claude (messages) | `https://<account>.services.ai.azure.com/anthropic` -- the SDK appends `/v1/messages`; the **deployment name** is passed as `model`. Entra scope `https://ai.azure.com/.default`. |
| `gpt-5-mini` (chat completions) | `https://<account>.services.ai.azure.com` (account root, from `OPENAI_BASE_URL`) -- the classic `AzureOpenAI` client appends `/openai/deployments/<model>/chat/completions`; api-version `2024-10-21`; the **deployment name** is passed as `model`. Entra scope `https://cognitiveservices.azure.com/.default`. |
| Foundry project | `https://<account>.services.ai.azure.com/api/projects/<project>` |
| Hosted agent (Responses) | `{project_endpoint}/agents/<prefix>-agent/endpoint/protocols/openai/responses` |
| Content Safety Prompt Shields | `POST {CONTENT_SAFETY_ENDPOINT}/contentsafety/text:shieldPrompt?api-version=2024-09-01` |
| Key Vault | `https://<kv-name>.vault.azure.net/` |

## What destroy removes

`destroy` = hosted agent delete (data plane) → `azd down --force --purge`.
The purge clears Key Vault and Cognitive Services **soft-deletes**, so
redeploys under the same names work immediately. It does **not** remove
anything you created by hand outside this stack (private networking, extra
secrets, other resource groups) -- remove those with the tool that created
them.
