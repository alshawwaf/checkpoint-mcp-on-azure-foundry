// foundry.bicep -- Microsoft Foundry account + project + the model deployments
// (Claude on Foundry for production; first-party Azure OpenAI gpt-5-mini for
// cheap testing). Resource types and API versions follow the
// Azure-Samples/claude starter kit exactly.
//
// Design notes:
// - The account MUST set allowProjectManagement: true (projects cannot be
//   created otherwise) and a customSubDomainName (the /anthropic route, the
//   /openai/v1 route, and the project endpoint all hang off the subdomain).
// - Claude deployments use model.format 'Anthropic' with the REQUIRED
//   modelProviderData block. Deploying via Bicep AUTO-ACCEPTS Anthropic's
//   commercial terms; organizationName/countryCode/industry are attested to
//   Anthropic. This block carries business metadata only -- never secrets.
// - The gpt-5-mini deployment is FIRST-PARTY (model.format 'OpenAI', NO
//   modelProviderData): it is not an Anthropic/Marketplace offer, so it
//   deploys on credit/MSDN/Dev-Test subscriptions where Claude is blocked --
//   the Azure analogue of using Amazon Nova instead of Claude on AWS Bedrock.
// - `deployClaudeModels` / `deployOpenAiModel` select which model family this
//   stack provisions. A test/azure-openai deploy sets deployClaudeModels:false
//   so it never attempts to create Claude on an ineligible subscription; a
//   production deploy leaves the Claude default and deployOpenAiModel:false.
// - `deployModels` exists so main.bicep can call this module twice: phase 1
//   (false) creates the account + project so their managed identities exist
//   for roles.bicep; phase 2 (true, dependsOn roles) adds the deployments,
//   letting RBAC propagate DURING the 30s-20min model-deployment LRO instead
//   of after it (starter-kit ordering). Re-PUTting the unchanged account and
//   project in phase 2 is an ARM no-op.
// - Multiple deployments under one account are serialized with dependsOn --
//   concurrent deployment PUTs 409.

@description('Foundry account name (<prefix>-foundry-<resourceToken>); also the custom subdomain.')
param accountName string

@description('Foundry project name (<prefix>-project).')
param projectName string

@description('Azure region for the account and project.')
param location string

@description('Tags applied to every resource in the stack.')
param tags object = {}

@description('Organization name sent to Anthropic via modelProviderData.')
param claudeOrganizationName string

@description('2-letter ISO country code sent to Anthropic.')
param claudeCountryCode string

@description('Industry sent to Anthropic -- must be a lowercase enum value.')
param claudeIndustry string

@description('Per-deployment capacity in thousands of tokens-per-minute (GlobalStandard).')
param claudeCapacity int

@description('Also deploy claude-haiku-4-5 as the cheaper fallback deployment.')
param deployFallbackModel bool

@description('Deploy the Claude (Anthropic) model deployments. Set false for a test/azure-openai stack so no Claude deployment is attempted on an ineligible subscription.')
param deployClaudeModels bool = true

@description('Deploy the first-party Azure OpenAI test deployment (gpt-5-mini). Set true for a cheap test stack on a credit/MSDN/Dev-Test subscription.')
param deployOpenAiModel bool = false

@description('Deployment name (== model name) for the first-party Azure OpenAI test model.')
param openAiDeploymentName string = 'gpt-5-mini'

@description('Model version for the first-party Azure OpenAI test model (gpt-5-mini GA text version).')
param openAiModelVersion string = '2025-08-07'

@description('Per-deployment capacity in thousands of tokens-per-minute for the Azure OpenAI test model (GlobalStandard).')
param openAiCapacity int = 50

@description('False = account + project only (phase 1); true = also the model deployments (phase 2).')
param deployModels bool = true

// Deployment name == model name by design; chkpmcpaz passes the DEPLOYMENT
// name as `model` to AnthropicFoundry (mirrors config.CLAUDE_DEPLOYMENTS).
var primaryDeploymentName = 'claude-sonnet-4-6'
var fallbackDeploymentName = 'claude-haiku-4-5'

resource account 'Microsoft.CognitiveServices/accounts@2025-10-01-preview' = {
  name: accountName
  location: location
  kind: 'AIServices'
  sku: {
    name: 'S0'
  }
  identity: {
    type: 'SystemAssigned'
  }
  tags: tags
  properties: {
    customSubDomainName: accountName
    allowProjectManagement: true
    publicNetworkAccess: 'Enabled'
    // Key-based (local) auth is DISABLED: this single account serves both the
    // Claude /anthropic inference route and the Content Safety Prompt Shields
    // endpoint, and every caller in this repo authenticates with Entra bearer
    // tokens (DefaultAzureCredential). Leaving shared keys enabled would let
    // anyone able to listKeys -- or anyone holding a leaked key -- bypass the
    // per-identity `Cognitive Services User` grants (deployer + agent identity)
    // and their audit trail. Same posture as the ACR admin user (registry.bicep)
    // and the RBAC-only Key Vault: no shared credentials anywhere.
    disableLocalAuth: true
  }
}

