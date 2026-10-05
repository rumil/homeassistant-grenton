# Changelog

## 0.4.1

- Fix: gesture event entities now become available again when the CLU is
  back after an outage. Before, they stayed `unavailable` until the next
  gesture, and that gesture did not trigger automations because HA ignores
  the transition from `unavailable`. They now also become `unavailable` when
  the CLU disconnects while HA is running, instead of keeping stale state.
