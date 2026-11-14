# Agent contract

This is the reference for the Retell agent that sits in front of the backend. It covers the prompt, the two custom functions, and the webhook contract. It is deliberately a contract, not a dashboard walkthrough: the interesting part is the backend's behaviour under real traffic.

## System prompt

The agent is Maya, the receptionist for Brightside Dental. The prompt below is the version used for the demo.

```text
## Role
You are Maya, the virtual receptionist for Brightside Dental. You answer calls and book appointments. You are not a dentist and you never give medical advice.

## Context
The clinic is in the {{system_timezone}} time zone. Right now it is {{current_time}}.
Calendar for the next two weeks:
{{current_calendar}}

## Style
Warm and brief. One or two short sentences per turn. Ask one question at a time. Never read out ids or technical terms.

## Booking flow
1. Ask why they are calling and for the patient's full name. Repeat the name back to confirm it.
2. Ask which day suits them. Use the calendar above to turn words like "next Tuesday" into a date, then call check_availability with that date as YYYY-MM-DD.
3. Offer at most three of the returned times. Never offer a time the tool did not return.
4. When the caller picks a time, call book_appointment with the exact slot_id that matches it, the patient's name and the reason for the visit.
5. Only say the appointment is booked if the tool returns booked as true. Then confirm the name, day and time.

## Rules
- If a tool fails, returns an error, or returns booked as false, apologise once and offer another time or a callback. Do not retry more than once.
- The clinic is open Monday to Friday, 9 AM to 5 PM. If the caller asks for a weekend, offer the nearest weekday.
- If the caller describes severe pain, swelling or bleeding, tell them to contact emergency services or the nearest emergency room, and offer a callback.
- If you do not know something, say so and offer a callback. Do not guess.
```

## Custom functions

Both functions are `POST` endpoints and require a valid `X-Retell-Signature`. Keep **Payload: args only** off: the handler reads `body["call"]["call_id"]` and `body["args"]`. Turn it on and the call ID disappears, which shows up as `call_id=unknown` in the logs.

### `check_availability`

Request:

```json
{
  "call": {"call_id": "call_..."},
  "args": {"date": "2030-01-07"}
}
```

Success:

```json
{
  "available": ["Monday January 7 at 9:00 AM"],
  "slot_ids": ["2030-01-07T09:00:00-08:00"],
  "message": "Openings found."
}
```

`date` must be `YYYY-MM-DD`. A weekend, a past date, or an invalid date returns `available: []` with a message the agent can act on. The handler never returns a bare 500 to a live call.

### `book_appointment`

Request:

```json
{
  "call": {"call_id": "call_..."},
  "args": {
    "slot_id": "2030-01-07T09:00:00-08:00",
    "patient_name": "Sam Njogu",
    "reason": "cleaning"
  }
}
```

Success:

```json
{"booked": true, "message": "Booked Sam Njogu for Monday January 7 at 9:00 AM."}
```

Every rejection returns `booked: false` and a message that tells the agent what to do next:

| Rejection | Message |
|---|---|
| Missing patient name | Ask for the patient's full name |
| `slot_id` not on the generated grid | Call `check_availability` again and use an exact `slot_id` |
| Slot in the past | Offer another time |
| Slot already booked | Offer another time |
| Handler exceeded its deadline | Offer a callback |
| Upstream error | Apologise and offer a callback |

The `slot_id` must match a generated slot exactly, including the clinic time zone offset. Free text such as `"tomorrow 3pm"` is rejected, so a model mistake cannot create an off-grid appointment.

## Webhooks

`POST /webhooks/retell` handles `call_started`, `call_ended`, and `call_analyzed`.

- The signature is checked against the raw request body. Re-serialising parsed JSON changes the bytes and breaks the hash.
- Events are persisted before the request is acknowledged with `204`.
- `(event, call_id)` is unique, so duplicate deliveries are ignored.
- `call_analyzed` arrives after `call_ended` and updates the same call record with the summary and sentiment.
- Unhandled event types are acknowledged with `204` and ignored.

## Timeouts and retries

Retell's function timeout is in **seconds**. Use `5` seconds for both functions.

`FUNCTION_DEADLINE_S` must be lower than the platform timeout, for example `4`. A booking that takes longer is refused instead of being committed after the caller was told it failed. The handler still waits for the slow work to finish, so the logged latency remains around `12000ms`; what changes is that the late write is rejected.

Set **Max retries** to `0` on `book_appointment`. The unique constraint on `slot_iso` prevents a double booking, but a retry after a committed write still produces a confusing `booked: false` result and an extra log row.
