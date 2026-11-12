"""Sanity tests without Retell: forge a valid signature and hit the endpoints."""
import hashlib, hmac, json, os, time
os.environ.setdefault("RETELL_API_KEY", "key_test123")
os.environ["DB_PATH"] = "test.db"
from fastapi.testclient import TestClient
import main

client = TestClient(main.app)

def sign(raw: str) -> str:
    ts = str(int(time.time() * 1000))
    d = hmac.new(main.API_KEY.encode(), (raw + ts).encode(), hashlib.sha256).hexdigest()
    return f"v={ts},d={d}"

def post(path, payload, sig=True):
    raw = json.dumps(payload)
    h = {"X-Retell-Signature": sign(raw)} if sig else {}
    return client.post(path, content=raw, headers=h)

def test_unsigned_rejected():
    assert post("/functions/check_availability", {"args": {"date": "2030-01-07"}}, sig=False).status_code == 401

def test_availability_and_booking():
    r = post("/functions/check_availability", {"call": {"call_id": "c1"}, "args": {"date": "2030-01-07"}})
    assert r.status_code == 200 and r.json()["slot_ids"]
    slot = r.json()["slot_ids"][0]
    b = post("/functions/book_appointment", {"call": {"call_id": "c1"}, "args": {"slot_id": slot, "patient_name": "Sam", "reason": "cleaning"}})
    assert b.json()["booked"] is True
    b2 = post("/functions/book_appointment", {"call": {"call_id": "c2"}, "args": {"slot_id": slot, "patient_name": "Ann"}})
    assert b2.json()["booked"] is False

def test_webhook_stores_call():
    call = {"call_id": "c1", "agent_id": "a1", "call_status": "ended", "transcript": "hi", "call_analysis": {"call_summary": "booked", "call_successful": True}}
    assert post("/webhooks/retell", {"event": "call_analyzed", "call": call}).status_code == 200
    d = client.get("/calls/c1").json()
    assert d["call"]["summary"] == "booked" and len(d["function_calls"]) >= 2
