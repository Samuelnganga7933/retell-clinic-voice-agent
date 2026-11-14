# Retell setup

Everything below happens in the Retell dashboard and needs no phone number. New accounts include trial credit, and browser web calls on a draft agent do not need a payment method.

## 1. API key

Open the API keys page and copy the key that shows the **webhook** badge. Only that key can verify webhook signatures. Put it in `.env` as `RETELL_API_KEY`.

## 2. Run the server and the tunnel

```bash
uvicorn main:app --port 8000
ngrok http 8000
```

Open `https://<ngrok-address>/health` in a browser. It should return `{"ok":true}`. Use that same address in every URL below.

## 3. Create the agent

1. Agents tab, **Create an agent**, then **Single prompt**.
2. Choose **Build from scratch**.
3. Paste the prompt below into the prompt field.
4. Set the opening message to: `Thank you for calling Brightside Dental, this is Maya. How can I help you today?`

### Prompt

```
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

## 4. Add the two custom functions

In the agent editor open **Functions**, choose **Add**, then **Custom Function**. Leave **Payload: args only** off, because the server reads `name`, `args` and `call`.

### check_availability

- Description: `Look up open appointment times for a date. Always call this before offering any time.`
- Method: `POST`
- URL: `https://<ngrok-address>/functions/check_availability`
- Timeout: `5000` ms
- Parameters:

```json
{
  "type": "object",
  "properties": {
    "date": {
      "type": "string",
      "description": "Appointment date in YYYY-MM-DD format, for example 2030-01-07"
    }
  },
  "required": ["date"]
}
```

### book_appointment

- Description: `Book the appointment after the caller has chosen a time. Use the exact slot_id returned by check_availability for that time.`
- Method: `POST`
- URL: `https://<ngrok-address>/functions/book_appointment`
- Timeout: `5000` ms
- Parameters:

```json
{
  "type": "object",
  "properties": {
    "slot_id": {
      "type": "string",
      "description": "Exact slot_id returned by check_availability for the chosen time"
    },
    "patient_name": {
      "type": "string",
      "description": "The patient's full name"
    },
    "reason": {
      "type": "string",
      "description": "Reason for the visit, for example cleaning or toothache"
    }
  },
  "required": ["slot_id", "patient_name"]
}
```

The top level of each schema must contain `"type": "object"`, otherwise the function will not save. If the editor offers a message to speak while a function runs, add a short one such as "One moment while I check."

## 5. Register the webhook

Open the **Webhooks** tab and set the account-level URL to `https://<ngrok-address>/webhooks/retell`. An agent-level URL, if you set one, takes precedence over the account-level one.

## 6. Test with a web call

Click **Test** on the agent and allow microphone access in the browser. Say: "Hi, I'd like a cleaning next Tuesday. My name is Sam Njogu."

Then check, in order:

1. The server terminal shows two function requests.
2. `curl -H "Authorization: Bearer $ADMIN_TOKEN" https://<ngrok-address>/functions` lists them with their latency.
3. `curl -H "Authorization: Bearer $ADMIN_TOKEN" https://<ngrok-address>/calls` shows the call after it ends, and gains its summary after `call_analyzed`.
4. Retell's call history shows the transcript, analysis and detailed log for the same call ID.

## 7. Troubleshooting

| Problem | Check |
|---|---|
| Function requests never arrive | Tunnel running, URL correct, ngrok inspector at `http://127.0.0.1:4040` |
| 401 on functions or webhooks | Key with the webhook badge, header present, **Payload: args only** off |
| Agent never calls a function | Function description and the booking flow in the prompt |
| Agent offers times the tool did not return | Strengthen the rule against inventing times |
| No rows in `/calls` | Webhook URL registered, and `call_ended` only arrives for calls that connected |
| `/calls` returns 503 | `ADMIN_TOKEN` is not set in `.env` |
| Works once, then breaks | The free ngrok address changed after a restart |
