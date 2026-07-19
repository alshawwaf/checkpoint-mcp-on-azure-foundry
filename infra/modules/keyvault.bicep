// keyvault.bicep -- one RBAC-mode vault for ALL Check Point credentials
// (per-server JSON secrets named <prefix>-<server>, e.g.
// chkpmcp-quantum-management). Secret VALUES never appear in Bicep or
// parameter files -- the CLI seeds placeholder bodies post-provision and
// `python3 -m chkpmcpaz creds apply` writes the real ones (org policy:
// secrets live in Key Vault only). Soft delete stays on with a 7-day window
// so destroy/redeploy mirrors the AWS Secrets Manager RecoveryWindowInDays=7
// behavior (deploy recovers a soft-deleted secret instead of failing).

@description('Key Vault name (kv-<prefix>-<resourceToken>, trimmed to 24 chars).')
param keyVaultName string

@description('Azure region for the vault.')
param location string

@description('Tags applied to every resource in the stack.')
param tags object = {}

@description('Public network access for the vault. Defaults to Enabled because the CLI seeds/updates the per-server credential secrets from the OPERATOR machine (deploy, creds apply) over the internet. Set Disabled (or wire a private endpoint) when the vault is only reached from inside Azure -- the agent identity reaches it via the AzureServices bypass either way. Mapped to KEY_VAULT_PUBLIC_NETWORK_ACCESS.')
@allowed(['Enabled', 'Disabled'])
param publicNetworkAccess string = 'Enabled'

resource vault 'Microsoft.KeyVault/vaults@2024-11-01' = {
  name: keyVaultName
  location: location
  tags: tags
  properties: {
    tenantId: tenant().tenantId
    sku: {
      family: 'A'
      name: 'standard'
    }
    // RBAC authorization only -- access is granted via roles.bicep (deployer:
    // Key Vault Secrets Officer) and by the CLI post-create (agent identity:
    // Key Vault Secrets User). No legacy access policies anywhere.
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 7
    // Network boundary in FRONT of RBAC (defense in depth for a store that
    // holds every server's real Check Point credentials). Default action is
    // Deny once public access is Disabled, but trusted Azure services keep
    // reachability via the bypass; with the default (Enabled) the vault behaves
    // as before so the operator-run deploy/creds flow is not broken.
    publicNetworkAccess: publicNetworkAccess
    networkAcls: {
      bypass: 'AzureServices'
      defaultAction: publicNetworkAccess == 'Enabled' ? 'Allow' : 'Deny'
    }
  }
}

output keyVaultName string = vault.name
output keyVaultUri string = vault.properties.vaultUri
output keyVaultId string = vault.id
