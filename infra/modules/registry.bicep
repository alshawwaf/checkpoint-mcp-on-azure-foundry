// registry.bicep -- Basic ACR for the single hosted-agent image
// (<login server>/chkp-agent:v1, built remotely with `az acr build` so no
// local Docker is needed). Admin user is OFF and azureADAuthenticationAsArmPolicy
// is ENABLED by requirement: Foundry Hosted Agents pull through the project
// managed identity's AcrPull grant (roles.bicep) using ARM-audience Entra
// tokens, and org policy forbids shared admin credentials anywhere.

@description('Registry name (acr<prefix-no-hyphens><resourceToken>, alphanumeric, trimmed to 50 chars).')
param registryName string

@description('Azure region for the registry.')
param location string

@description('Tags applied to every resource in the stack.')
param tags object = {}

resource registry 'Microsoft.ContainerRegistry/registries@2025-11-01' = {
  name: registryName
  location: location
  sku: {
    name: 'Basic'
  }
  tags: tags
  properties: {
    adminUserEnabled: false
    policies: {
      azureADAuthenticationAsArmPolicy: {
        status: 'enabled'
      }
    }
  }
}

output registryName string = registry.name
output registryLoginServer string = registry.properties.loginServer
output registryId string = registry.id
