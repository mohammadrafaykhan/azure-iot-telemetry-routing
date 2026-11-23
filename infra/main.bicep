// Telemetry routing and alerting: IoT Hub (F1) routes telemetry and twin changes to Event Hubs
// with its managed identity; a Flex Consumption function app archives, evaluates alert rules,
// and tracks device presence. Every data-plane connection uses Microsoft Entra ID.

@description('Azure region for all resources.')
param location string = resourceGroup().location

@description('Object ID of the operator who runs opsctl (az ad signed-in-user show --query id -o tsv).')
param operatorPrincipalId string

@description('Email address for alert notifications. Empty skips the Azure Monitor alert rule.')
param alertEmail string = ''

@description('Seconds a device may stay disconnected before an offline alert is raised.')
@minValue(30)
@maxValue(86400)
param offlineAfterSeconds int = 120

var suffix = uniqueString(resourceGroup().id)
var hubName = 'iot-telemetry-${suffix}'
var namespaceName = 'evhns-telemetry-${suffix}'
var eventHubName = 'telemetry'
var storageName = 'sttelem${suffix}'
var functionAppName = 'func-telemetry-${suffix}'
var tableNames = ['alertstate', 'alerts', 'presence']

var roles = {
  eventHubsDataSender: '2b629674-e913-4c01-ae53-ef4638d8f975'
  eventHubsDataReceiver: 'a638d3c7-ab3a-418d-83e6-5f17a39d4fde'
  storageBlobDataOwner: 'b7e6dc6d-f1e8-4753-8033-0f276bb0955b'
  storageBlobDataReader: '2a2b9908-6ea1-4ae2-8e65-a410df84e7d1'
  storageTableDataContributor: '0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3'
  storageTableDataReader: '76199698-9eea-4c19-bc75-cec21354c6b6'
  monitoringMetricsPublisher: '3913510d-42f4-4e42-8a64-420c390055eb'
  iotHubDataContributor: '4fc6c259-987e-4a07-842e-c321cc9d413f'
}

// ---------------------------------------------------------------- observability

resource workspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'log-telemetry-${suffix}'
  location: location
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
    features: { disableLocalAuth: true }
  }
}

resource insights 'Microsoft.Insights/components@2020-02-02' = {
  name: 'appi-telemetry-${suffix}'
  location: location
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: workspace.id
    DisableLocalAuth: true
  }
}

// ---------------------------------------------------------------- storage

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageName
  location: location
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: {
    accessTier: 'Hot'
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    defaultToOAuthAuthentication: true
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storage
  name: 'default'
}

resource deploymentContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: 'deployments'
}

resource archiveContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: 'archive'
}

resource tableService 'Microsoft.Storage/storageAccounts/tableServices@2023-05-01' = {
  parent: storage
  name: 'default'
}

resource tables 'Microsoft.Storage/storageAccounts/tableServices/tables@2023-05-01' = [
  for name in tableNames: {
    parent: tableService
    name: name
  }
]

// ---------------------------------------------------------------- event hub

resource namespace 'Microsoft.EventHub/namespaces@2024-01-01' = {
  name: namespaceName
  location: location
  sku: { name: 'Basic', tier: 'Basic', capacity: 1 }
  properties: {
    disableLocalAuth: true
    minimumTlsVersion: '1.2'
  }
}

resource eventHub 'Microsoft.EventHub/namespaces/eventhubs@2024-01-01' = {
  parent: namespace
  name: eventHubName
  properties: {
    partitionCount: 2
    messageRetentionInDays: 1
  }
}

// ---------------------------------------------------------------- IoT hub

var hubProperties = {
  disableDeviceSAS: true
  disableModuleSAS: true
  eventHubEndpoints: {
    events: { retentionTimeInDays: 1, partitionCount: 2 }
  }
  features: 'None'
}

resource hub 'Microsoft.Devices/IotHubs@2023-06-30' = {
  name: hubName
  location: location
  sku: { name: 'F1', capacity: 1 }
  identity: { type: 'SystemAssigned' }
  properties: hubProperties
}

resource hubSendsToEventHub 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(eventHub.id, hub.id, roles.eventHubsDataSender)
  scope: eventHub
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.eventHubsDataSender)
    principalId: hub.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

// The identity-based endpoint can only be added once the hub's identity holds its role.
module routing 'hub-routing.bicep' = {
  name: 'hub-routing'
  params: {
    hubName: hub.name
    location: location
    baseProperties: hubProperties
    eventHubNamespace: namespace.name
    eventHubName: eventHub.name
  }
  dependsOn: [hubSendsToEventHub]
}

// ---------------------------------------------------------------- function app

