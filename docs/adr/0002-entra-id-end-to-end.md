# ADR 0002: Microsoft Entra ID end to end, on Flex Consumption

- **Status:** Accepted
- **Date:** 2026-11-23

## Context

The built-in endpoint used in the first project of this portfolio can only be read with a
shared access key. Every hop of this pipeline should instead authenticate with an identity
that can be scoped and revoked through Azure RBAC.

## Decision

- **Local authentication is disabled** on Event Hubs, the storage account (shared key off),
  Application Insights, and Log Analytics. Keys exist nowhere in configuration.
- **The IoT hub** sends to Event Hubs as its system-assigned identity.
- **The function app** runs on the **Flex Consumption** plan with a system-assigned identity
  that holds only:
  - *Azure Event Hubs Data Receiver* on the event hub (`EventHubConnection__fullyQualifiedNamespace`);
  - *Storage Blob Data Owner* and *Storage Table Data Contributor* on its storage account
    (host storage, deployment package, archive, and tables);
  - *Monitoring Metrics Publisher* on Application Insights (`APPLICATIONINSIGHTS_AUTHENTICATION_STRING=Authorization=AAD`).
- **The operator** uses `az login` with *IoT Hub Data Contributor* on the hub and read-only
  *Storage Table Data Reader* and *Storage Blob Data Reader* on the storage account.
- **Event Grid** delivers connection events with the `AzureFunction` endpoint type, which
  resolves the function by resource ID; no function key is handled by the deployment.
- **Code is deployed as a zip with a remote build**, packaged with forward-slash paths. The
  Event Grid subscription is created only after the functions have indexed, because Event
  Grid validates the endpoint when the subscription is made.

## Consequences

- Granting or removing access is a role assignment; there is nothing to rotate.
- Flex Consumption bills per execution with a monthly free grant, so an idle pipeline costs
  close to nothing.
- Clients are created on first use rather than at import, so a missing setting fails an
  invocation visibly instead of stopping the host from indexing the functions.
