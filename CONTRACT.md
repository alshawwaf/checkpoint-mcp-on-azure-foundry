# CONTRACT -- builder interface specification (v1, frozen)

Five builder agents (**infra**, **runtime**, **cli**, **tests**, **docs**) work
on this repo IN PARALLEL with no coordination. This document is the only
shared truth. If something here conflicts with your instinct, this document
wins. `chkpmcpaz/config.py` is ARCHITECT-OWNED, already written, and frozen --
import from it; never modify it. Same for `pyproject.toml` and `.gitignore`.

## 0. Ground rules (all builders)

- Check Point org policy: NEVER hardcode API keys/secrets/tokens/credentials;
  never commit `.env`/`*.pem`/`*.key`; never disable TLS verification (no
  `verify=False` anywhere); validate/sanitize all user input; secrets live in
  Azure Key Vault only; no GPL dependencies; every endpoint authenticated.
- Secret VALUES are never printed, logged, or embedded in exceptions -- only
  secret NAMES and env-var KEY names.
- Shell commands in docs/help text are written `python3 -m chkpmcpaz ...`
  (always `python3`, never `python`).
- Python floor 3.11. Only these runtime deps (already in pyproject):
  `anthropic`, `azure-identity`, `azure-keyvault-secrets`, `requests`;
  optional extras `mcp` (`mcp>=1.0`), `hosting` (`azure-ai-projects>=2.1.0`),
  `dev` (`pytest>=7`). Import `mcp` and `azure.ai.projects` lazily with a
  friendly install hint (`pip install "chkpmcpaz[mcp]"` etc.) when missing.
- Write files ONLY under the paths you own (section 2). Do not create files in
  another builder's paths, and do not edit architect-owned files.
- Style mirror: module docstrings explain design decisions; `--` (double
  hyphen) as em-dash in prose; f-strings; stdlib `argparse`; no click/typer.

## 1. Fixed names quick reference

| Thing | Value |
|---|---|
| Python package / CLI | `chkpmcpaz`; console script `chkpmcpaz`; `python3 -m chkpmcpaz` |
| Default prefix / azd env name | `chkpmcp` (regex `^[a-z][a-z0-9-]{0,11}$`) |
| Default location | `eastus2` (allowed: `eastus2`, `swedencentral`) |
| Resource group | `rg-<prefix>` -> `rg-chkpmcp` |
| Foundry account | `<prefix>-foundry-<resourceToken>`; custom subdomain = same |
| Foundry project | `<prefix>-project` |
| Claude deployments | `claude-sonnet-4-6` (primary), `claude-haiku-4-5` (fallback) |
| `gpt-5-mini` deployment (cheap test path) | `gpt-5-mini` (first-party Azure OpenAI, `format: 'OpenAI'`, version `2025-08-07`, `GlobalStandard`) |
| Providers | `anthropic` (Claude, default) · `azure-openai` (`gpt-5-mini`) · `auto` = detect from model name |
| Key Vault | `kv-<prefix>-<resourceToken>` (trim to 24 chars) |
| KV secret per server | `<prefix>-<server>` -> `chkpmcp-quantum-management` |
| ACR | `acr<prefix-no-hyphens><resourceToken>` (alnum, trim to 50) |
| Agent image | `<acr login server>/chkp-agent:v1` |
| Hosted agent name | `<prefix>-agent` -> `chkpmcp-agent`; protocol `responses` `2.0.0`; port 8088 |
| Log Analytics / App Insights | `log-<prefix>-<resourceToken>` / `appi-<prefix>-<resourceToken>` |
| Tags | `project=chkp-mcp-foundry`, `stack=<prefix>` |
| Tool namespace | `<server-no-hyphens>___<tool>` (e.g. `quantummanagement___show_hosts`) |
| resourceToken | `uniqueString(subscription().id, environmentName)` (Bicep) |

Env vars (exact strings; constants exist in `config.py`):
`CHKP_SERVERS`, `CHKP_PREFIX`, `CHKP_PROVIDER` (`anthropic`|`azure-openai`),
`CHKP_MODEL`, `CHKP_GUARDRAIL` (`"1"`=on), `CHKP_GUARDRAIL_PROVIDER`
(`content-safety`|`lakera`, default `content-safety`), `CHKP_LOG_DIR`, `CHKP_UI`
(`plain`|`tui`), `FOUNDRY_PROJECT_ENDPOINT` (platform-injected -- never
redeclare `FOUNDRY_*`), `CLAUDE_BASE_URL`, `CLAUDE_MODEL_DEPLOYMENT`,
`OPENAI_BASE_URL`, `OPENAI_MODEL_DEPLOYMENT`, `KEY_VAULT_URI`,
`CONTENT_SAFETY_ENDPOINT`, `AZURE_SUBSCRIPTION_ID`, `AZURE_LOCATION`,
`AZURE_ENV_NAME`,
`GAIA_GATEWAY_IP/GAIA_PORT/GAIA_USER/GAIA_PASSWORD/GAIA_ADDRESS`, plus the
optional Check Point AI Guardrail (Lakera Guard) engine's `LAKERA_API_KEY`,
`LAKERA_PROJECT_ID`, optional `LAKERA_API_URL` (older `LAKERA_GUARD_*` names
accepted as aliases; key/project id live in the Key Vault secret
`<prefix>-lakera-guard`, never forced -- `content-safety` stays the default).

Infra-only azd env → Bicep params (cheap `gpt-5-mini` gate): `DEPLOY_CLAUDE_MODELS`,
`DEPLOY_OPENAI_MODEL`, `OPENAI_DEPLOYMENT_NAME`, `OPENAI_MODEL_VERSION`,
`OPENAI_CAPACITY`.

Provider detection (single source of truth in `config.py`): explicit
`--provider`/`CHKP_PROVIDER` (non-`auto`) wins; else `provider_for(model)`
(`gpt-*`/`o[0-9]*`/`*openai*` → `azure-openai`, else `anthropic`); default
`anthropic`.

Entra scopes: Claude route `https://ai.azure.com/.default`; classic
`AzureOpenAI` client (`gpt-5-mini`) + Content Safety + Key Vault via
`https://cognitiveservices.azure.com/.default` / their SDK defaults.
`gpt-5-mini` inference role: `Cognitive Services OpenAI User`
(`5e0bd9bd-7b93-4f28-af87-19fc36ad61bd`). Content Safety api-version:
`2024-09-01`; classic `AzureOpenAI` api-version: `2024-10-21`.

## 2. File tree and ownership

