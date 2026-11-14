# Post-incident summary: booking committed after the caller was told it failed

This incident was reproduced locally. A test client that gives up after one second stands in for Retell's function timeout. No live call data is used here, and the section on real calls is left for a run against a live agent.

## Impact

When the booking lookup is slower than the function timeout, the caller hears that scheduling failed while the appointment is still written. The slot is then unavailable to other patients and nobody knows it is booked.

## Summary

Retell waits for a custom function only up to its configured timeout. After that it returns an error to the agent, which tells the caller to try again. Our handler had no idea the request had been abandoned, so it finished the work and committed the appointment.

## Timeline of the reproduction

1. Server started with `FAIL_MODE=slow` and `SLOW_SECONDS=3`.
2. A signed `book_appointment` request was sent with a client timeout of 1 second.
3. The client gave up after 1 second, which is what the agent would experience.
4. The handler kept running and finished after about 3 seconds.

## Evidence

| Setting | Appointments table | Logged function call |
|---|---|---|
| `FUNCTION_DEADLINE_S=0` (no check) | 1 row for the abandoned request | `ok=true`, `latency_ms=3003` |
| `FUNCTION_DEADLINE_S=2` | empty | `ok=false`, `latency_ms=2999`, error `deadline of 2s exceeded, not committed` |

The first row is the problem: our own log says the call succeeded, while the caller was told it failed. When logs on both sides disagree, compare the timestamps before assuming either side is wrong.

## Root cause

The handler committed after an unbounded delay. Nothing connected the platform's timeout to the server's work.

## Resolution

`book_appointment` now measures its own elapsed time and refuses to commit once it exceeds `FUNCTION_DEADLINE_S`, returning a message that tells the agent to offer a callback. The deadline should be set a little below the timeout configured on the function in Retell.

## Prevention

- Keep function timeouts short and set the deadline just below them.
- Alert when `latency_ms` in `/functions` passes a few seconds.
- Make writes safe to repeat, so a retry or a late duplicate cannot double book. The unique constraint on `slot_iso` already protects against double booking.
- Test slow dependencies deliberately. `tests/test_api.py` covers both the enforced and the unenforced case.

## Related finding

An earlier version used a blocking sleep inside an async handler, so one slow function froze every other request including webhooks. The sleep is now non-blocking and covered by a regression test.

## Real calls

Add the call ID, the Retell log excerpt and a screenshot from a live agent here once the drill has been run against Retell.
