# ADR 0004: Device presence from connection events and last-seen telemetry

- **Status:** Accepted
- **Date:** 2026-11-23

## Context

IoT Hub publishes `DeviceConnected` and `DeviceDisconnected` through Event Grid, but Event
Grid does not guarantee order, state changes can be reported up to a minute late, and
changes within one minute can be missed. A device can also stay connected while sending
nothing. Microsoft recommends combining lifecycle events with a last-seen time.

## Decision

- **Connection state** comes from the events, applied only if the event's `sequenceNumber`
  (a hexadecimal string) is greater than the one already stored, so a late, older event
  cannot undo a newer one. Module events are ignored.
- **Last seen** is the enqueued time of the latest routed telemetry; the **expected interval**
  is the `telemetryIntervalMs` the device reports through the twin-reported route.
- **A timer every minute raises:**
  - **offline** (critical) when a device has been disconnected for `OFFLINE_AFTER_SECONDS`
    (default 120), long enough to absorb the reporting delay and brief network drops;
  - **silent** (warning) when a device is connected but nothing has arrived for three
    intervals, with a two-minute floor.
- Each alert is raised once and **resolved** by the opposite signal: reconnection for offline,
  new telemetry for silent.
- **Concurrency:** three functions write the same presence row, so every write uses the row's
  ETag. A conflict re-reads and re-applies the change, up to four times; the timer simply
  re-evaluates on its next run.

## Consequences

- A device that drops for under two minutes raises nothing; one that stays away is reported
  within about three minutes, and one that hangs while connected is still caught.
- Presence works for any device that sends telemetry and reports its interval, with no
  heartbeat message of its own.