```
checkpoint-mcp-on-azure-foundry/
├── pyproject.toml            ARCHITECT (frozen)
├── .gitignore                ARCHITECT (frozen)
├── DESIGN.md                 ARCHITECT (frozen)
├── CONTRACT.md               ARCHITECT (frozen)
├── README.md                 DOCS (stub exists; replace wholesale)
├── azure.yaml                INFRA
├── infra/
│   ├── main.bicep            INFRA
│   ├── main.parameters.json  INFRA
│   ├── abbreviations.json    INFRA (optional)
│   └── modules/
│       ├── foundry.bicep     INFRA
│       ├── keyvault.bicep    INFRA
│       ├── registry.bicep    INFRA
│       ├── monitoring.bicep  INFRA
│       └── roles.bicep       INFRA
├── chkpmcpaz/
│   ├── __init__.py           ARCHITECT (frozen; __version__)
│   ├── config.py             ARCHITECT (frozen; single source of truth)
│   ├── __main__.py           CLI
│   ├── cli.py                CLI
│   ├── deploy.py             CLI
│   ├── destroy.py            CLI
│   ├── verify.py             CLI      (status command internals)
│   ├── doctor.py             CLI
│   ├── creds.py              CLI
│   ├── hosting.py            CLI      (hosted-agent create/invoke via azure-ai-projects)
│   ├── azutil.py             CLI
│   ├── ui.py                 CLI
│   ├── agent.py              RUNTIME  (Claude tool-use loop)
│   ├── mcp_stdio.py          RUNTIME  (stdio child-process MCP client)
│   ├── keyvault.py           RUNTIME  (secret read/write helpers)
│   ├── guardrail.py          RUNTIME  (Prompt Shields)
│   ├── gaia.py               RUNTIME  (elicitation answerer)
│   └── _hosting_server.py    RUNTIME  (Responses-protocol server, runs in container)
├── agent/
│   ├── Dockerfile            RUNTIME
│   ├── main.py               RUNTIME  (thin: chkpmcpaz._hosting_server.main())
│   └── requirements.txt      RUNTIME
├── scripts/
│   └── mcp_probe.mjs         RUNTIME  (local stdio probe: initialize -> tools/list -> optional tools/call)
├── tests/
│   ├── __init__.py           ARCHITECT (frozen, empty)
│   └── test_*.py             TESTS
├── .github/workflows/tests.yml  TESTS
└── docs/
    ├── commands.md           DOCS
    ├── servers.md            DOCS
    ├── resources.md          DOCS
    ├── scenarios/*.md        DOCS
    └── img/                  DOCS
```

## 3. Architect-provided API (`chkpmcpaz/config.py`) -- already written

Constants: `DEFAULT_LOCATION`, `SUPPORTED_LOCATIONS`, `DEFAULT_PREFIX`,
`PREFIX_RE`, `CLAUDE_DEPLOYMENTS` (tuples `(deployment, model, version)`),
`MODEL_PREFERENCE`, `CHEAPEST_MODEL`, `MAX_TURNS=12`, `MAX_TOKENS=2048`,
`TOOL_RESULT_MAX_CHARS=6000`, `TOOL_DESCRIPTION_MAX_CHARS=1000`,
`MEMORY_CONTEXT_MAX_CHARS=1500`, `AI_SCOPE`, `COGNITIVE_SCOPE`,
`CONTENT_SAFETY_API_VERSION`, `SYSTEM_PROMPT` (verbatim AWS prompt),
`GUARDRAIL_TEST_INJECTION`, `PLACEHOLDER_VALUE`, `CRED_SHAPE`,
`SERVERS: dict[str, ServerSpec]`, `DEFAULT_SERVERS` (9 names),
`NAMESPACE_SEP="___"`, env-var name constants (`ENV_*`), `GAIA_ENV_KEYS`,
`IMAGE_REPO="chkp-agent"`, `IMAGE_TAG="v1"`,
`AGENT_PROTOCOL=("responses","2.0.0")`, `AGENT_PORT=8088`,
`DEFAULT_ACTOR="chkp-analyst"`, `TAGS`.

Types/functions:
```python
class ServerSpec:  # frozen dataclass
    name; version; creds; agent_creds; args: tuple[str, ...]
    in_default: bool; exclude_from_all: bool; note: str
    package -> "@chkp/<name>-mcp"; pinned -> "@chkp/<name>-mcp@<version>"

validate_prefix(prefix: str) -> str                      # raises ValueError
parse_servers(spec: str | None) -> list[str]             # ''/None->default 9, 'all'->11, names validated
target_name(server: str) -> str                          # hyphens stripped
tool_namespace(server: str, tool: str) -> str
split_namespaced(name: str) -> tuple[str, str]           # raises ValueError
env_name(prefix=DEFAULT_PREFIX) -> str
resource_group_name(prefix) -> str                       # "rg-<prefix>"
agent_name(prefix) -> str                                # "<prefix>-agent"
secret_name(server, prefix=DEFAULT_PREFIX) -> str        # "<prefix>-<server>"
image_ref(registry_login_server, tag=IMAGE_TAG) -> str
sanitize_id(value, max_len=128) -> str                   # [a-zA-Z0-9_-]

class StackConfig:  # frozen dataclass
    prefix; location; subscription_id; project_endpoint; claude_base_url
    claude_deployment; key_vault_uri; content_safety_endpoint; servers: tuple
    StackConfig.from_env(env: Mapping | None = None) -> StackConfig
    .secret_name(server) -> str; .agent_name() -> str; .resource_group() -> str
```

## 4. INFRA builder -- `azure.yaml` + `infra/**`

### 4.1 `azure.yaml` (exact)

```yaml
# yaml-language-server: $schema=https://raw.githubusercontent.com/Azure/azure-dev/main/schemas/v1.0/azure.yaml.json
name: checkpoint-mcp-on-azure-foundry
infra:
  provider: bicep
  path: infra
  module: main
```

NO `services:` block -- the hosted agent is data-plane, created by the CLI via
`azure-ai-projects` (the azd `microsoft.foundry` provider cannot express
`format: Anthropic` model deployments). Hooks are optional; if you add a
`preprovision` hook it must only WARN, never mutate state.

### 4.2 `infra/main.bicep`

`targetScope = 'subscription'`. Creates `rg-${environmentName}` and calls the
modules. Parameters (exact names/defaults):

