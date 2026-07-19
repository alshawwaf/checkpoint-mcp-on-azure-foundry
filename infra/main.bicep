// main.bicep -- subscription-scope entry point for `azd provision`.
//
// Creates rg-<environmentName> and, inside it: the Foundry account + project +
// Claude (Anthropic) model deployments, Key Vault (Check Point credentials --
// names only here, never values), ACR (hosted-agent image), Log Analytics +
// Application Insights, and the provision-time role assignments. The hosted
// agent itself is data-plane and is created afterwards by the CLI
// (python3 -m chkpmcpaz deploy) -- see azure.yaml.
//
// Ordering (the one non-obvious part): foundry runs TWICE. Phase 1 creates
// the account + project so their managed identities exist; roles.bicep then
// assigns RBAC; phase 2 (dependsOn roles) adds the Claude deployments, so
// role propagation overlaps the 30s-20min model-deployment LRO instead of
// starting after it. Re-PUTting the unchanged account/project is an ARM no-op.
//
// Every name below is fixed by CONTRACT.md section 1 and mirrored by
// chkpmcpaz/config.py -- destroy always finds what deploy made.

targetScope = 'subscription'

@description('azd environment name == stack prefix (must match ^[a-z][a-z0-9-]{0,11}$), e.g. chkpmcp.')
@minLength(1)
@maxLength(12)
param environmentName string

@description('Region hosting BOTH Foundry Hosted Agents and Claude starter-kit accounts (July 2026 overlap).')
@allowed(['eastus2', 'swedencentral'])
param location string

@description('Object id of the deploying user (azd injects AZURE_PRINCIPAL_ID). Empty skips deployer role grants.')
param principalId string = ''

@description('Principal type of the deployer for the role assignments. azd runs as a signed-in User by default; CI deploying under a service principal MUST pass ServicePrincipal (azd env set AZURE_PRINCIPAL_TYPE ServicePrincipal) so ARM skips the AAD replication check correctly and avoids PrincipalNotFound.')
@allowed(['User', 'ServicePrincipal'])
param principalType string = 'User'

@description('Organization name sent to Anthropic via modelProviderData; Bicep deploys auto-accept the Anthropic commercial terms. Optional (default empty) so a gpt-only test stack (deployClaudeModels:false) validates without it; deploy.py enforces it for the anthropic path.')
param claudeOrganizationName string = ''

@description('2-letter ISO country code sent to Anthropic.')
@minLength(2)
@maxLength(2)
param claudeCountryCode string = 'US'

@description('Industry sent to Anthropic -- MUST be a lowercase enum value.')
@allowed(['technology', 'finance', 'healthcare', 'education', 'retail', 'manufacturing', 'government', 'media', 'other'])
param claudeIndustry string = 'technology'

@description('Per-deployment capacity in thousands of tokens-per-minute (GlobalStandard sku).')
@minValue(1)
param claudeCapacity int = 25

@description('Also deploy claude-haiku-4-5 as the cheaper fallback deployment. Mapped to DEPLOY_FALLBACK_MODEL (azd env set to change).')
param deployFallbackModel bool = true

@description('Deploy the Claude (Anthropic) model deployments. Mapped to DEPLOY_CLAUDE_MODELS. A test/azure-openai deploy sets this false so no Claude deployment is attempted on an ineligible subscription.')
param deployClaudeModels bool = true

@description('Deploy the first-party Azure OpenAI test deployment (gpt-5-mini). Mapped to DEPLOY_OPENAI_MODEL. First-party, so it deploys on credit/MSDN/Dev-Test subscriptions where Claude is blocked.')
param deployOpenAiModel bool = false

@description('Deployment name (== model name) for the Azure OpenAI test model. Mapped to OPENAI_DEPLOYMENT_NAME.')
param openAiDeploymentName string = 'gpt-5-mini'

@description('Model version for the Azure OpenAI test model. Mapped to OPENAI_MODEL_VERSION (gpt-5-mini GA text version).')
param openAiModelVersion string = '2025-08-07'

@description('Per-deployment capacity in thousands of tokens-per-minute for the Azure OpenAI test model (GlobalStandard). Mapped to OPENAI_CAPACITY.')
@minValue(1)
param openAiCapacity int = 50

@description('Public network access for the credential Key Vault. Enabled (default) keeps the operator-run deploy/creds flow working; Disabled restricts to trusted Azure services + private networking. Mapped to KEY_VAULT_PUBLIC_NETWORK_ACCESS.')
@allowed(['Enabled', 'Disabled'])
param keyVaultPublicNetworkAccess string = 'Enabled'

var resourceToken = uniqueString(subscription().id, environmentName)

var tags = {
  project: 'chkp-mcp-foundry'
  stack: environmentName
  'azd-env-name': environmentName
}

// Fixed names (CONTRACT.md section 1). Key Vault caps at 24 chars; ACR is
// alphanumeric-only and caps at 50. resourceToken is 13 lowercase alnum chars,
// so with the 12-char max prefix every take() still ends on an alnum char.
var accountName = '${environmentName}-foundry-${resourceToken}'
var projectName = '${environmentName}-project'
var keyVaultName = take('kv-${environmentName}-${resourceToken}', 24)
var registryName = take('acr${replace(environmentName, '-', '')}${resourceToken}', 50)
var logAnalyticsName = 'log-${environmentName}-${resourceToken}'
var applicationInsightsName = 'appi-${environmentName}-${resourceToken}'

