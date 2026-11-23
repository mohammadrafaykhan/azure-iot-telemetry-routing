// Message routing for the hub, applied after its managed identity holds Azure Event Hubs Data
// Sender. A free hub allows one custom endpoint and five routing queries; both routes share the
// endpoint, and anything matching no route falls back to the built-in endpoint.

param hubName string
param location string
param baseProperties object
param eventHubNamespace string
param eventHubName string

var endpointName = 'telemetry-stream'

// Kept as plain values: a quoted literal inside the union() expression below fails ARM validation.
var telemetryCondition = '$body.schemaVersion = \'1.0\''
var twinReportedCondition = 'IS_OBJECT($body.properties.reported)'

resource hub 'Microsoft.Devices/IotHubs@2023-06-30' = {
  name: hubName
  location: location
  sku: { name: 'F1', capacity: 1 }
  identity: { type: 'SystemAssigned' }
  properties: union(baseProperties, {
    routing: {
      endpoints: {
        eventHubs: [
          {
            name: endpointName
            authenticationType: 'identityBased'
            endpointUri: 'sb://${eventHubNamespace}.servicebus.windows.net'
            entityPath: eventHubName
            subscriptionId: subscription().subscriptionId
            resourceGroup: resourceGroup().name
          }
        ]
        serviceBusQueues: []
        serviceBusTopics: []
        storageContainers: []
      }
      routes: [
        {
          // Telemetry that follows the schema (JSON body, utf-8); anything else falls back.
          name: 'telemetry'
          source: 'DeviceMessages'
          condition: telemetryCondition
          endpointNames: [endpointName]
          isEnabled: true
        }
        {
          // Reported-property changes only; desired-only changes are the operator's own writes.
          name: 'twin-reported'
          source: 'TwinChangeEvents'
          condition: twinReportedCondition
          endpointNames: [endpointName]
          isEnabled: true
        }
      ]
      fallbackRoute: {
        name: '$fallback'
        source: 'DeviceMessages'
        condition: 'true'
        endpointNames: ['events']
        isEnabled: true
      }
      enrichments: [
        { key: 'site', value: '$twin.tags.site', endpointNames: [endpointName] }
        { key: 'line', value: '$twin.tags.line', endpointNames: [endpointName] }
      ]
    }
  })
}