```bicep
param environmentName string                 // azd env name == prefix, e.g. 'chkpmcp'
param location string                        // constrain with @allowed(['eastus2','swedencentral'])
param principalId string = ''                // deploying user objectId (azd injects)
param claudeOrganizationName string = ''     // sent to Anthropic via modelProviderData; '' for a gpt-only deploy
param claudeCountryCode string = 'US'        // 2-letter ISO
param claudeIndustry string = 'technology'   // MUST be lowercase enum value
param claudeCapacity int = 25                // TPM/1000 per deployment
param deployFallbackModel bool = true        // claude-haiku-4-5
param deployClaudeModels bool = true         // provision the Claude deployments
param deployOpenAiModel bool = false         // provision the first-party gpt-5-mini deployment
param openAiDeploymentName string = 'gpt-5-mini'
param openAiModelVersion string = '2025-08-07'
param openAiCapacity int = 50                // TPM/1000 for gpt-5-mini (GlobalStandard)
```

`var resourceToken = uniqueString(subscription().id, environmentName)`.
All resources tagged `{ project: 'chkp-mcp-foundry', stack: environmentName, 'azd-env-name': environmentName }`.

Outputs (EXACT names -- azd copies them into the env, and `chkpmcpaz` reads
them via `azd env get-values`):

```bicep
output AZURE_LOCATION string
output AZURE_TENANT_ID string
output AZURE_RESOURCE_GROUP string                    // rg-<env>
output FOUNDRY_ACCOUNT_NAME string
output FOUNDRY_PROJECT_NAME string
output FOUNDRY_PROJECT_ENDPOINT string                // https://<acct>.services.ai.azure.com/api/projects/<project>
output CLAUDE_BASE_URL string                         // https://<acct>.services.ai.azure.com/anthropic ('' when Claude not deployed)
output CLAUDE_MODEL_DEPLOYMENT string                 // 'claude-sonnet-4-6'
output CLAUDE_FALLBACK_DEPLOYMENT string              // 'claude-haiku-4-5' or ''
output OPENAI_BASE_URL string                         // https://<acct>.services.ai.azure.com (account ROOT, no route suffix; client appends /openai)
output OPENAI_MODEL_DEPLOYMENT string                 // 'gpt-5-mini' or ''
output KEY_VAULT_NAME string
output KEY_VAULT_URI string                           // https://<kv>.vault.azure.net/
output AZURE_CONTAINER_REGISTRY_NAME string
output AZURE_CONTAINER_REGISTRY_ENDPOINT string       // <name>.azurecr.io
output CONTENT_SAFETY_ENDPOINT string                 // https://<acct>.cognitiveservices.azure.com/
output APPLICATIONINSIGHTS_CONNECTION_STRING string
```

### 4.3 `infra/main.parameters.json`

Maps `environmentName<-${AZURE_ENV_NAME}`, `location<-${AZURE_LOCATION}`,
`principalId<-${AZURE_PRINCIPAL_ID}`,
`claudeOrganizationName<-${CLAUDE_ORGANIZATION_NAME}` (now tolerates empty),
`claudeCountryCode<-${CLAUDE_COUNTRY_CODE}`,
`claudeIndustry<-${CLAUDE_INDUSTRY}`, plus the cheap-test gate:
`deployClaudeModels<-${DEPLOY_CLAUDE_MODELS=true}`,
`deployOpenAiModel<-${DEPLOY_OPENAI_MODEL=false}`,
`openAiDeploymentName<-${OPENAI_DEPLOYMENT_NAME=gpt-5-mini}`,
`openAiModelVersion<-${OPENAI_MODEL_VERSION=2025-08-07}`,
`openAiCapacity<-${OPENAI_CAPACITY=50}`.

### 4.4 Modules (exact file names, params in parens, outputs listed)

- **`modules/foundry.bicep`** (name/location/tags/claude* params) --
  `Microsoft.CognitiveServices/accounts@2025-10-01-preview` kind `AIServices`,
  sku `S0`, SystemAssigned identity, `customSubDomainName` = account name,
  `allowProjectManagement: true`, `publicNetworkAccess: 'Enabled'`,
  `disableLocalAuth: false`;
  child `accounts/projects@2025-10-01-preview` (SystemAssigned, `properties: {}`);
  `accounts/deployments@2025-10-01-preview` per Claude model: sku
  `{name:'GlobalStandard', capacity: claudeCapacity}`, `properties.model
  {format:'Anthropic', name, version:'1'}`, REQUIRED
  `properties.modelProviderData {organizationName, countryCode, industry}`,
  `versionUpgradeOption:'OnceNewDefaultVersionAvailable'`,
  `raiPolicyName:'Microsoft.DefaultV2'`; multiple deployments serialized with
  `dependsOn` (409 avoidance). Claude resources gated
  `if (deployModels && deployClaudeModels)`.
  Also (cheap test path) the first-party OpenAI deployment gated
  `if (deployModels && deployOpenAiModel)`: sku `{name:'GlobalStandard',
  capacity: openAiCapacity}`, `properties.model {format:'OpenAI',
  name: openAiDeploymentName, version: openAiModelVersion}`, **NO
  `modelProviderData`**, `versionUpgradeOption:'OnceCurrentVersionExpired'`,
  `raiPolicyName:'Microsoft.DefaultV2'`.
  Outputs: `accountName`, `accountId`, `accountPrincipalId`,
  `projectName`, `projectPrincipalId`, `projectEndpoint`, `claudeBaseUrl`
  (`''` when Claude not deployed), `openaiBaseUrl`
  (`https://${account.name}.services.ai.azure.com`, account ROOT),
  `openaiDeployment` (`openAiDeploymentName` or `''`),
  `contentSafetyEndpoint` (account `properties.endpoint`),
  `primaryDeployment`, `fallbackDeployment`.
- **`modules/keyvault.bicep`** -- `Microsoft.KeyVault/vaults@2024-11-01`,
  `enableRbacAuthorization: true`, `enableSoftDelete: true`,
  `softDeleteRetentionInDays: 7`, sku standard.
  Outputs: `keyVaultName`, `keyVaultUri`, `keyVaultId`.
- **`modules/registry.bicep`** --
  `Microsoft.ContainerRegistry/registries@2025-11-01`, sku Basic,
  `adminUserEnabled: false`, policy `azureADAuthenticationAsArmPolicy:
  {status: 'enabled'}`. Outputs: `registryName`, `registryLoginServer`,
  `registryId`.
- **`modules/monitoring.bicep`** --
  `Microsoft.OperationalInsights/workspaces@2025-07-01` +
  `Microsoft.Insights/components@2020-02-02` (linked). Outputs:
  `logAnalyticsWorkspaceId`, `applicationInsightsName`,
  `applicationInsightsConnectionString`.
