# ADR 0001: Message routing on a free IoT hub

- **Status:** Accepted
- **Date:** 2026-11-23

## Context

The pipeline needs telemetry delivered to a processor, a raw archive, and the devices'
reported settings. A free (F1) hub has every Standard feature but allows **one custom
endpoint** and five routing queries, and it caps the day at 8,000 messages of 0.5 KB.

## Decision

- **One custom endpoint, Event Hubs**, authenticated with the hub's system-assigned managed
  identity (*Azure Event Hubs Data Sender* on the event hub only).
- **Two routes into it:**
  - `telemetry`: device messages where `$body.schemaVersion = '1.0'`. Querying the body
    works because devices send `application/json` in UTF-8.
  - `twin-reported`: twin change notifications where `IS_OBJECT($body.properties.reported)`.
    These carry the interval each device reports, so presence checks know how often to
    expect telemetry. Desired-only changes are the operator's own writes and are left out.
- **Enrichments** stamp `site` and `line` from the device twin's tags on every routed message,
  so consumers get context without a registry lookup.
- **Fallback route on**: anything that matches no route goes to the built-in endpoint instead
  of being dropped.
- **The archive is written by the function**, not by a Storage route: one JSON Lines blob per
  Event Hubs batch, partitioned by hour, named from the batch's positions so redelivery
  overwrites rather than duplicates.
- **Default interval 60 s.** Five presses then send 7,200 messages a day, inside the free
  quota. A previous project in this portfolio exhausted the quota at a 5 s default.

## Consequences

- Routing to Event Hubs keeps per-device order within a partition, which the alert rules use.
- Event Hubs Basic allows one consumer group, so the function app is the only reader of the
  stream; operators read results from Storage instead.
- The hub's identity needs its role before the endpoint can be created, so the routing is
  applied in a second Bicep module after the role assignment, and `deploy.ps1` retries once
  for role propagation.
