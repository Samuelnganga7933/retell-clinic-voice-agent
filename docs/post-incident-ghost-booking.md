# Post-incident: booking committed after the caller was told it failed

## Impact

When the booking handler is slower than the function timeout, the caller hears that scheduling failed while the appointment is still written. The slot is then unavailable to other patients and nobody knows it is booked.

## Summary

Retell waits for a custom function only up to its configured timeout. After that it returns an error to the agent, which tells the caller to try again. The handler had no idea the request had been abandoned, so it finished the work and committed the appointment.

The platform log says the tool failed. The server log says `ok=true`. Both are correct; they describe different moments in the same request.

## Reproduction

The behaviour is covered by `tests/test_api.py` and was reproduced locally with a client that gives up after one second. The client timeout stands in for Retell's five-second function timeout.

Configuration for the local reproduction:

```text
FAIL_MODE=slow
FAIL_TARGET=book_appointment
SLOW_SECONDS=3
FUNCTION_DEADLINE_S=0
```

Timeline:

1. The server starts with `FAIL_MODE=slow` and `SLOW_SECONDS=3`.
2. A signed `book_appointment` request is sent with a client timeout of one second.
3. The client gives up after one second, which is what the agent experiences.
4. The handler keeps running and finishes after about three seconds.

Evidence from the two configurations:

| Setting | `appointments` table | Logged function call |
|---|---|---|
| `FUNCTION_DEADLINE_S=0` (no check) | 1 row for the abandoned request | `ok=true`, `latency_ms=3003` |
| `FUNCTION_DEADLINE_S=2` | empty | `ok=false`, `latency_ms=2999`, error `deadline of 2s exceeded, not committed` |

The first row is the problem: the server log says the call succeeded while the caller was told it failed. When logs on both sides disagree, compare the timestamps before assuming either side is wrong.

## Live evidence

Two browser test calls against the running agent, with the Retell function timeout set to five seconds:

| | Before | After |
|---|---|---|
| Configuration | `SLOW_SECONDS=12`, `FUNCTION_DEADLINE_S=0` | `SLOW_SECONDS=12`, `FUNCTION_DEADLINE_S=4` |
| Call ID | `call_ed63c3649180d22e9650e8c8761` | `call_5634cdd5ce5d40cb5bdb7e588e6` |
| `check_availability` | `ok=1`, `latency_ms=6` | `ok=1`, `latency_ms=2` |
| `book_appointment` | `ok=1`, `latency_ms=12006` | `ok=0`, `latency_ms=12001` |
| Error | none | `deadline of 4s exceeded, not committed` |
| `appointments` | 1 row: Test Patient, Nov 17 at 1:00 PM | no row |
| Agent told the caller | booking failed | booking failed |
| Webhook events | `call_started`, `call_ended`, `call_analyzed` stored | same |

Both calls sound the same: Retell stops waiting after five seconds, the agent apologises and offers a callback. The difference is in the database. The before run committed a booking the caller was told did not exist. The after run refused it.

The after run still shows `latency_ms=12001`, because the handler waits for the slow work before deciding. The fix refuses the write; it does not cancel the request.

### Reproducing it

Set the before configuration and restart the server:

```text
FAIL_MODE=slow
FAIL_TARGET=book_appointment
SLOW_SECONDS=12
FUNCTION_DEADLINE_S=0
```

Call, ask for a slot, and pick it. About twelve seconds after the request started, the server commits the booking. Then set `FUNCTION_DEADLINE_S=4`, restart, and repeat on a different slot. Use a different slot for the second run, because the first run's ghost booking occupies its slot.

```bash
sqlite3 clinic.db "select call_id, patient, slot_iso, datetime(created_at,'unixepoch') from appointments"
sqlite3 clinic.db "select call_id, name, ok, latency_ms, error from function_calls order by id desc limit 4"
```

## Root cause

The handler committed after an unbounded delay. Nothing connected the platform's timeout to the server's work.

## Resolution

`book_appointment` measures its own elapsed time and refuses to commit once it exceeds `FUNCTION_DEADLINE_S`, returning a message that tells the agent to offer a callback.

The fix refuses to commit a late booking. It does not cancel the request: the caller has already been told the tool failed, and the remaining work has no safe side effect.

## Prevention

- Keep function timeouts short and set `FUNCTION_DEADLINE_S` just below them.
- Alert when `latency_ms` in `/functions` passes a few seconds.
- Make writes safe to repeat, so a retry or a late duplicate cannot double book. The unique constraint on `slot_iso` protects against double booking; the deadline protects against a write after the caller was told it failed.
- Test slow dependencies deliberately. `tests/test_api.py` covers both the enforced and the unenforced case.

## Related finding

An earlier version used a blocking sleep inside an async handler, so one slow function froze every other request including webhooks. The sleep is now non-blocking and covered by a regression test.