- **`modules/roles.bicep`** --
  `Microsoft.Authorization/roleAssignments@2022-04-01`, all with
  `principalType` set and deterministic `guid(...)` names:
  - deployer `principalId` (skip when empty): `Cognitive Services User`
    (`a97b65f3-24c7-4388-baec-2e87135dc908`) on the account; `Key Vault
    Secrets Officer` (`b86a8fe4-44ce-4948-aee5-eccb2c155cd7`) on the vault;
    `AcrPush` (`8311e382-0749-4cb8-b61a-304f252e45ec`) on the registry;
    `Azure AI User`/`Foundry Project Manager` at account scope if available as
    built-in id, else document the `az role assignment` command in a comment.
  - project managed identity (`projectPrincipalId`): `AcrPull`
    (`7f951dda-4ed3-4680-a7ca-43fe172d538d`) on the registry.
  Role assignments must be declared BEFORE model deployments complete
  (dependsOn chain per the starter kit) so RBAC propagates during the LRO.

NOTE: the hosted agent's per-agent identity does NOT exist at provision time;
its `Cognitive Services User` + `Key Vault Secrets User`
(`4633458b-17de-408a-b874-0445c86b69e6`) grants are made by the CLI
post-create (section 6.3). Do not attempt them in Bicep.

## 5. RUNTIME builder -- agent loop, stdio MCP, KV, guardrail, container

All async code uses `asyncio` + the `mcp` package's stdio client. Public
signatures below are FROZEN (cli + tests import them).

### 5.1 `chkpmcpaz/keyvault.py`

```python
PLACEHOLDER_VALUE  # re-export from config
def get_secret_json(vault_uri: str, name: str, credential=None) -> dict[str, str] | None
    # None if secret absent; raises nothing for not-found; JSON-decodes body
def set_secret_json(vault_uri: str, name: str, payload: dict[str, str], credential=None) -> None
    # creates or updates; recovers a soft-deleted secret first if needed
def is_placeholder(payload: dict[str, str] | None) -> bool
    # True if None, empty, or ANY value == PLACEHOLDER_VALUE
def list_stack_secrets(vault_uri: str, prefix: str, credential=None) -> list[str]
def delete_secret(vault_uri: str, name: str, purge: bool = False, credential=None) -> None
```
`credential=None` means construct `DefaultAzureCredential()` internally.
Values never logged.

### 5.2 `chkpmcpaz/mcp_stdio.py`

Spawns each server as `npx -y <ServerSpec.pinned> [*spec.args]` (stdio
transport is the @chkp default) with env = parent env + the server's KV secret
JSON + `TELEMETRY_DISABLED=true`.

```python
@dataclass
class NamespacedTool:
    namespaced: str      # 'quantummanagement___show_hosts'
    server: str          # 'quantum-management'
    description: str     # truncated to TOOL_DESCRIPTION_MAX_CHARS
    input_schema: dict   # normalized to an object schema

class ToolCallResult(TypedDict):
    text: str            # truncated to TOOL_RESULT_MAX_CHARS by the CALLER? No:
                         # full text here; agent.py truncates before feeding back
    error: bool

class ServerPool:        # async context manager
    def __init__(self, servers: list[str], creds_by_server: dict[str, dict[str, str]],
                 *, elicitation_callback=None, env: Mapping[str, str] | None = None): ...
    async def __aenter__(self) -> "ServerPool"; async def __aexit__(...)
    async def list_tools(self) -> list[NamespacedTool]     # merged across children
    async def call(self, namespaced: str, args: dict) -> ToolCallResult
    # call() catches EVERY exception -> {"text": f"tool call failed: {exc}", "error": True}

def npx_command(spec: ServerSpec) -> list[str]   # ['npx','-y','@chkp/x-mcp@1.2.3', *args]
def build_child_env(base: Mapping[str, str], creds: dict[str, str] | None) -> dict[str, str]
```

Failure of one child to start must not kill the pool -- log
`✗ <server> failed to start: <reason ≤140ch>` and continue with the rest.

### 5.3 `chkpmcpaz/agent.py`

```python
MODEL_PREFERENCE, MAX_TURNS, MAX_TOKENS  # re-export from config

@dataclass
class AgentResult:
    text: str
    usage: dict          # {"input_tokens","output_tokens","cache_read_input_tokens","cache_creation_input_tokens"} summed across turns
    model: str           # deployment name actually used
    error: bool = False

def run_task(task: str, cfg: StackConfig, *, model: str | None = None,
             guardrail: bool = False, session: str | None = None,
             actor: str = DEFAULT_ACTOR, out=None) -> AgentResult
    # synchronous facade; runs the async loop; streams text deltas to `out`
    # (default sys.stdout) prefixed 'assistant  '; prints tool lines
    # '→ tool <name> <args≤120ch>' and '✓ ok' / '✗ error' + 140-char preview;
    # prints one telemetry line 'tokens  N in · N out · N cache-read · P% of input from cache'
def run_task_captured(task: str, cfg: StackConfig, *, model: str | None = None,
                      guardrail: bool = False, session: str | None = None,
                      actor: str = DEFAULT_ACTOR) -> dict
    # {"result": str, "usage": dict, "model": str, "error": bool} -- NEVER raises;
    # exceptions become {"error": True, "result": "<Type>: <msg≤300>"}

# pure helpers (unit-tested):
def sanitize_tool_name(name: str) -> str                  # [a-zA-Z0-9_-]{1,64}
def dedupe_names(names: list[str]) -> list[str]           # '_1' suffixes
def build_tools(mcp_tools: list[NamespacedTool]) -> tuple[list[dict], dict[str, str]]
    # Anthropic tools list (name/description/input_schema) + {api_name: namespaced}
    # last tool dict gets {"cache_control": {"type": "ephemeral"}}
def make_client(cfg: StackConfig):                        # AnthropicFoundry via
    # get_bearer_token_provider(DefaultAzureCredential(), AI_SCOPE), base_url=cfg.claude_base_url
def pick_model(client, preference: list[str] = MODEL_PREFERENCE) -> str
    # 8-token probe per deployment; first success wins; on total failure raise
    # ModelUnavailable listing what was tried
def is_transient(exc: BaseException) -> bool              # 429/500/503/529 + APIConnectionError/APITimeoutError
def first_api_error(exc: BaseException) -> BaseException  # unwrap BaseExceptionGroup
class ModelUnavailable(RuntimeError): ...
class GuardrailBlocked(RuntimeError): ...                 # re-export from guardrail
```

