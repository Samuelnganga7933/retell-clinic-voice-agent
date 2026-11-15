"""Local tests: forge a valid Retell signature and exercise the endpoints without Retell.

Each test gets its own throwaway SQLite file, so runs never leak state into each other.
"""
import asyncio
import hashlib
import hmac
import json
import os
import subprocess
import sys
import tempfile
import time

# Set before importing main so a developer's real .env cannot leak in (load_dotenv does not override).
os.environ["RETELL_API_KEY"] = "key_test123"
os.environ["FAIL_MODE"] = "none"
os.environ["VERIFY_FUNCTIONS"] = "true"
os.environ["ADMIN_TOKEN"] = "test-admin-token"
os.environ["FUNCTION_DEADLINE_S"] = "5"
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "import.db")

import httpx
import pytest
from fastapi.testclient import TestClient

import main

client = TestClient(main.app)
ADMIN = {"Authorization": "Bearer test-admin-token"}
MONDAY = "2030-01-07"
SUNDAY = "2030-01-06"
SLOT = f"{MONDAY}T09:00:00-08:00"


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", str(tmp_path / "t.db"))
    main.init_db()


def sign(raw: str) -> str:
    ts = str(int(time.time() * 1000))
    d = hmac.new(main.API_KEY.encode(), (raw + ts).encode(), hashlib.sha256).hexdigest()
    return f"v={ts},d={d}"


def post(path, payload, sig=True):
    raw = json.dumps(payload)
    headers = {"X-Retell-Signature": sign(raw)} if sig else {}
    return client.post(path, content=raw, headers=headers)


def check(date, call_id="c1"):
    return post("/functions/check_availability", {"call": {"call_id": call_id}, "args": {"date": date}})


def book(slot, name="Sam", call_id="c1", **extra):
    args = {"slot_id": slot, "patient_name": name, "reason": "cleaning", **extra}
    return post("/functions/book_appointment", {"call": {"call_id": call_id}, "args": args})


def appointment_count() -> int:
    with main.db() as c:
        return c.execute("SELECT COUNT(*) FROM appointments").fetchone()[0]


# ---------- signatures ----------
def test_unsigned_function_call_rejected():
    assert post("/functions/check_availability", {"args": {"date": MONDAY}}, sig=False).status_code == 401


def test_unsigned_webhook_rejected():
    assert post("/webhooks/retell", {"event": "call_ended", "call": {"call_id": "c1"}}, sig=False).status_code == 401


# ---------- availability and booking ----------
def test_availability_and_booking():
    r = check(MONDAY)
    assert r.status_code == 200 and r.json()["slot_ids"]
    slot = r.json()["slot_ids"][0]
    assert book(slot).json()["booked"] is True
    second = book(slot, name="Ann", call_id="c2").json()
    assert second["booked"] is False and "just taken" in second["message"]
    assert slot not in check(MONDAY).json()["slot_ids"]


def test_weekend_has_no_slots():
    assert check(SUNDAY).json()["available"] == []


def test_invalid_date_gets_a_specific_message():
    r = check("next tuesday").json()
    assert r["available"] == [] and "YYYY-MM-DD" in r["message"]


@pytest.mark.parametrize("slot", [
    f"{SUNDAY}T10:00:00-08:00",    # weekend, never offered
    f"{MONDAY}T09:30:00-08:00",    # off the hourly grid
    f"{MONDAY}T03:00:00-08:00",    # outside clinic hours
    f"{MONDAY}T17:00:00+00:00",    # same instant as a real slot, different string
    f"{MONDAY}T09:00:00",          # no offset
    "tomorrow 3pm",                # free text from the model
    "",
    None,
])
def test_invalid_slot_rejected_and_not_stored(slot):
    r = book(slot).json()
    assert r["booked"] is False and "Invalid slot_id" in r["message"]
    assert appointment_count() == 0


def test_past_slot_rejected():
    r = book("1999-01-04T09:00:00-08:00").json()
    assert r["booked"] is False and "already passed" in r["message"]
    assert appointment_count() == 0


@pytest.mark.parametrize("name", ["", "   ", None])
def test_missing_patient_name_rejected(name):
    r = book(SLOT, name=name).json()
    assert r["booked"] is False and "patient name" in r["message"]
    assert appointment_count() == 0


def test_patient_name_is_trimmed():
    assert book(SLOT, name="  Sam Njogu  ").json()["message"].startswith("Booked Sam Njogu for")


# ---------- failure modes ----------
def test_error_mode_returns_guidance_not_a_500(monkeypatch):
    monkeypatch.setattr(main, "FAIL_MODE", "error")
    r = book(SLOT)
    assert r.status_code == 200 and r.json()["booked"] is False and "callback" in r.json()["message"]
    assert appointment_count() == 0


def test_slow_mode_does_not_block_the_server(monkeypatch):
    """Regression: time.sleep in an async handler froze every other request."""
    monkeypatch.setattr(main, "FAIL_MODE", "slow")
    monkeypatch.setattr(main, "SLOW_SECONDS", 0.6)
    raw = json.dumps({"call": {"call_id": "slow1"}, "args": {"date": MONDAY}})

    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://t") as ac:
            slow = asyncio.create_task(ac.post("/functions/check_availability", content=raw, headers={"X-Retell-Signature": sign(raw)}))
            await asyncio.sleep(0.1)
            t0 = time.time()
            health = await ac.get("/health")
            elapsed = time.time() - t0
            await slow
            return health.status_code, elapsed

    status, elapsed = asyncio.run(go())
    assert status == 200 and elapsed < 0.3


