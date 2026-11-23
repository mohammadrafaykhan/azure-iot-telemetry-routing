// Device connection-state events from IoT Hub to the connectivity function. Deployed after the
// function code, because Event Grid validates the function endpoint when the subscription is made.

param location string = resourceGroup().location
param hubName string
param functionAppName string

resource hub 'Microsoft.Devices/IotHubs@2023-06-30' existing = {
  name: hubName
}

resource functionApp 'Microsoft.Web/sites@2024-04-01' existing = {
  name: functionAppName
}

resource topic 'Microsoft.EventGrid/systemTopics@2022-06-15' = {
  name: 'evgt-${hubName}'
  location: location
  properties: {
    source: hub.id
    topicType: 'Microsoft.Devices.IoTHubs'
  }
}

resource connectivity 'Microsoft.EventGrid/systemTopics/eventSubscriptions@2022-06-15' = {
  parent: topic
  name: 'device-connectivity'
  properties: {
    destination: {
      endpointType: 'AzureFunction'
      properties: {
        resourceId: '${functionApp.id}/functions/connectivity'
        maxEventsPerBatch: 1
        preferredBatchSizeInKilobytes: 64
      }
    }
    filter: {
      includedEventTypes: [
        'Microsoft.Devices.DeviceConnected'
        'Microsoft.Devices.DeviceDisconnected'
      ]
    }
    eventDeliverySchema: 'EventGridSchema'
    retryPolicy: {
      maxDeliveryAttempts: 30
      eventTimeToLiveInMinutes: 1440
    }
  }
}