Loop semantics (mirror AWS exactly): max 12 turns; system prompt =
`config.SYSTEM_PROMPT` with `cache_control: {"type": "ephemeral"}` on its
block; streaming via `client.messages.stream(...)`; on `stop_reason ==
"tool_use"` call each tool through the ServerPool, truncate result text to
`TOOL_RESULT_MAX_CHARS`, append `tool_result` blocks (with `is_error` flag),
loop; transient errors retried 3 attempts with `1.5s * attempt` backoff;
mid-stream failure retries whole-stream with a printed
`… model stream error — retrying …`; turn budget exhaustion prints
`stopped after 12 turns (turn budget reached)`; guardrail=True calls
`guardrail.screen_prompt(...)` (the provider seam -- content-safety default or
opt-in lakera) BEFORE any model call and raises `GuardrailBlocked` on detection.

### 5.4 `chkpmcpaz/guardrail.py`

The guardrail is OPTIONAL and has two interchangeable engines behind one
provider seam, selected by `--guardrail-provider` / `CHKP_GUARDRAIL_PROVIDER`.
Default `content-safety` = Azure AI Content Safety Prompt Shields
(`screen_input` below); opt-in `lakera` = Check Point's own AI Guardrail
(Lakera Guard) -- a single API call, IDENTICAL on AWS and Azure. The
cloud-native engine stays the default; Lakera is never forced. On Azure, Lakera
screens inline in local chat and, when baked at deploy (`--guardrail`), inside
the hosted container.

```python
class GuardrailBlocked(RuntimeError): ...
def screen_prompt(cfg: StackConfig, text: str, *, env=None) -> tuple[bool, str, str]
    # provider seam: dispatches on CHKP_GUARDRAIL_PROVIDER (via env/os.environ)
    # ('content-safety' default -> screen_input; 'lakera' -> lakera_screen);
    # agent.py calls THIS before any model call. Returns (flagged, label, detail):
    # flagged=attack detected, label=engine name, detail=detector(s)
def screen_input(endpoint: str, user_prompt: str, documents: list[str] | None = None,
                 *, credential=None, timeout: float = 10.0) -> bool
    # Azure Content Safety engine (default).
    # POST {endpoint}/contentsafety/text:shieldPrompt?api-version=2024-09-01
    # Entra bearer (COGNITIVE_SCOPE), json={"userPrompt":..., "documents": documents or []}
    # True iff userPromptAnalysis.attackDetected or any documentsAnalysis[i].attackDetected
def lakera_screen(text: str, api_key: str, project_id: str | None, url: str | None = None,
                  *, timeout: float = 10.0) -> tuple[bool, list[str]]
    # Check Point AI Guardrail (Lakera Guard) engine (opt-in). ONE call:
    # POST {url or https://api.lakera.ai/v2/guard} -- identical on AWS and Azure.
    # Returns (flagged, detectors): flagged from `flagged`, detectors from `breakdown`.
    # Key/project id from LAKERA_API_KEY/LAKERA_PROJECT_ID or the Key Vault
    # secret <prefix>-lakera-guard (older LAKERA_GUARD_* names accepted as aliases)
def run_guardrail_test(cfg: StackConfig) -> int
    # sends a benign prompt then config.GUARDRAIL_TEST_INJECTION; prints
    # allow/deny per case; returns 0 iff benign passes AND injection is detected
```

### 5.5 `chkpmcpaz/gaia.py`

```python
def load_gaia_creds(cfg: StackConfig, env: Mapping[str, str] | None = None) -> dict[str, str] | None
    # env GAIA_* keys first, else KV secret cfg.secret_name('quantum-gaia'); None if placeholder
def make_elicitation_callback(creds: dict[str, str] | None)
    # returns async callback for ServerPool; matches requested fields case-
    # insensitively via aliases {gateway_ip,ip,address,host}->GAIA_GATEWAY_IP,
    # {port}->GAIA_PORT, {user,username}->GAIA_USER, {password}->GAIA_PASSWORD;
    # DECLINES (never hangs) when required fields can't be satisfied
def map_fields(requested: list[str], creds: dict[str, str]) -> dict[str, str] | None  # pure, unit-tested
```

### 5.6 `chkpmcpaz/_hosting_server.py` + `agent/`

`_hosting_server.py`: Responses-protocol server using
`azure.ai.agentserver.responses.ResponsesAgentServerHost` (options
`default_fetch_history_count=20`). The `@app.response_handler` extracts the
input text (`await context.get_input_text()`), builds
`StackConfig.from_env()`, calls `agent.run_task_captured(...)`, and returns a
`TextResponse` whose text is `result["result"]` (if `result["error"]` the text
is prefixed `ERROR: `). Session continuity comes from platform history
(`context.get_history()`); do NOT implement `/readiness`; do NOT bind ports
other than 8088. `def main() -> None:` runs `app.run()`;
`if __name__ == "__main__": main()`.

`agent/main.py` (verbatim):
```python
from chkpmcpaz._hosting_server import main

if __name__ == "__main__":
    main()
```

`agent/requirements.txt` (exact):
```
anthropic>=0.69
azure-identity>=1.19
azure-keyvault-secrets>=4.8
azure-ai-agentserver-responses==1.0.0b8
mcp>=1.0
requests>=2.32
certifi
```

`agent/Dockerfile`: build context is the REPO ROOT (the CLI runs
`az acr build --file agent/Dockerfile .`):
```dockerfile
# linux/amd64 required by Foundry Hosted Agents; base via ECR public mirror
# (not docker.io) to dodge Docker Hub anonymous-pull 429s from build fleets.
FROM public.ecr.aws/docker/library/python:3.12-slim
# Node 20 for the @chkp stdio child processes (npx)
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
 && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && rm -rf /var/lib/apt/lists/*
ENV TELEMETRY_DISABLED=true
WORKDIR /app
COPY agent/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY chkpmcpaz /app/chkpmcpaz
COPY agent/main.py /app/main.py
EXPOSE 8088
CMD ["python", "main.py"]
```

### 5.7 `scripts/mcp_probe.mjs`

Node ESM script: `node scripts/mcp_probe.mjs <package@version> [tool] [json-args]`
-- spawns the package over stdio with `TELEMETRY_DISABLED=true`, does
initialize -> tools/list (prints names) -> optional tools/call, prints raw
JSON-RPC frames. No npm deps beyond node stdlib.

## 6. CLI builder -- `cli.py` and orchestration modules

### 6.1 Command surface (argparse; bare `chkpmcpaz` prints full help, exit 0)

Global flags accepted BEFORE OR AFTER any subcommand (argparse SUPPRESS
trick, subcommand position wins): `--subscription <id>`, `--location <loc>`,
`--prefix <p>`, `--plain`, `--version`.

