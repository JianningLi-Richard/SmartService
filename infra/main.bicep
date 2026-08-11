// Smart Service Request System — core Azure infrastructure.
// Deploy into an already-created resource group:
//   az deployment group create -g rg-smartservice-demo -f infra/main.bicep -p infra/main.parameters.json
//
// Provisions: storage (Table Storage + Functions runtime), Function App (Linux/Python),
// Key Vault, Log Analytics + Application Insights (+ health availability test),
// Azure AI Speech, and a Static Web App for the dashboard.
//
// Secrets (AI Foundry key/endpoint, device keys) are created as empty Key Vault
// placeholders — fill them in via `az keyvault secret set` once the values exist;
// the Function App already reads them through Key Vault references, so no redeploy
// is needed when a value changes.

@description('Short project name used to build resource names.')
param projectName string = 'smartservice'

@description('Environment suffix, e.g. demo, prod.')
param envName string = 'demo'

@description('Azure region for all resources.')
param location string = resourceGroup().location

@description('Default Azure AI Speech voice.')
param speechVoice string = 'en-CA-ClaraNeural'

@description('Static Web Apps is only available in a subset of regions; pick the closest one.')
param staticWebAppLocation string = 'eastus2'

@description('Set by `az keyvault secret set ... device-keys`, not by hand. This secret resource is a full overwrite on every deploy, so deploy.sh re-reads the current value and passes it back -- otherwise every redeploy wipes it.')
@secure()
param deviceKeysValue string = ' '

@description('AI Foundry (Agent Service) has GlobalStandard model quota in eastus2 on this subscription; canadacentral does not.')
param aiFoundryLocation string = 'eastus2'

@description('Chat model deployed for the Routing Agent.')
param aiFoundryModel string = 'gpt-5-mini'
param aiFoundryModelVersion string = '2025-08-07'

@description('Set by create_foundry_agent.py, not by hand. siteConfig.appSettings is a full overwrite on every deploy, so deploy.sh re-reads this from the live app and passes it back -- otherwise every redeploy wipes it.')
param aiFoundryAgentId string = ''

var uniqueSuffix = uniqueString(resourceGroup().id)
var namePrefix = '${projectName}-${envName}'
var storageAccountName = take(toLower(replace('st${projectName}${envName}${uniqueSuffix}', '-', '')), 24)
var keyVaultName = take('kv-${namePrefix}-${uniqueSuffix}', 24)
var functionAppName = '${namePrefix}-func-${uniqueSuffix}'
var speechAccountName = '${namePrefix}-speech-${uniqueSuffix}'
var staticWebAppName = '${namePrefix}-dashboard'
var aiFoundryName = '${namePrefix}-ai-${uniqueSuffix}'
var aiFoundryProjectName = 'smartservice'

// ---------- Storage (Table Storage for requests/sessions + Functions runtime) ----------

resource storage 'Microsoft.Storage/storageAccounts@2023-01-01' = {
  name: storageAccountName
  location: location
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: {
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
  }
}

resource tableService 'Microsoft.Storage/storageAccounts/tableServices@2023-01-01' = {
  parent: storage
  name: 'default'
}

resource requestsTable 'Microsoft.Storage/storageAccounts/tableServices/tables@2023-01-01' = {
  parent: tableService
  name: 'requests'
}

resource sessionsTable 'Microsoft.Storage/storageAccounts/tableServices/tables@2023-01-01' = {
  parent: tableService
  name: 'sessions'
}

// ---------- Observability ----------

resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2022-10-01' = {
  name: '${namePrefix}-logs'
  location: location
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: '${namePrefix}-appi'
  location: location
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logAnalytics.id
  }
}