resource rg 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: 'rg-${environmentName}'
  location: location
  tags: tags
}

module monitoring 'modules/monitoring.bicep' = {
  name: 'monitoring'
  scope: rg
  params: {
    logAnalyticsName: logAnalyticsName
    applicationInsightsName: applicationInsightsName
    location: location
    tags: tags
  }
}

module keyVault 'modules/keyvault.bicep' = {
  name: 'keyvault'
  scope: rg
  params: {
    keyVaultName: keyVaultName
    location: location
    tags: tags
    publicNetworkAccess: keyVaultPublicNetworkAccess
  }
}

module registry 'modules/registry.bicep' = {
  name: 'registry'
  scope: rg
  params: {
    registryName: registryName
    location: location
    tags: tags
  }
}

// Phase 1 -- Foundry account + project only (no model deployments yet): the
// project managed identity has to exist before roles.bicep can grant it.
module foundryCore 'modules/foundry.bicep' = {
  name: 'foundry-core'
  scope: rg
  params: {
    accountName: accountName
    projectName: projectName
    location: location
    tags: tags
    claudeOrganizationName: claudeOrganizationName
    claudeCountryCode: claudeCountryCode
    claudeIndustry: claudeIndustry
    claudeCapacity: claudeCapacity
    deployFallbackModel: deployFallbackModel
    deployClaudeModels: deployClaudeModels
    deployOpenAiModel: deployOpenAiModel
    openAiDeploymentName: openAiDeploymentName
    openAiModelVersion: openAiModelVersion
    openAiCapacity: openAiCapacity
    deployModels: false
  }
}

module roles 'modules/roles.bicep' = {
  name: 'roles'
  scope: rg
  params: {
    principalId: principalId
    principalType: principalType
    projectPrincipalId: foundryCore.outputs.projectPrincipalId
    accountName: foundryCore.outputs.accountName
    keyVaultName: keyVault.outputs.keyVaultName
    registryName: registry.outputs.registryName
  }
}

// Phase 2 -- same module with the Claude deployments enabled. dependsOn roles
// so every assignment is in place while the model-deployment LRO runs.
module foundry 'modules/foundry.bicep' = {
  name: 'foundry'
  scope: rg
  dependsOn: [
    roles
  ]
  params: {
    accountName: accountName
    projectName: projectName
    location: location
    tags: tags
    claudeOrganizationName: claudeOrganizationName
    claudeCountryCode: claudeCountryCode
    claudeIndustry: claudeIndustry
    claudeCapacity: claudeCapacity
    deployFallbackModel: deployFallbackModel
    deployClaudeModels: deployClaudeModels
    deployOpenAiModel: deployOpenAiModel
    openAiDeploymentName: openAiDeploymentName
    openAiModelVersion: openAiModelVersion
    openAiCapacity: openAiCapacity
    deployModels: true
  }
}

// Outputs -- EXACT names per CONTRACT.md 4.2: azd copies them into the env
// and chkpmcpaz reads them back via `azd env get-values`.
output AZURE_LOCATION string = location
output AZURE_TENANT_ID string = tenant().tenantId
output AZURE_RESOURCE_GROUP string = rg.name
output FOUNDRY_ACCOUNT_NAME string = foundry.outputs.accountName
output FOUNDRY_PROJECT_NAME string = foundry.outputs.projectName
output FOUNDRY_PROJECT_ENDPOINT string = foundry.outputs.projectEndpoint
output CLAUDE_BASE_URL string = foundry.outputs.claudeBaseUrl
output CLAUDE_MODEL_DEPLOYMENT string = foundry.outputs.primaryDeployment
output CLAUDE_FALLBACK_DEPLOYMENT string = foundry.outputs.fallbackDeployment
// First-party Azure OpenAI test target. OPENAI_BASE_URL is the account ROOT
// (the OpenAI client appends its own route); OPENAI_MODEL_DEPLOYMENT is '' when
// the gpt model was not deployed. CLAUDE_* likewise emit '' on a gpt-only stack.
output OPENAI_BASE_URL string = foundry.outputs.openaiBaseUrl
output OPENAI_MODEL_DEPLOYMENT string = foundry.outputs.openaiDeployment
output KEY_VAULT_NAME string = keyVault.outputs.keyVaultName
output KEY_VAULT_URI string = keyVault.outputs.keyVaultUri
output AZURE_CONTAINER_REGISTRY_NAME string = registry.outputs.registryName
output AZURE_CONTAINER_REGISTRY_ENDPOINT string = registry.outputs.registryLoginServer
output CONTENT_SAFETY_ENDPOINT string = foundry.outputs.contentSafetyEndpoint
output APPLICATIONINSIGHTS_CONNECTION_STRING string = monitoring.outputs.applicationInsightsConnectionString
// Name (not just connection string) so the CLI can print a clickable portal
// link to the agent traces after deploy/status. Optional for older envs.
output APPLICATIONINSIGHTS_NAME string = monitoring.outputs.applicationInsightsName
