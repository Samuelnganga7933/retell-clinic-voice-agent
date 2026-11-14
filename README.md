# Retell dental-clinic receptionist: backend

A voice agent built on Retell that books dental appointments, with a FastAPI backend providing
custom functions, a signature-verified webhook receiver, call logging and a failure-injection switch
for incident practice.

```
Caller -> Retell agent --(custom function POST)--> /functions/*   -> SQLite (slots, appointments)
                       --(call_* webhooks)------> /webhooks/retell -> SQLite (calls, transcripts)
```

## Run it
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # paste your Retell API key (the one with the webhook badge)
pytest -q                   # local tests, no Retell needed; each test uses a throwaway DB
uvicorn main:app --reload --port 8000
ngrok http 8000             # copy the https URL
```

## Retell dashboard setup
1. **Webhook:** Account (or agent) webhook URL = `https://<ngrok>/webhooks/retell`
2. **Custom function 1** `check_availability`: POST `https://<ngrok>/functions/check_availability`,
   parameters: `date` (string, YYYY-MM-DD, required)
3. **Custom function 2** `book_appointment`: POST `https://<ngrok>/functions/book_appointment`,
   parameters: `slot_id` (string, required, exact value from check_availability), `patient_name` (string, required), `reason` (string)
4. Set function timeout to ~5s and "speak during execution" on, with a short filler line.

## Agent prompt (starter)
```
You are Maya, the receptionist for Brightside Dental. Today is {{current_time}}.
Goals: understand why the caller is calling, then book an appointment.
Rules:
- Ask for the patient's name and reason for visit.
- Ask for the day they want. Convert it to YYYY-MM-DD and call check_availability.
- Offer at most 3 times from the result. Never invent times.
- When they choose, call book_appointment with the exact slot_id for that time.
- If a tool returns an error or booked=false, apologise once, and offer another time or a callback.
- Keep replies to one or two short sentences. Confirm name, day and time before ending.
```

## Viewing logs
- `GET /calls`, `GET /calls/{call_id}` (transcript + every function call with latency and error), `GET /functions`
- These contain transcripts and patient names, so they require `Authorization: Bearer $ADMIN_TOKEN` (set `ADMIN_TOKEN` in `.env`; unset means 503). Example: `curl -H "Authorization: Bearer $ADMIN_TOKEN" https://<ngrok>/calls`
- `book_appointment` rejects any `slot_id` that isn't an exact slot from `check_availability` (wrong format, off-grid, weekend, or in the past) and logs it as a failed call with the reason.

## Incident drills (`FAIL_MODE` in .env, restart the server after changing)
| Mode | Symptom on the call | Where to look |
|---|---|---|
| `slow` | Long silence, agent apologises or tool call fails. The handler still finishes later, so a booking can land after the caller was told it failed (ghost booking) | `latency_ms` in `/functions` vs the call's timeline in Retell, `appointments` table |
| `error` | Agent says scheduling is unavailable | `error` column, server traceback |
| `tz_bug` | Agent offers times that are wrong for the clinic | `slot_ids` offset vs `CLINIC_TZ`, transcript |
