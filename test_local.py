"""Sanity tests without Retell: forge a valid signature and hit the endpoints.

Each test gets its own throwaway SQLite file, so runs never leak state into each other.
"""
import asyncio, hashlib, hmac, json, os, tempfile, time

# Set before importing main so a developer's real .env can't leak in (load_dotenv won't override).
os.environ["RETELL_API_KEY"] = "key_test123"
os.environ["FAIL_MODE"] = "none"
os.environ["VERIFY_FUNCTIONS"] = "true"
os.environ["ADMIN_TOKEN"] = "test-admin-token"
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "import.db")

import httpx
import pytest
from fastapi.testclient import TestClient
import main

client = TestClient(main.app)
ADMIN = {"Authorization": "Bearer test-admin-token"}
MONDAY = "2030-01-07"
SUNDAY = "2030-01-06"


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
    h = {"X-Retell-Signature": sign(raw)} if sig else {}
    return client.post(path, content=raw, headers=h)


def check(date, call_id="c1"):
    return post("/functions/check_availability", {"call": {"call_id": call_id}, "args": {"date": date}})


def book(slot, name="Sam", call_id="c1"):
    return post("/functions/book_appointment", {"call": {"call_id": call_id}, "args": {"slot_id": slot, "patient_name": name, "reason": "cleaning"}})


def test_unsigned_rejected():
    assert post("/functions/check_availability", {"args": {"date": MONDAY}}, sig=False).status_code == 401


def test_availability_and_booking():
    r = check(MONDAY)
    assert r.status_code == 200 and r.json()["slot_ids"]
    slot = r.json()["slot_ids"][0]
    assert book(slot).json()["booked"] is True
    b2 = book(slot, name="Ann", call_id="c2")
    assert b2.json()["booked"] is False and "just taken" in b2.json()["message"]


def test_webhook_stores_call():
    check(MONDAY)  # gives c1 a function call to join against
    call = {"call_id": "c1", "agent_id": "a1", "call_status": "ended", "transcript": "hi", "call_analysis": {"call_summary": "booked", "call_successful": True}}
    assert post("/webhooks/retell", {"event": "call_analyzed", "call": call}).status_code == 200
    d = client.get("/calls/c1", headers=ADMIN).json()
    assert d["call"]["summary"] == "booked" and len(d["function_calls"]) >= 1


@pytest.mark.parametrize("slot", [
    f"{SUNDAY}T10:00:00-08:00",    # weekend, never offered
    f"{MONDAY}T09:30:00-08:00",    # off the hourly grid
    f"{MONDAY}T03:00:00-08:00",    # outside clinic hours
    f"{MONDAY}T09:00:00",          # no offset
    "tomorrow 3pm",                # garbage from the LLM
    "",                            # empty
])
def test_invalid_slot_rejected_and_not_stored(slot):
    r = book(slot).json()
    assert r["booked"] is False and "Invalid slot_id" in r["message"]
    assert main.db().execute("SELECT COUNT(*) FROM appointments").fetchone()[0] == 0


def test_past_slot_rejected():
    r = book("1999-01-04T09:00:00-08:00").json()
    assert r["booked"] is False and "already passed" in r["message"]
    assert main.db().execute("SELECT COUNT(*) FROM appointments").fetchone()[0] == 0


def test_log_viewer_requires_token():
    for path in ["/calls", "/calls/c1", "/functions"]:
        assert client.get(path).status_code == 401
        assert client.get(path, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/calls", headers=ADMIN).status_code == 200
    assert client.get("/functions", headers=ADMIN).status_code == 200
    assert client.get("/health").status_code == 200


def test_log_viewer_fails_closed_without_token(monkeypatch):
    monkeypatch.setattr(main, "ADMIN_TOKEN", "")
    assert client.get("/calls", headers=ADMIN).status_code == 503


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