| Command | Flags | Behavior |
|---|---|---|
| `deploy` | `--servers "<names or all>"` (env `CHKP_SERVERS`), `--creds [file]` (const `chkp-credentials.env`), `--no-agent`, `--org <name>` / env `CLAUDE_ORGANIZATION_NAME`, `--guardrail`, `--guardrail-provider {content-safety,lakera}` (persists `CHKP_GUARDRAIL_PROVIDER`) | full stack up (6.3) |
| `destroy` | `--yes/-y`, `--force-delete-secret` | inventory plan -> confirm -> teardown (6.4) |
| `chat "<task>"` | `--runtime {local,hosted}` (default `local`), `--model <deployment>`, `--session <id>`, `--actor <id>` (default `chkp-analyst`), `--guardrail`, `--servers` | run the agent; no task -> exit 2 + example-question catalog. Guardrail is optional; the ENGINE is chosen by the `CHKP_GUARDRAIL_PROVIDER` env var (set at deploy via `--guardrail-provider`, or exported/`.env`) -- cloud-native Prompt Shields is the default, `lakera` selects the Check Point AI Guardrail (Lakera Guard). `chat` itself has NO `--guardrail-provider` flag (only `deploy` does) |
| `status` | `--json` (optional) | read-only health (6.5) |
| `doctor` | — | local preflight (6.6) |
| `refresh` | — | bump hosted agent version so sandboxes restart and re-read KV secrets |
| `creds` | `template \| apply`, `--file <path>` (default `chkp-credentials.env`) | 6.7 |
| `guardrail` | `test` | `guardrail.run_guardrail_test(cfg)` exit code passthrough |

Exit codes: 0 success; 1 failure (incl. partial deploy, hosted `error:true`);
2 usage (missing task, unknown server); 130 Ctrl-C.

### 6.2 `cli.py` main() wrapper (mirror AWS `02b8c28` exactly)

```python
def main(argv: list[str] | None = None) -> int
def _is_credential_error(exc: BaseException) -> bool   # unit-tested, pure
```
`KeyboardInterrupt` -> print `Interrupted. Every command is idempotent -- re-run
it to continue.` exit 130. Credential-shaped errors
(`azure.identity.CredentialUnavailableError`, `ClientAuthenticationError`,
`az`/`azd` subprocess failures whose stderr mentions expired/login/
reauthenticate, ...) print, exit 1, NO traceback:

```
Your Azure session has expired or credentials are unavailable.
Log in again (az login, or azd auth login), then re-run the same
command -- every command here is idempotent, so re-running is safe.
  (<ExceptionType>: <message truncated to 160 chars>)
```
Non-credential exceptions re-raise (real bugs are never swallowed).

### 6.3 `deploy.py` -- `DEPLOY_STEPS` order

1. doctor-preflight (fatal failures abort before any mutation)
2. `azd env new <prefix>` (idempotent) + `azd env set` for
   `CLAUDE_ORGANIZATION_NAME`/`CLAUDE_COUNTRY_CODE`/`CLAUDE_INDUSTRY`/`AZURE_LOCATION`
3. `azd provision --no-prompt` (Bicep infra incl. Claude deployments)
4. read outputs via `azutil.azd_env_values()`
5. seed per-server placeholder KV secrets (`keyvault.set_secret_json` with
   `CRED_SHAPE[spec.creds]`); NEVER overwrite a secret whose current body is
   non-placeholder; also seed `<prefix>-quantum-gaia` from shape `gaia`
6. optional `creds.apply_file(...)` when `--creds`
7. unless `--no-agent`: `az acr build --registry <name> --image chkp-agent:v1
   --file agent/Dockerfile .` (repo root context)
8. unless `--no-agent`: `hosting.deploy_hosted_agent(cfg, env)` -- create
   version, route 100% traffic, poll `active`
9. `hosting.grant_agent_identity(cfg, env)` -- `Cognitive Services User` at
   account scope + `Key Vault Secrets User` on the vault for
   `instance_identity.principal_id` (via `az role assignment create
   --assignee-principal-type ServicePrincipal`); tolerate already-exists
10. smoke: `verify.run_status(cfg)`; collect failures; exit 1 on any (never
    report partial success), listing each failed step

### 6.4 `destroy.py`

Read-only inventory first (resource group existence, hosted agent, KV secrets
incl. soft-deleted, ACR images, Claude deployments); print plan; `y/N` confirm
(`--yes` required when stdin is not a TTY); then: delete hosted agent (data
plane, via hosting.py) -> `azd down --force --purge` (purges KV + Cognitive
Services soft-deletes so redeploys work) -> report. `--force-delete-secret`
additionally purges soft-deleted secrets if the vault survives. "Nothing to
destroy." on a clean subscription. Idempotent re-runs.

### 6.5 `verify.py`

```python
def run_status(cfg: StackConfig, *, as_json: bool = False) -> int
```
Read-only checks, each with specific remediation text: azd env + outputs
present; Foundry account/project reachable; Claude deployments exist +
8-token probe per `MODEL_PREFERENCE` entry (prints callable models); KV
reachable, per-server secret present + placeholder-or-real flag (names only);
ACR image `chkp-agent:v1` present; hosted agent exists/`active` + endpoint
printed; Content Safety `shieldPrompt` reachable; local Node/npx present.
Exit 1 if anything fails.

### 6.6 `doctor.py`