resource healthAvailabilityTest 'Microsoft.Insights/webtests@2022-06-15' = {
  name: '${namePrefix}-health-test'
  location: location
  tags: {
    'hidden-link:${appInsights.id}': 'Resource'
  }
  properties: {
    SyntheticMonitorId: '${namePrefix}-health-test'
    Name: '${namePrefix}-health-test'
    Enabled: true
    Frequency: 300
    Timeout: 30
    Kind: 'ping'
    RetryEnabled: true
    Locations: [
      { Id: 'us-fl-mia-edge' }
      { Id: 'emea-nl-ams-azr' }
      { Id: 'us-ca-sjc-azr' }
    ]
    Configuration: {
      WebTest: '<WebTest Name="${namePrefix}-health-test" Enabled="True" Timeout="30" xmlns="http://microsoft.com/schemas/VisualStudio/TeamTest/2010"><Items><Request Method="GET" Url="https://${functionAppName}.azurewebsites.net/api/health" ThinkTime="0" Timeout="30" ParseDependentRequests="False" FollowRedirects="True" RecordResult="True" Cache="False" ExpectedHttpStatusCode="200" ExpectedResponseUrl="" IgnoreHttpStatusCode="False" /></Items></WebTest>'
    }
  }
}

// ---------- Key Vault ----------

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: keyVaultName
  location: location
  properties: {
    tenantId: subscription().tenantId
    sku: { family: 'A', name: 'standard' }
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 7
  }
}

resource storageConnStringSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVault
  name: 'storage-connection-string'
  properties: {
    value: 'DefaultEndpointsProtocol=https;AccountName=${storage.name};AccountKey=${storage.listKeys().keys[0].value};EndpointSuffix=${environment().suffixes.storage}'
  }
}

resource speechKeySecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVault
  name: 'speech-key'
  properties: {
    value: speechAccount.listKeys().key1
  }
}

// Placeholders — fill in once the corresponding resource/value exists.
// `az keyvault secret set --vault-name <kv> --name ai-foundry-key --value <value>`
resource aiFoundryKeySecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVault
  name: 'ai-foundry-key'
  properties: {
    value: ' '
  }
}

resource deviceKeysSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVault
  name: 'device-keys'
  properties: {
    value: deviceKeysValue
  }
}

// ---------- Azure AI Speech ----------

resource speechAccount 'Microsoft.CognitiveServices/accounts@2023-05-01' = {
  name: speechAccountName
  location: location
  kind: 'SpeechServices'
  sku: { name: 'F0' }
  properties: {
    customSubDomainName: speechAccountName
  }
}

// ---------- Azure AI Foundry (Routing Agent) ----------
// Unified "AIServices" account + project (replaces the old hub/workspace split).
// The Function App talks to it with its managed identity — no key in app settings.

resource aiFoundry 'Microsoft.CognitiveServices/accounts@2025-06-01' = {
  name: aiFoundryName
  location: aiFoundryLocation
  kind: 'AIServices'
  sku: { name: 'S0' }
  identity: { type: 'SystemAssigned' }
  properties: {
    customSubDomainName: aiFoundryName
    publicNetworkAccess: 'Enabled'
    allowProjectManagement: true
  }
}

resource aiFoundryProject 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' = {
  parent: aiFoundry
  name: aiFoundryProjectName
  location: aiFoundryLocation
  identity: { type: 'SystemAssigned' }
  properties: {}
}

resource aiFoundryModelDeployment 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = {
  parent: aiFoundry
  name: aiFoundryModel
  sku: {
    name: 'GlobalStandard'
    capacity: 10
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: aiFoundryModel
      version: aiFoundryModelVersion
    }
  }
}

// ---------- Function App (Linux, Python) ----------

resource hostingPlan 'Microsoft.Web/serverfarms@2023-01-01' = {
  name: '${namePrefix}-plan'
  location: location
  sku: { name: 'Y1', tier: 'Dynamic' }
  kind: 'linux'
  properties: {
    reserved: true
  }
}

