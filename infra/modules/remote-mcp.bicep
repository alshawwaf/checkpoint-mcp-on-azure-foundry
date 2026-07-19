// remote-mcp.bicep -- the opt-in remote MCP tier (`deploy --remote-mcp`): the
// Azure analogue of the AWS AgentCore Gateway. Each selected @chkp server runs
// as its own scale-to-zero Azure Container App in streamable-HTTP transport
// mode, fronted by Entra Easy Auth, so a SECOND consumer (Foundry portal
// agents, Copilot Studio, Claude Desktop, another MCP client) can use the same
// tools -- not just this stack's agent via its stdio children.
//
// Deployed IMPERATIVELY by the CLI (az deployment group create) AFTER the agent
// image is built and the gateway Entra app registration exists -- NOT part of
// `azd provision`. Same reason the hosted agent is imperative: the container
// image must exist first, and Easy Auth needs the app-registration client id.
//
// Names come from chkpmcpaz/config.py (single source of truth) and are passed
// in, so destroy always finds what deploy made.

@description('Azure region.')
param location string

@description('Tags applied to every resource.')
param tags object = {}

@description('Name of the stack Log Analytics workspace (existing) -- ACA logs are wired to it.')
param logAnalyticsName string

@description('ACR name (existing) hosting the agent image -- AcrPull granted to the shared identity.')
param registryName string

@description('ACR login server (e.g. acr<...>.azurecr.io).')
param registryLoginServer string

@description('Agent image reference (digest-pinned when known).')
param agentImage string

@description('Key Vault name (existing) holding per-server credential secrets.')
param keyVaultName string

@description('Key Vault URI (https://<name>.vault.azure.net/).')
param keyVaultUri string

@description('Container Apps managed-environment name (config.container_env_name).')
param containerEnvName string

@description('Shared user-assigned identity name (config.remote_identity_name).')
param identityName string

@description('Entra app-registration client id for the Easy Auth audience (api://<clientId>).')
param audienceClientId string

@description('Per-server descriptors from config.remote_server_descriptors: {server, appName, package, args, secretName, target}.')
param servers array

var tenantId = tenant().tenantId
// Built-in role definition ids (stable GUIDs).
var acrPullRoleId = '7f951dda-4ed3-4680-a7ca-43fe172d538d'
var kvSecretsUserRoleId = '4633458b-17de-408a-b874-0445c86b69e6'

resource la 'Microsoft.OperationalInsights/workspaces@2023-09-01' existing = {
  name: logAnalyticsName
}

resource registry 'Microsoft.ContainerRegistry/registries@2025-11-01' existing = {
  name: registryName
}

resource vault 'Microsoft.KeyVault/vaults@2024-11-01' existing = {
  name: keyVaultName
}

// One shared user-assigned identity for every remote-MCP app: pull the agent
// image (AcrPull) and read the per-server credential secrets (KV Secrets User).
resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: identityName
  location: location
  tags: tags
}

resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, identity.id, acrPullRoleId)
  scope: registry
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleId)
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource kvUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(vault.id, identity.id, kvSecretsUserRoleId)
  scope: vault
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', kvSecretsUserRoleId)
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource env 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: containerEnvName
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: la.properties.customerId
        sharedKey: la.listKeys().primarySharedKey
      }
    }
  }
}

// One Container App (+ its Easy Auth config) per server. dependsOn the role
// assignments so the identity can pull the image / read secrets on first start.
module apps 'remote-mcp-app.bicep' = [for s in servers: {
  name: 'mcp-app-${s.appName}'
  params: {
    location: location
    tags: tags
    appName: s.appName
    managedEnvironmentId: env.id
    identityId: identity.id
    identityClientId: identity.properties.clientId
    registryLoginServer: registryLoginServer
    agentImage: agentImage
    keyVaultUri: keyVaultUri
    packageSpec: s.package
    args: s.args
    secretName: s.secretName
    tenantId: tenantId
    audienceClientId: audienceClientId
  }
  dependsOn: [
    acrPull
    kvUser
  ]
}]

// The catalog the CLI persists as CHKP_REMOTE_MCP and the client reads back:
// one {server, url} per app (url = https://<fqdn>/mcp, added CLI-side).
output endpoints array = [for (s, i) in servers: {
  server: s.server
  appName: s.appName
  fqdn: apps[i].outputs.fqdn
}]
output identityPrincipalId string = identity.properties.principalId
output environmentId string = env.id