```python
def run_doctor(cfg: StackConfig) -> int
```
Local-only preflight: python>=3.11; `az` + logged in (`az account show`);
`azd` >= 1.25.3; `node`/`npx` >= 20 present; location in
`SUPPORTED_LOCATIONS`; subscription is pay-as-you-go-capable warning (CSP/
free-trial subs can't deploy Claude); `mcp` + `azure-ai-projects` extras
importable (hints); org policy reminders. Warnings vs failures clearly
separated; exit 1 only on hard failures.

### 6.7 `creds.py`

```python
def write_template(path: str, servers: list[str]) -> None   # refuses to overwrite
def parse_creds_file(path: str) -> dict[str, dict[str, str]]  # INI [server] sections,
    # case-preserving keys, no % interpolation; unknown sections -> skip with reason
def apply_file(cfg: StackConfig, path: str) -> int
    # skips unknown/empty/still-placeholder sections with printed reasons;
    # keyvault.set_secret_json per section; then triggers refresh; values never printed
```
Template file: one `[<server>]` section per DEPLOYED server with that server's
`CRED_SHAPE` keys (placeholder values), plus `[quantum-gaia]` gaia shape.

### 6.8 `hosting.py` (lazy-imports `azure.ai.projects`)

```python
def deploy_hosted_agent(cfg: StackConfig, env: dict[str, str]) -> str   # returns version id
    # AIProjectClient(endpoint=env['FOUNDRY_PROJECT_ENDPOINT'], credential=DefaultAzureCredential(), allow_preview=True)
    # create_version(agent_name=cfg.agent_name(), definition=HostedAgentDefinition(
    #   protocol_versions=[responses 2.0.0], cpu='1', memory='2Gi',
    #   container_configuration=ContainerConfiguration(image=image_ref(env['AZURE_CONTAINER_REGISTRY_ENDPOINT'])),
    #   environment_variables={CLAUDE_BASE_URL, CLAUDE_MODEL_DEPLOYMENT, KEY_VAULT_URI,
    #                          CONTENT_SAFETY_ENDPOINT, CHKP_SERVERS, CHKP_PREFIX, CHKP_GUARDRAIL}))
    # then route 100% traffic to it; poll status until 'active' (creating|active|failed)
def grant_agent_identity(cfg: StackConfig, env: dict[str, str]) -> None
def invoke_hosted_agent(cfg: StackConfig, env: dict[str, str], task: str,
                        *, session: str | None = None) -> dict
    # project.get_openai_client(agent_name=...).responses.create(input=task);
    # returns {"result","error"}; text beginning 'ERROR: ' => error True
def hosted_agent_status(cfg: StackConfig, env: dict[str, str]) -> dict | None
def refresh_hosted_agent(cfg: StackConfig, env: dict[str, str]) -> str
def delete_hosted_agent(cfg: StackConfig, env: dict[str, str]) -> None
```
FAIL-FAST parity (AWS `8dd198f`): `chat --runtime hosted` first calls
`hosted_agent_status`; if absent, exit 1 in seconds with
`No hosted agent '<name>' found -- deploy first: python3 -m chkpmcpaz deploy`
and never start a build. Honest errors: an `error:true` payload inside a
successful invoke prints `Hosted agent could not complete the task:` + message
+ `The runtime is up; this is a data-path issue -- python3 -m chkpmcpaz status`
and exits 1; transport failure exits 1 pointing at the unaffected local
runtime (`python3 -m chkpmcpaz chat "<task>"`). A **guardrail block is NOT an
error**: it comes back as a distinct `guardrail_block`/`blocked` outcome
(sentinel-tagged over the wire), renders the win banner, and exits 0.

### 6.9 `azutil.py`

```python
REPO_ROOT: pathlib.Path
def run(cmd: list[str], *, capture: bool = True, check: bool = True,
        env: Mapping | None = None, timeout: float | None = None) -> subprocess.CompletedProcess
    # never shell=True; raises AzCliError(cmd_name, returncode, stderr_tail) on failure
class AzCliError(RuntimeError): ...
def azd_env_values(prefix: str) -> dict[str, str]     # `azd env get-values -e <prefix>` parsed KEY="VALUE"
def parse_env_values(text: str) -> dict[str, str]     # pure, unit-tested
def get_credential():                                  # cached DefaultAzureCredential()
def have(cmd: str) -> bool                             # shutil.which
```

### 6.10 `ui.py`

Mirror the AWS UI contract: `class StepUI` with `step(name)` context manager,
checklist + per-step timers + streaming tail in TTY alt-screen mode; plain
line mode when not a TTY / `--plain` / `NO_COLOR` / `CHKP_UI=plain`; full
transcript tee'd to `~/.chkpmcpaz/logs/<command>-<timestamp>.log`
(override `CHKP_LOG_DIR`); terminal state ALWAYS restored (try/finally).
Secret values never reach the log (callers guarantee; ui does not filter).

### 6.11 `__main__.py`

```python
from chkpmcpaz.cli import main
import sys
if __name__ == "__main__":
    sys.exit(main())
```

## 7. TESTS builder -- `tests/**` + CI

Pure-logic suite: NO Azure calls, NO network, NO subprocesses that require
az/azd/node (mock/stub callables instead), no `mcp`/`azure-ai-projects`
imports at collection time. Target ~100 tests. Files (flat):

- `test_config.py` -- catalog invariants (15 servers, 9 default, `all`==11
  and excludes argos-erm/harmony-sase/workforce-ai/quantum-gaia; exact pins
  from CONTRACT section 12), `parse_servers` (default/all/names/unknown/
  dedupe), `secret_name`, `target_name`/`tool_namespace`/`split_namespaced`,
  `validate_prefix` matrix, `StackConfig.from_env` (incl. `CHKP_MODEL`
  overriding `CLAUDE_MODEL_DEPLOYMENT`), `sanitize_id`.
- `test_agent.py` -- `sanitize_tool_name`, `dedupe_names`, `build_tools`
  (mapping + cache_control on last tool), `is_transient` matrix,
  `first_api_error` (BaseExceptionGroup unwrap), truncation constants applied,
  captured-result shape (stubbed loop), telemetry line formatting.
- `test_cli.py` -- global-flag positioning (before/after subcommand), bare
  invocation help exit 0, missing chat task exit 2, `_is_credential_error`
  matrix (credential-shaped True; ValueError/RuntimeError False),
  KeyboardInterrupt exit 130, re-auth message text.
- `test_hosting_logic.py` -- fail-fast (absent agent -> exit 1, no build call:
  assert via stub), `error:true` -> exit 1, clean result -> exit 0,
  env-var dict passed to create_version contains exactly the section 6.8 keys.
- `test_keyvault.py` -- `is_placeholder` matrix, JSON round-trip against a
  stub client, never-clobber-real-values rule in deploy seeding helper.
- `test_creds.py` -- template generation content, INI parsing (case
  preserved, no interpolation), unknown/empty/placeholder section skips.
- `test_guardrail.py` -- response parsing (attackDetected true/false,
  documents array), request shape (api-version param, both fields present),
  `run_guardrail_test` interpretation matrix (stubbed screen_input).
- `test_gaia.py` -- `map_fields` alias matrix, decline-on-missing-required.
- `test_mcp_stdio.py` -- `npx_command` (pin + args), `build_child_env`
  (TELEMETRY_DISABLED, creds merged, no parent-secret leakage assertions),
  namespacing round-trip.
- `test_azutil.py` -- `parse_env_values` (quotes, equals-in-value, empty).
- `test_ui.py` -- plain-mode selection matrix (`NO_COLOR`, non-TTY,
  `CHKP_UI=plain`), log path shape.
- `test_tls.py` -- grep-style guard: no `verify=False` / `verify = False`
  anywhere under `chkpmcpaz/`; `requests.post` in guardrail.py not called
  with a `verify` kwarg (inspect source text).

CI `.github/workflows/tests.yml`: name `tests`; job `pytest`; on push+PR to
`main`; ubuntu-latest; matrix `python-version: ["3.11", "3.12", "3.13"]`;
steps: checkout@v4, setup-python@v5, `pip install ".[dev]"`,
`python -m pytest tests/ -q`. No credentials/secrets in CI.

## 8. DOCS builder -- `README.md` + `docs/**`

Replace the README stub with: what/why, architecture diagram reference
(`docs/img/`), quick start (`pip install -e ".[mcp,hosting,dev]"` -> `doctor`
-> `azd auth login`/`az login` -> `deploy` -> `status` -> `chat`), command
reference table (section 6.1), server catalog table (section 12 pins + cred
shapes), resource-name table (section 1), security notes (org policy), region
constraint (eastus2/swedencentral + why), Claude terms note (Bicep
`modelProviderData` auto-accepts Anthropic commercial terms; org/country/
industry sent to Anthropic), cost warnings.

`docs/commands.md` (full flag reference incl. exit codes),
`docs/servers.md` (15 servers, pins, credential env keys, gaia elicitation
note), `docs/resources.md` (every Azure resource + RBAC role created),
`docs/scenarios/quickstart.md`, `docs/scenarios/creds-and-golive.md`,
`docs/scenarios/guardrail.md` (optional guardrail demo covering BOTH engines --
default Azure Content Safety Prompt Shields and opt-in Check Point AI Guardrail
(Lakera Guard) via `--guardrail-provider lakera`, incl. the test payload; states
that MCP servers are always present and the guardrail is optional, the
cloud-native engine is the default and never forced, and Lakera is one API call
identical on AWS and Azure),
`docs/scenarios/local-probe.md` (`scripts/mcp_probe.mjs` usage),
`docs/aws-vs-azure.md`, `docs/diagrams/*.md`. All shell examples use `python3`.
Never include real hostnames/keys -- placeholders only.

**Multi-provider (cheap `gpt-5-mini` test path):** README gets a
"Test cheaply without Claude (like AWS Nova)" section (how to run on
`gpt-5-mini`, provider auto-detection, first-party/no-Marketplace/Dev-Test +
Visual Studio credits, exact commands). Every command/flag/env/name must match
this contract exactly: `--provider {auto,anthropic,azure-openai}`, `--model`,
`CHKP_PROVIDER`, `CHKP_MODEL`, `OPENAI_BASE_URL`, `OPENAI_MODEL_DEPLOYMENT`,
Bicep params `deployClaudeModels`/`deployOpenAiModel`/`openAiDeploymentName`/
`openAiModelVersion`/`openAiCapacity`, role `Cognitive Services OpenAI User`.
`docs/aws-vs-azure.md` maps Amazon Nova ↔ `gpt-5-mini` (cheap test) and
Claude ↔ Claude (prod), and notes Azure needs a real Provider abstraction
(Anthropic Messages vs OpenAI Chat Completions) where AWS got it free via
Bedrock Converse. `docs/diagrams/agent-flow.md` shows the provider seam
(`agent` → `providers.get_provider` → AnthropicFoundry | AzureOpenAI →
ServerPool → @chkp tools).

## 9. Cross-cutting UX text (exact strings, used by cli+tests)

- Re-auth message: section 6.2 block, verbatim.
- Interrupt: `Interrupted. Every command is idempotent -- re-run it to continue.`
- Turn budget: `stopped after 12 turns (turn budget reached)`
- Stream retry: `… model stream error — retrying …`
- Hosted fail-fast: `No hosted agent '<name>' found -- deploy first: python3 -m chkpmcpaz deploy`
- Hosted in-agent error prefix (real failures only): `Hosted agent could not complete the task:`
- Guardrail scan progress (before the screen): `guardrail  screening the prompt with <engine>…` (dim)
- Guardrail block is a deliberate DENY, not an error: ONE red line (firewall allow/deny colours; blocked = red) and **exits 0** (local and hosted, byte-identical to the AWS port): `🛡 Prompt blocked by Azure AI Content Safety Prompt Shields (attack detected).` (content-safety, default) or `🛡 Prompt blocked by Check Point AI Guardrail (attack detected): <detector>.` (lakera). The hosted container tags a block with the `GUARDRAIL_BLOCK: ` sentinel so the CLI never renders it through the error path (a legacy pre-sentinel container is still recognised by signature).
- Telemetry: `tokens  {in:,} in · {out:,} out · {cache_read:,} cache-read · {pct}% of input from cache`
- Destroy clean: `Nothing to destroy.`

## 10. Endpoint URL shapes (reference)

- Claude: `https://<account>.services.ai.azure.com/anthropic` (SDK appends `/v1/messages`; deployment name as `model`)
- `gpt-5-mini`: `https://<account>.services.ai.azure.com` (account ROOT, from `OPENAI_BASE_URL`; classic `AzureOpenAI` client appends `/openai/deployments/<model>/chat/completions`; api-version `2024-10-21`; deployment name as `model`)
- Project: `https://<account>.services.ai.azure.com/api/projects/<project>`
- Hosted agent Responses: `{project_endpoint}/agents/<prefix>-agent/endpoint/protocols/openai/responses`
- Content Safety: `POST {CONTENT_SAFETY_ENDPOINT}/contentsafety/text:shieldPrompt?api-version=2024-09-01`
- Key Vault: `https://<kv-name>.vault.azure.net/`

## 11. Definition of done (per builder)

- infra: `az bicep build --file infra/main.bicep` clean; outputs exactly as 4.2.
- runtime: `python3 -c "import chkpmcpaz.agent, chkpmcpaz.mcp_stdio, chkpmcpaz.keyvault, chkpmcpaz.guardrail, chkpmcpaz.gaia"` succeeds WITHOUT `mcp`/azure extras installed (lazy imports); signatures match section 5.
- cli: `python3 -m chkpmcpaz --help` exit 0; every subcommand parses; no module import requires `mcp`/`azure-ai-projects` unless its command runs.
- tests: `python3 -m pytest tests/ -q` green on 3.11-3.13 with ONLY `.[dev]` installed.
- docs: every command/flag documented matches section 6.1; all examples `python3`.

## 12. Server catalog pins (frozen; already encoded in config.py)

quantum-management@1.4.7, management-logs@1.4.6, threat-prevention@1.5.4,
https-inspection@1.4.6, policy-insights@0.3.5, quantum-gw-cli@1.4.8,
reputation-service@1.3.1, threat-emulation@1.3.1, documentation@1.4.6
(args `--region US`) -- the default 9; cloudguard-waf@0.1.0,
spark-management@1.4.8 -- complete `--servers all` (11); argos-erm@0.5.4,
harmony-sase@1.3.1, workforce-ai@1.1.0, quantum-gaia@1.3.5 -- explicit-only.
