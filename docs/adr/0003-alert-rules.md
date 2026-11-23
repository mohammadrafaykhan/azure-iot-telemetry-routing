# ADR 0003: Threshold alerts with debounce, hysteresis, and idempotent processing

- **Status:** Accepted
- **Date:** 2026-11-23

## Context

Sensor readings are noisy and Event Hubs delivers at least once. A naive "value above
threshold" check would page on single spikes, flap around the threshold, and repeat alerts
whenever a batch is redelivered.

## Decision

- **Rules per metric with ascending levels.** Vibration uses the ISO 10816-3 zone boundaries
  for medium machines: warning at 4.5 mm/s, critical at 7.1 mm/s. Temperature warns at 80 °C
  and is critical at 88 °C. Rules can be replaced with the `ALERT_RULES` app setting (JSON,
  validated at startup).
- **Hysteresis:** each level has a lower clear threshold (vibration 4.0 and 6.5 mm/s), and a
  level is held until the value falls below it.
- **Debounce:** any change of severity must be seen in two consecutive readings.
- **Transitions, not states, are alerts:** *raised*, *escalated*, *eased*, *cleared*. Only
  *raised* and *escalated* are logged at warning level, which is what notifies the operator.
- **Idempotency:**
  - rule state remembers the time of the last reading; anything at or before it is a replay
    and is ignored;
  - alert rows are keyed by the reading's time, device, and metric, so a repeated write
    overwrites itself;
  - alerts are written **before** the rule state, so a failure between the two can repeat an
    alert but never lose one;
  - the `stream` function has an exponential retry policy, and the whole batch is safe to
    process again.
- **State per device** lives in Table storage (`alertstate`). IoT Hub assigns each device to
  one Event Hubs partition, and one function instance owns a partition at a time, so a
  device's rule state is never updated concurrently.

## Consequences

- A single spike, or a value hovering at a boundary, produces no alert.
- Clearing takes two readings below the clear threshold. At the 60 s default that is two
  minutes; the operator can shorten the interval for a device being watched.
- Alert history is partitioned by UTC day and ordered newest first.