resource functionApp 'Microsoft.Web/sites@2023-01-01' = {
  name: functionAppName
  location: location
  kind: 'functionapp,linux'
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    serverFarmId: hostingPlan.id
    httpsOnly: true
    siteConfig: {
      linuxFxVersion: 'PYTHON|3.12'
      minTlsVersion: '1.2'
      ftpsState: 'Disabled'
      appSettings: [
        { name: 'AzureWebJobsStorage', value: 'DefaultEndpointsProtocol=https;AccountName=${storage.name};AccountKey=${storage.listKeys().keys[0].value};EndpointSuffix=${environment().suffixes.storage}' }
        { name: 'FUNCTIONS_EXTENSION_VERSION', value: '~4' }
        { name: 'FUNCTIONS_WORKER_RUNTIME', value: 'python' }
        { name: 'WEBSITE_RUN_FROM_PACKAGE', value: '1' }
        { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appInsights.properties.ConnectionString }
        { name: 'AZURE_STORAGE_CONNECTION_STRING', value: '@Microsoft.KeyVault(SecretUri=${storageConnStringSecret.properties.secretUri})' }
        { name: 'TABLE_REQUESTS', value: 'requests' }
        { name: 'TABLE_SESSIONS', value: 'sessions' }
        { name: 'AI_FOUNDRY_ENDPOINT', value: 'https://${aiFoundry.name}.services.ai.azure.com/api/projects/${aiFoundryProjectName}' }
        { name: 'AI_FOUNDRY_AGENT_ID', value: aiFoundryAgentId }
        { name: 'AI_FOUNDRY_KEY', value: '@Microsoft.KeyVault(SecretUri=${aiFoundryKeySecret.properties.secretUri})' }
        { name: 'AGENT_TIMEOUT_MS', value: '6000' }
        { name: 'SPEECH_KEY', value: '@Microsoft.KeyVault(SecretUri=${speechKeySecret.properties.secretUri})' }
        { name: 'SPEECH_REGION', value: location }
        { name: 'SPEECH_VOICE', value: speechVoice }
        { name: 'SPEECH_TIMEOUT_MS', value: '2500' }
        { name: 'DEVICE_KEYS', value: '@Microsoft.KeyVault(SecretUri=${deviceKeysSecret.properties.secretUri})' }
        { name: 'SESSION_TTL_SECONDS', value: '60' }
        { name: 'STT_CONFIDENCE_THRESHOLD', value: '0.55' }
        { name: 'MAX_CLARIFICATION_TURNS', value: '2' }
        { name: 'ESCALATION_MINUTES', value: '10' }
      ]
    }
  }
}

resource kvSecretsUserRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(keyVault.id, functionApp.id, 'Key Vault Secrets User')
  scope: keyVault
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '4633458b-17de-408a-b874-0445c86b69e6')
    principalId: functionApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

// Lets the Function App call the Foundry Agent Service using its managed identity
// (DefaultAzureCredential) instead of an API key.
resource aiFoundryDeveloperRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(aiFoundry.id, functionApp.id, 'Azure AI Developer')
  scope: aiFoundry
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '64702f94-c441-49e6-a78b-ef80e0188fee')
    principalId: functionApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

// ---------- Static Web App (dashboard) ----------

resource staticWebApp 'Microsoft.Web/staticSites@2023-01-01' = {
  name: staticWebAppName
  location: staticWebAppLocation
  sku: { name: 'Free', tier: 'Free' }
  properties: {}
}

output functionAppName string = functionApp.name
output functionAppHostname string = functionApp.properties.defaultHostName
output staticWebAppName string = staticWebApp.name
output staticWebAppHostname string = staticWebApp.properties.defaultHostname
output keyVaultName string = keyVault.name
output appInsightsConnectionString string = appInsights.properties.ConnectionString
output storageAccountName string = storage.name
output aiFoundryName string = aiFoundry.name
output aiFoundryEndpoint string = 'https://${aiFoundry.name}.services.ai.azure.com/api/projects/${aiFoundryProjectName}'
output aiFoundryModelDeploymentName string = aiFoundryModelDeployment.name
