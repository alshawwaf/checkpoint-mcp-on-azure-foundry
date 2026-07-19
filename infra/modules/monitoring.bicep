// monitoring.bicep -- Log Analytics workspace + workspace-linked Application
// Insights (AWS parity: CloudWatch logs/metrics). The hosted agent's protocol
// libraries emit OTel traces to the APPLICATIONINSIGHTS_CONNECTION_STRING that
// Foundry auto-injects into the sandbox; this component is where those traces
// (and any chkpmcpaz telemetry) land. No secrets are stored or logged here.

@description('Log Analytics workspace name (log-<prefix>-<resourceToken>).')
param logAnalyticsName string

@description('Application Insights component name (appi-<prefix>-<resourceToken>).')
param applicationInsightsName string

@description('Azure region for both resources.')
param location string

@description('Tags applied to every resource in the stack.')
param tags object = {}

resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2025-07-01' = {
  name: logAnalyticsName
  location: location
  tags: tags
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
  }
}

resource applicationInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: applicationInsightsName
  location: location
  kind: 'web'
  tags: tags
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logAnalytics.id
  }
}

output logAnalyticsWorkspaceId string = logAnalytics.id
output applicationInsightsName string = applicationInsights.name
output applicationInsightsConnectionString string = applicationInsights.properties.ConnectionString