// A project is REQUIRED for Claude and hosts the hosted agent + its sessions.
// Its system-assigned identity is the "project managed identity" that pulls
// the agent image from ACR (AcrPull granted in roles.bicep).
resource project 'Microsoft.CognitiveServices/accounts/projects@2025-10-01-preview' = {
  parent: account
  name: projectName
  location: location
  identity: {
    type: 'SystemAssigned'
  }
  tags: tags
  properties: {}
}

resource claudePrimary 'Microsoft.CognitiveServices/accounts/deployments@2025-10-01-preview' = if (deployModels && deployClaudeModels) {
  parent: account
  name: primaryDeploymentName
  tags: tags
  sku: {
    name: 'GlobalStandard'
    capacity: claudeCapacity
  }
  properties: {
    model: {
      format: 'Anthropic'
      name: primaryDeploymentName
      version: '1'
    }
    // Bicep's bundled type definitions lag the 2025-10-01-preview API;
    // modelProviderData is REQUIRED for Anthropic deployments and accepted by
    // ARM (Azure-Samples/claude ships this exact shape).
    #disable-next-line BCP037
    modelProviderData: {
      organizationName: claudeOrganizationName
      countryCode: claudeCountryCode
      industry: claudeIndustry
    }
    versionUpgradeOption: 'OnceNewDefaultVersionAvailable'
    raiPolicyName: 'Microsoft.DefaultV2'
  }
  dependsOn: [
    project
  ]
}

resource claudeFallback 'Microsoft.CognitiveServices/accounts/deployments@2025-10-01-preview' = if (deployModels && deployClaudeModels && deployFallbackModel) {
  parent: account
  name: fallbackDeploymentName
  tags: tags
  sku: {
    name: 'GlobalStandard'
    capacity: claudeCapacity
  }
  properties: {
    model: {
      format: 'Anthropic'
      name: fallbackDeploymentName
      version: '1'
    }
    #disable-next-line BCP037 // see claudePrimary -- type-definition lag
    modelProviderData: {
      organizationName: claudeOrganizationName
      countryCode: claudeCountryCode
      industry: claudeIndustry
    }
    versionUpgradeOption: 'OnceNewDefaultVersionAvailable'
    raiPolicyName: 'Microsoft.DefaultV2'
  }
  dependsOn: [
    claudePrimary // serialize -- concurrent deployment PUTs under one account 409
  ]
}

// First-party Azure OpenAI test deployment (gpt-5-mini). model.format 'OpenAI'
// with NO modelProviderData -- the DeploymentModel schema (format/name/version)
// is all a first-party OpenAI model needs, so this deploys on credit/MSDN/
// Dev-Test subscriptions where the Anthropic/Marketplace Claude offer is
// blocked. Serialized AFTER the Claude deployments so a rare deploy of BOTH
// families under one account does not 409 (each is a no-op when its family is
// not selected, so the chain is cost-free on a single-family stack).
resource openAiModel 'Microsoft.CognitiveServices/accounts/deployments@2025-10-01-preview' = if (deployModels && deployOpenAiModel) {
  parent: account
  name: openAiDeploymentName
  tags: tags
  sku: {
    name: 'GlobalStandard'
    capacity: openAiCapacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: openAiDeploymentName
      version: openAiModelVersion
    }
    versionUpgradeOption: 'OnceCurrentVersionExpired'
    raiPolicyName: 'Microsoft.DefaultV2'
  }
  dependsOn: [
    project
    claudePrimary // serialize under one account (no-op when Claude is not deployed)
    claudeFallback
  ]
}

output accountName string = account.name
output accountId string = account.id
output accountPrincipalId string = account.identity.principalId
output projectName string = project.name
output projectPrincipalId string = project.identity.principalId
output projectEndpoint string = 'https://${account.name}.services.ai.azure.com/api/projects/${project.name}'
// Account-level Anthropic route; the SDK appends /v1/messages. NOT proxied by
// the project endpoint -- callers need Cognitive Services User on the account.
output claudeBaseUrl string = 'https://${account.name}.services.ai.azure.com/anthropic'
// The account endpoint doubles as the Content Safety endpoint for the
// Prompt Shields guardrail (POST .../contentsafety/text:shieldPrompt).
output contentSafetyEndpoint string = account.properties.endpoint
output primaryDeployment string = (deployModels && deployClaudeModels) ? primaryDeploymentName : ''
output fallbackDeployment string = (deployModels && deployClaudeModels && deployFallbackModel) ? fallbackDeploymentName : ''
// First-party Azure OpenAI target. openaiBaseUrl is the account ROOT (no route
// suffix): the classic AzureOpenAI client appends /openai, the v1 OpenAI client
// appends /openai/v1 -- the provider's make_client owns that choice.
output openaiBaseUrl string = 'https://${account.name}.services.ai.azure.com'
output openaiDeployment string = (deployModels && deployOpenAiModel) ? openAiDeploymentName : ''
