// remote-mcp-app.bicep -- ONE @chkp MCP server as a scale-to-zero Azure
// Container App in streamable-HTTP transport mode, fronted by Entra Easy Auth
// (unauthenticated requests get 401 -- org policy: every endpoint authenticated).
// Looped per selected server by remote-mcp.bicep.
//
// The container runs the SAME agent image (agent/Dockerfile) with a different
// command -- `python -m chkpmcpaz.remote_server` -- which pulls the server's
// Key Vault secret via the shared user-assigned identity and execs the pinned
// @chkp package with `--transport http`. No second image is built.

@description('Azure region.')
param location string

@description('Tags applied to the Container App.')
param tags object = {}

@description('Container App name (config.container_app_name: <prefix>-mcp-<server>).')
param appName string

@description('Resource id of the shared Container Apps managed environment.')
param managedEnvironmentId string

@description('Resource id of the shared user-assigned managed identity (AcrPull + KV Secrets User).')
param identityId string

@description('Client id of that identity -- exported as AZURE_CLIENT_ID so DefaultAzureCredential in the container selects it.')
param identityClientId string

@description('ACR login server (e.g. acr<...>.azurecr.io) the image is pulled from.')
param registryLoginServer string

@description('Agent image reference (digest-pinned when known).')
param agentImage string

@description('Key Vault URI the container reads its credential secret from.')
param keyVaultUri string

@description('Pinned @chkp package to run (e.g. @chkp/quantum-management-mcp@1.4.7).')
param packageSpec string

@description('Extra server args (space-separated), e.g. "--region US". May be empty.')
param args string = ''

@description('Key Vault secret name to hydrate into the child env. Empty for creds-less servers.')
param secretName string = ''

@description('Tenant id for the Easy Auth Entra issuer.')
param tenantId string

@description('Entra app-registration client id whose audience (api://<clientId>) Easy Auth requires.')
param audienceClientId string

@description('HTTP transport port the @chkp server listens on and ingress targets.')
param httpPort int = 8000

resource app 'Microsoft.App/containerApps@2024-03-01' = {
  name: appName
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${identityId}': {}
    }
  }
  properties: {
    managedEnvironmentId: managedEnvironmentId
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: httpPort
        transport: 'auto'
        allowInsecure: false
      }
      registries: [
        {
          server: registryLoginServer
          identity: identityId
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'mcp'
          image: agentImage
          command: [
            'python'
            '-m'
            'chkpmcpaz.remote_server'
          ]
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
          env: [
            {
              name: 'CHKP_PKG'
              value: packageSpec
            }
            {
              name: 'CHKP_ARGS'
              value: args
            }
            {
              name: 'CHKP_SECRET_NAME'
              value: secretName
            }
            {
              name: 'KEY_VAULT_URI'
              value: keyVaultUri
            }
            {
              name: 'CHKP_HTTP_PORT'
              value: string(httpPort)
            }
            {
              name: 'AZURE_CLIENT_ID'
              value: identityClientId
            }
          ]
        }
      ]
      scale: {
        // Scale-to-zero keeps idle cost ~0 -- the tier only bills while a
        // consumer is actually calling tools.
        minReplicas: 0
        maxReplicas: 3
        rules: [
          {
            name: 'http'
            http: {
              metadata: {
                concurrentRequests: '20'
              }
            }
          }
        ]
      }
    }
  }
}

// Easy Auth: require an Entra token for the gateway audience. Unauthenticated
// callers get 401; there is no anonymous path to the tools.
resource auth 'Microsoft.App/containerApps/authConfigs@2024-03-01' = {
  parent: app
  name: 'current'
  properties: {
    platform: {
      enabled: true
    }
    globalValidation: {
      unauthenticatedClientAction: 'Return401'
    }
    identityProviders: {
      azureActiveDirectory: {
        enabled: true
        registration: {
          openIdIssuer: '${environment().authentication.loginEndpoint}${tenantId}/v2.0'
          clientId: audienceClientId
        }
        validation: {
          allowedAudiences: [
            'api://${audienceClientId}'
          ]
        }
      }
    }
  }
}

output fqdn string = app.properties.configuration.ingress.fqdn
output appName string = app.name