def test_late_booking_is_refused_when_deadline_enforced(monkeypatch):
    monkeypatch.setattr(main, "FAIL_MODE", "slow")
    monkeypatch.setattr(main, "SLOW_SECONDS", 0.3)
    monkeypatch.setattr(main, "FUNCTION_DEADLINE_S", 0.1)
    r = book(SLOT).json()
    assert r["booked"] is False and "too long" in r["message"]
    assert appointment_count() == 0


def test_late_booking_commits_without_deadline(monkeypatch):
    """Documents the ghost booking: with the check off, a late handler still writes."""
    monkeypatch.setattr(main, "FAIL_MODE", "slow")
    monkeypatch.setattr(main, "SLOW_SECONDS", 0.3)
    monkeypatch.setattr(main, "FUNCTION_DEADLINE_S", 0)
    assert book(SLOT).json()["booked"] is True
    assert appointment_count() == 1


def test_tz_bug_changes_the_stored_offset(monkeypatch):
    monkeypatch.setattr(main, "FAIL_MODE", "tz_bug")
    assert check(MONDAY).json()["slot_ids"][0].endswith("+00:00")


def test_fail_target_limits_the_drill_to_one_function(monkeypatch):
    monkeypatch.setattr(main, "FAIL_MODE", "error")
    monkeypatch.setattr(main, "FAIL_TARGET", "check_availability")
    unavailable = check(MONDAY).json()
    assert unavailable["available"] == [] and "unavailable" in unavailable["message"]
    assert book(SLOT).json()["booked"] is True

    monkeypatch.setattr(main, "FAIL_TARGET", "book_appointment")
    assert check(MONDAY).json()["slot_ids"]
    failed = book(SLOT, call_id="c2").json()
    assert failed["booked"] is False and "callback" in failed["message"]


def test_startup_rejects_unknown_fail_target(tmp_path):
    env = os.environ.copy()
    env.update({
        "FAIL_TARGET": "book_apointment",
        "DB_PATH": str(tmp_path / "startup.db"),
        "RETELL_API_KEY": "key_test123",
    })
    result = subprocess.run(
        [sys.executable, "-c", "import main"],
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "FAIL_TARGET must be all, check_availability or book_appointment" in result.stderr


# ---------- webhooks ----------
def call_payload(event="call_analyzed", **extra):
    call = {"call_id": "c1", "agent_id": "a1", "call_status": "ended", "transcript": "hi",
            "call_analysis": {"call_summary": "booked", "call_successful": True}, **extra}
    return {"event": event, "call": call}


def test_webhook_acknowledges_with_204_and_stores_call():
    check(MONDAY)  # gives c1 a function call to join against
    assert post("/webhooks/retell", call_payload()).status_code == 204
    d = client.get("/calls/c1", headers=ADMIN).json()
    assert d["call"]["summary"] == "booked" and len(d["function_calls"]) == 1
    fn = d["function_calls"][0]
    assert fn["name"] == "check_availability" and fn["ok"] is True
    assert fn["args"] == {"date": MONDAY} and fn["result"]["slot_ids"]


def test_duplicate_webhook_is_ignored():
    assert post("/webhooks/retell", call_payload()).status_code == 204
    assert post("/webhooks/retell", call_payload(call_status="changed")).status_code == 204
    assert client.get("/calls/c1", headers=ADMIN).json()["call"]["status"] == "ended"


def test_call_analyzed_updates_the_record_from_call_ended():
    post("/webhooks/retell", {"event": "call_ended", "call": {"call_id": "c1", "call_status": "ended", "duration_ms": 1234, "disconnection_reason": "user_hangup"}})
    post("/webhooks/retell", call_payload())
    call = client.get("/calls/c1", headers=ADMIN).json()["call"]
    assert call["duration_ms"] == 1234 and call["disconnection_reason"] == "user_hangup" and call["summary"] == "booked"


def test_unhandled_event_is_acknowledged_and_ignored():
    assert post("/webhooks/retell", {"event": "transcript_updated", "call": {"call_id": "c9"}}).status_code == 204
    assert client.get("/calls/c9", headers=ADMIN).status_code == 404


def test_webhook_without_call_id_is_a_400():
    assert post("/webhooks/retell", {"event": "call_ended", "call": {}}).status_code == 400


def test_malformed_json_is_a_400():
    raw = "{not json"
    r = client.post("/webhooks/retell", content=raw, headers={"X-Retell-Signature": sign(raw)})
    assert r.status_code == 400


# ---------- log viewer and surface area ----------
def test_log_viewer_requires_token():
    for path in ["/calls", "/calls/c1", "/functions"]:
        assert client.get(path).status_code == 401
        assert client.get(path, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/calls", headers=ADMIN).status_code == 200
    assert client.get("/functions", headers=ADMIN).status_code == 200


def test_log_viewer_fails_closed_without_token(monkeypatch):
    monkeypatch.setattr(main, "ADMIN_TOKEN", "")
    assert client.get("/calls", headers=ADMIN).status_code == 503


def test_health_is_public_and_minimal():
    r = client.get("/health")
    assert r.status_code == 200 and r.json() == {"ok": True}


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_api_docs_are_not_exposed(path):
    assert client.get(path).status_code == 404
