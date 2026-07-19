// roles.bicep -- role assignments for the deploying principal and the Foundry
// PROJECT managed identity. Split into its own module so main.bicep can order
// it BEFORE the Claude model deployments (phase-2 foundry module dependsOn
// this one): the deployment LRO runs 30s-20min, and assigning roles first
// lets RBAC propagate while it runs instead of after (starter-kit ordering).
//
// NOT here by design: the hosted agent's per-agent Entra identity does not
// exist at provision time -- it is created with the agent version on the data
// plane. Its grants (Cognitive Services User at account scope for the
// /anthropic route + Prompt Shields, Key Vault Secrets User
// 4633458b-17de-408a-b874-0445c86b69e6 on the vault) are made by the CLI
// post-create (chkpmcpaz hosting.grant_agent_identity). Do not add them here.
//
// All assignment names are deterministic guid(...) so re-provisioning is
// idempotent, and every assignment sets principalType to avoid the
// PrincipalNotFound replication race on freshly created identities.

@description('Object id of the deploying user/SP (azd injects AZURE_PRINCIPAL_ID). Empty skips the deployer grants.')
param principalId string = ''

@description('Principal type of the deployer, forwarded from main.bicep (mapped to AZURE_PRINCIPAL_TYPE, default User). CI deploying under a service principal must set it to ServicePrincipal so ARM skips the AAD replication check for the freshly-created SP and avoids PrincipalNotFound.')
@allowed(['User', 'ServicePrincipal'])
param principalType string = 'User'

@description('Principal id of the Foundry PROJECT system-assigned managed identity (pulls the agent image).')
param projectPrincipalId string

@description('Foundry account name (role scope for model inference + agent management).')
param accountName string

@description('Key Vault name (role scope for the Check Point credential secrets).')
param keyVaultName string

@description('Container registry name (role scope for image push/pull).')
param registryName string

// Built-in role definition ids (global constants, subscription-independent).
// 'Foundry Project Manager' eadc314b-... verified live 2026-07-17 via
// `az role definition list` (formerly named 'Azure AI Project Manager').
var cognitiveServicesUserRoleId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'a97b65f3-24c7-4388-baec-2e87135dc908')
var foundryProjectManagerRoleId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'eadc314b-1a2d-4efa-be10-5d325db5065e')
var keyVaultSecretsOfficerRoleId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'b86a8fe4-44ce-4948-aee5-eccb2c155cd7')
var acrPushRoleId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '8311e382-0749-4cb8-b61a-304f252e45ec')
var acrPullRoleId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')

resource account 'Microsoft.CognitiveServices/accounts@2025-10-01-preview' existing = {
  name: accountName
}

resource vault 'Microsoft.KeyVault/vaults@2024-11-01' existing = {
  name: keyVaultName
}

resource registry 'Microsoft.ContainerRegistry/registries@2025-11-01' existing = {
  name: registryName
}

// ---------------------------------------------------------------------------
// Deployer grants (skipped when principalId is empty, e.g. some CI flows)
// ---------------------------------------------------------------------------

// Model inference: lets the deployer call the /anthropic route and Content
// Safety (Prompt Shields) keylessly the moment the deployments finish.
resource deployerCognitiveServicesUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(principalId)) {
  name: guid(account.id, principalId, 'a97b65f3-24c7-4388-baec-2e87135dc908')
  scope: account
  properties: {
    roleDefinitionId: cognitiveServicesUserRoleId
    principalId: principalId
    principalType: principalType
  }
}

// Data-plane agent management: create/update hosted agent versions, route
// traffic, invoke. Owner/Contributor do NOT cover this (control plane only).
// Equivalent manual grant if this role id ever changes in your cloud:
//   az role assignment create --role "Foundry Project Manager" \
//     --assignee-object-id <principalId> --assignee-principal-type User \
//     --scope <account resource id>
resource deployerFoundryProjectManager 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(principalId)) {
  name: guid(account.id, principalId, 'eadc314b-1a2d-4efa-be10-5d325db5065e')
  scope: account
  properties: {
    roleDefinitionId: foundryProjectManagerRoleId
    principalId: principalId
    principalType: principalType
  }
}

// Seed/apply the per-server credential secrets (placeholder bodies at deploy,
// real values via `python3 -m chkpmcpaz creds apply`).
resource deployerKeyVaultSecretsOfficer 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(principalId)) {
  name: guid(vault.id, principalId, 'b86a8fe4-44ce-4948-aee5-eccb2c155cd7')
  scope: vault
  properties: {
    roleDefinitionId: keyVaultSecretsOfficerRoleId
    principalId: principalId
    principalType: principalType
  }
}

// Push chkp-agent:v1 via `az acr build`.
resource deployerAcrPush 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(principalId)) {
  name: guid(registry.id, principalId, '8311e382-0749-4cb8-b61a-304f252e45ec')
  scope: registry
  properties: {
    roleDefinitionId: acrPushRoleId
    principalId: principalId
    principalType: principalType
  }
}

// ---------------------------------------------------------------------------
// Project managed-identity grants
// ---------------------------------------------------------------------------

// The hosted agent's image is pulled by the PROJECT managed identity (infra
// identity), not the per-agent identity.
resource projectAcrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, projectPrincipalId, '7f951dda-4ed3-4680-a7ca-43fe172d538d')
  scope: registry
  properties: {
    roleDefinitionId: acrPullRoleId
    principalId: projectPrincipalId
    principalType: 'ServicePrincipal'
  }
}