resource plan 'Microsoft.Web/serverfarms@2024-04-01' = {
  name: 'plan-telemetry-${suffix}'
  location: location
  kind: 'functionapp'
  sku: { tier: 'FlexConsumption', name: 'FC1' }
  properties: { reserved: true }
}

resource functionApp 'Microsoft.Web/sites@2024-04-01' = {
  name: functionAppName
  location: location
  kind: 'functionapp,linux'
  identity: { type: 'SystemAssigned' }
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    siteConfig: {
      minTlsVersion: '1.2'
      ftpsState: 'Disabled'
      appSettings: [
        { name: 'AzureWebJobsStorage__accountName', value: storage.name }
        { name: 'AzureWebJobsStorage__credential', value: 'managedidentity' }
        { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: insights.properties.ConnectionString }
        { name: 'APPLICATIONINSIGHTS_AUTHENTICATION_STRING', value: 'Authorization=AAD' }
        { name: 'EventHubConnection__fullyQualifiedNamespace', value: '${namespace.name}.servicebus.windows.net' }
        { name: 'EventHubConnection__credential', value: 'managedidentity' }
        { name: 'EVENTHUB_NAME', value: eventHub.name }
        { name: 'STORAGE_ACCOUNT_NAME', value: storage.name }
        { name: 'OFFLINE_AFTER_SECONDS', value: string(offlineAfterSeconds) }
      ]
    }
    functionAppConfig: {
      deployment: {
        storage: {
          type: 'blobContainer'
          value: '${storage.properties.primaryEndpoints.blob}${deploymentContainer.name}'
          authentication: { type: 'SystemAssignedIdentity' }
        }
      }
      scaleAndConcurrency: {
        maximumInstanceCount: 40
        instanceMemoryMB: 2048
      }
      runtime: { name: 'python', version: '3.12' }
    }
  }
}

var functionRoles = [
  { scope: 'storage', role: roles.storageBlobDataOwner }
  { scope: 'storage', role: roles.storageTableDataContributor }
]

resource functionStorageRoles 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for assignment in functionRoles: {
    name: guid(storage.id, functionApp.id, assignment.role)
    scope: storage
    properties: {
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', assignment.role)
      principalId: functionApp.identity.principalId
      principalType: 'ServicePrincipal'
    }
  }
]

resource functionReadsEventHub 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(eventHub.id, functionApp.id, roles.eventHubsDataReceiver)
  scope: eventHub
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.eventHubsDataReceiver)
    principalId: functionApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource functionPublishesTelemetry 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(insights.id, functionApp.id, roles.monitoringMetricsPublisher)
  scope: insights
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.monitoringMetricsPublisher)
    principalId: functionApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

// ---------------------------------------------------------------- operator (opsctl)

resource operatorHub 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(hub.id, operatorPrincipalId, roles.iotHubDataContributor)
  scope: hub
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.iotHubDataContributor)
    principalId: operatorPrincipalId
    principalType: 'User'
  }
}

resource operatorStorageRoles 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for role in [roles.storageTableDataReader, roles.storageBlobDataReader]: {
    name: guid(storage.id, operatorPrincipalId, role)
    scope: storage
    properties: {
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', role)
      principalId: operatorPrincipalId
      principalType: 'User'
    }
  }
]

// ---------------------------------------------------------------- notifications

resource actionGroup 'Microsoft.Insights/actionGroups@2023-01-01' = if (!empty(alertEmail)) {
  name: 'ag-telemetry-${suffix}'
  location: 'global'
  properties: {
    groupShortName: 'telemetry'
    enabled: true
    emailReceivers: [
      { name: 'operator', emailAddress: alertEmail, useCommonAlertSchema: true }
    ]
  }
}

resource alertRule 'Microsoft.Insights/scheduledQueryRules@2023-12-01' = if (!empty(alertEmail)) {
  name: 'alert-telemetry-${suffix}'
  location: location
  properties: {
    displayName: 'IoT telemetry alert raised'
    description: 'A device crossed an alert threshold, went offline, or went silent.'
    severity: 2
    enabled: true
    scopes: [insights.id]
    evaluationFrequency: 'PT5M'
    windowSize: 'PT5M'
    criteria: {
      allOf: [
        {
          query: 'traces | where message startswith "ALERT " and severityLevel >= 2 | project timestamp, message'
          timeAggregation: 'Count'
          operator: 'GreaterThan'
          threshold: 0
          failingPeriods: { numberOfEvaluationPeriods: 1, minFailingPeriodsToAlert: 1 }
        }
      ]
    }
    autoMitigate: false
    actions: { actionGroups: [actionGroup.id] }
  }
}

output iotHubName string = hub.name
output iotHubHostName string = hub.properties.hostName
output functionAppName string = functionApp.name
output storageAccountName string = storage.name
output eventHubNamespace string = namespace.name
output appInsightsName string = insights.name
