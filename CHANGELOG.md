# Changelog

## 0.5.0

Reliability of the connection to the CLU, compared against the vclu project
(github.com/Domktorymysli/vclu) and its measurements on real CLUs.

- New: CLU restart detection. A restart is detected from the CLU's uptime,
  even when it is too quick for the connectivity sensor. The CLU's entities
  pass through `unavailable` and come back with its state after the
  restart; then the new diagnostic sensor `sensor.grenton_clu_last_boot`
  changes and the event `grenton_clu_restarted` is fired, for automations
  that restore state. See "CLU restart signal" in the README.
- Changed: a command the CLU never confirms now fails its service call with
  an error, so the UI and automations see it. Before, it only logged a
  warning. An automation run stops at such an error unless the action has
  `continue_on_error: true`.
- Changed: an action attempt waits 2 s for the CLU's answer (was 5 s), so
  3 attempts take at most 6 s.
- Fix: a retried command could undo a newer one ("on" whose answer was lost,
  then "off", left the CLU on). Retries now never overtake a newer command.
- Fix: the subscription is split into sessions of at most 28 entries that
  fit in one packet. A single large subscription could exceed what a CLU
  accepts, and the CLU then never answered: entities stayed unavailable.
- Fix: sessions are renewed every 30 s (was 45 s), and a failed renewal is
  retried after 3 s. One lost renewal could leave the CLU without a session
  for up to about 30 s, losing wall-button presses.
- Fix: a report pushed right after a register answer could be overwritten by
  the older answer, and the same gesture could then fire twice.
- Fix: a string containing a comma, or a table, in a report shifted every
  later value onto the wrong entity.
- Fix: a nil value read at registration showed as the text "None" (a switch
  showed off instead of unknown).
- Fix: a CLU in emergency mode counted as connected, and a CLU that stopped
  accepting its subscription kept showing old values as current. Both now
  make its entities unavailable.
- Fix: an answer arriving in the same event-loop pass as its timeout was
  discarded.
- Fix: each ping has its own message ID, only datagrams from the CLU are
  processed, a message that is not valid UTF-8 is no longer dropped whole,
  and sessions are ended with clientDestroy when the integration unloads.

## 0.4.1

- Fix: gesture event entities now become available again when the CLU is
  back after an outage. Before, they stayed `unavailable` until the next
  gesture, and that gesture did not trigger automations because HA ignores
  the transition from `unavailable`. They now also become `unavailable` when
  the CLU disconnects while HA is running, instead of keeping stale state.
