"""Brightside Dental receptionist backend for a Retell voice agent.

Endpoints
  POST /functions/check_availability   custom function, called mid-call by the agent
  POST /functions/book_appointment     custom function, called mid-call by the agent
  POST /webhooks/retell                call_started / call_ended / call_analyzed
  GET  /calls, /calls/{id}, /functions  log viewer (bearer token required)
  GET  /health
"""
import asyncio
import hmac
import json
import logging
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from retell import Retell

load_dotenv()
API_KEY = os.environ["RETELL_API_KEY"]
CLINIC_TZ = ZoneInfo(os.getenv("CLINIC_TZ", "America/Los_Angeles"))
FAIL_MODE = os.getenv("FAIL_MODE", "none")
VERIFY_FUNCTIONS = os.getenv("VERIFY_FUNCTIONS", "true").lower() == "true"
DB_PATH = os.getenv("DB_PATH", "clinic.db")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")
SLOW_SECONDS = float(os.getenv("SLOW_SECONDS") or "12")
FUNCTION_DEADLINE_S = float(os.getenv("FUNCTION_DEADLINE_S") or "5")
HANDLED_EVENTS = {"call_started", "call_ended", "call_analyzed"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("clinic")
retell = Retell(api_key=API_KEY)
app = FastAPI(title="Retell clinic demo", docs_url=None, redoc_url=None, openapi_url=None)


# ---------- storage ----------
@contextmanager
def db():
    """Open a connection, commit on success, roll back on error, always close."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init_db() -> None:
    with db() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS calls (
              call_id TEXT PRIMARY KEY, agent_id TEXT, status TEXT,
              started_at INTEGER, ended_at INTEGER, duration_ms INTEGER,
              disconnection_reason TEXT, transcript TEXT,
              summary TEXT, sentiment TEXT, call_successful INTEGER,
              raw_json TEXT, updated_at INTEGER);
            CREATE TABLE IF NOT EXISTS webhook_events (
              event TEXT NOT NULL, call_id TEXT NOT NULL, received_at INTEGER NOT NULL,
              PRIMARY KEY (event, call_id));
            CREATE TABLE IF NOT EXISTS function_calls (
              id INTEGER PRIMARY KEY AUTOINCREMENT, call_id TEXT, name TEXT,
              args TEXT, result TEXT, ok INTEGER, latency_ms INTEGER,
              error TEXT, ts INTEGER);
            CREATE TABLE IF NOT EXISTS appointments (
              id INTEGER PRIMARY KEY AUTOINCREMENT, call_id TEXT, patient TEXT,
              slot_iso TEXT UNIQUE, reason TEXT, created_at INTEGER);
            """
        )


init_db()


# ---------- helpers ----------
async def verified_body(request: Request, enforce: bool = True) -> dict:
    """Verify X-Retell-Signature against the RAW body, then parse JSON."""
    raw = (await request.body()).decode("utf-8")
    sig = request.headers.get("X-Retell-Signature", "")
    if enforce and not retell.verify(raw, api_key=API_KEY, signature=sig):
        log.warning("signature check failed path=%s header_present=%s", request.url.path, bool(sig))
        raise HTTPException(status_code=401, detail="invalid signature")
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="body is not valid JSON")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    return body


def require_admin(request: Request) -> None:
    """Bearer-token guard for the log viewer. Fails closed if ADMIN_TOKEN is unset."""
    if not ADMIN_TOKEN:
        raise HTTPException(status_code=503, detail="ADMIN_TOKEN not configured")
    supplied = request.headers.get("Authorization", "")
    if not hmac.compare_digest(supplied.encode(), f"Bearer {ADMIN_TOKEN}".encode()):
        raise HTTPException(status_code=401, detail="unauthorized")


def log_function_call(call_id, name, args, result, ok, t0, error=None) -> None:
    latency_ms = int((time.time() - t0) * 1000)
    with db() as c:
        c.execute(
            "INSERT INTO function_calls(call_id,name,args,result,ok,latency_ms,error,ts)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (call_id, name, json.dumps(args), json.dumps(result), int(ok), latency_ms, error, int(time.time())),
        )


def clean_text(value, limit: int = 120) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def slot_grid(day: datetime) -> list[datetime]:
    """Every bookable hourly slot 09:00-16:00 clinic time, Mon-Fri, booked or not."""
    if day.weekday() >= 5:
        return []
    tz = CLINIC_TZ
    if FAIL_MODE == "tz_bug":  # INCIDENT: UTC instead of clinic time
        tz = ZoneInfo("UTC")
    return [datetime(day.year, day.month, day.day, h, 0, tzinfo=tz) for h in range(9, 17)]


def open_slots(day: datetime) -> list[datetime]:
    """The grid minus anything already booked."""
    slots = slot_grid(day)
    with db() as c:
        taken = {r["slot_iso"] for r in c.execute("SELECT slot_iso FROM appointments")}
    return [s for s in slots if s.isoformat() not in taken]


def human(s: datetime) -> str:
    return s.strftime("%A %B %d at %I:%M %p").replace(" 0", " ")


async def apply_fail_mode() -> None:
    if FAIL_MODE == "slow":
        # INCIDENT: exceeds the agent's function timeout. Non-blocking on purpose, so a slow
        # dependency cannot freeze the whole server.
        await asyncio.sleep(SLOW_SECONDS)
    elif FAIL_MODE == "error":
        raise RuntimeError("simulated upstream scheduling system failure")


# ---------- custom functions ----------
@app.post("/functions/check_availability")
async def check_availability(request: Request):
    body = await verified_body(request, enforce=VERIFY_FUNCTIONS)
    call_id = (body.get("call") or {}).get("call_id", "unknown")
    args = body.get("args") or {}
    t0 = time.time()
    try:
        try:
            day = datetime.strptime(str(args.get("date", "")), "%Y-%m-%d")
        except ValueError:
            result = {"available": [], "message": "Invalid date. Ask for the day again and use YYYY-MM-DD."}
            log_function_call(call_id, "check_availability", args, result, False, t0, "invalid date")
            return result
        await apply_fail_mode()
        if day.date() < datetime.now(CLINIC_TZ).date():
            result = {"available": [], "message": "That date is in the past."}
        else:
            now = datetime.now(CLINIC_TZ)
            slots = [s for s in open_slots(day) if s > now]
            result = {
                "available": [human(s) for s in slots],
                "slot_ids": [s.isoformat() for s in slots],
                "message": "No openings that day." if not slots else "Openings found.",
            }
        log_function_call(call_id, "check_availability", args, result, True, t0)
        return result
    except Exception as e:  # never return a bare 500 to a live call
        log.exception("check_availability failed call_id=%s", call_id)
        log_function_call(call_id, "check_availability", args, None, False, t0, str(e))
        return {"available": [], "message": "Scheduling system unavailable. Apologise and offer a callback."}


def reject_booking(call_id, args, t0, message: str, reason: str) -> dict:
    result = {"booked": False, "message": message}
    log_function_call(call_id, "book_appointment", args, result, False, t0, reason)
    return result


@app.post("/functions/book_appointment")
async def book_appointment(request: Request):
    body = await verified_body(request, enforce=VERIFY_FUNCTIONS)
    call_id = (body.get("call") or {}).get("call_id", "unknown")
    args = body.get("args") or {}
    t0 = time.time()
    try:
        name = clean_text(args.get("patient_name"))
        if not name:
            return reject_booking(call_id, args, t0, "Missing patient name. Ask for the patient's full name.", "missing patient_name")
        slot = args.get("slot_id")
        try:
            when = datetime.fromisoformat(slot)
        except (TypeError, ValueError):
            when = None
        if when is None or when.tzinfo is None or slot not in {s.isoformat() for s in slot_grid(when)}:
            return reject_booking(
                call_id, args, t0,
                "Invalid slot_id. Call check_availability again and pass an exact slot_id from its result.",
                "invalid slot_id",
            )
        if when <= datetime.now(CLINIC_TZ):
            return reject_booking(call_id, args, t0, "That time has already passed. Offer another time.", "slot in the past")

        await apply_fail_mode()
        # Retell stops waiting after its function timeout, so a booking committed after
        # that point would exist while the caller was told it failed.
        if FUNCTION_DEADLINE_S > 0 and time.time() - t0 > FUNCTION_DEADLINE_S:
            return reject_booking(
                call_id, args, t0,
                "Scheduling took too long. Offer a callback.",
                f"deadline of {FUNCTION_DEADLINE_S:g}s exceeded, not committed",
            )

        with db() as c:
            try:
                c.execute(
                    "INSERT INTO appointments(call_id,patient,slot_iso,reason,created_at) VALUES (?,?,?,?,?)",
                    (call_id, name, slot, clean_text(args.get("reason"), 200), int(time.time())),
                )
                result = {"booked": True, "message": f"Booked {name} for {human(when)}."}
            except sqlite3.IntegrityError:
                result = {"booked": False, "message": "That slot was just taken. Offer another time."}
        log_function_call(call_id, "book_appointment", args, result, True, t0)
        return result
    except Exception as e:
        log.exception("book_appointment failed call_id=%s", call_id)
        log_function_call(call_id, "book_appointment", args, None, False, t0, str(e))
        return {"booked": False, "message": "Scheduling system unavailable. Offer a callback."}


# ---------- webhook ----------
def store_event(event: str, call: dict) -> bool:
    """Persist one webhook event. Returns False if this (event, call_id) was already stored."""
    cid = call["call_id"]
    analysis = call.get("call_analysis") or {}
    with db() as c:
        seen = c.execute(
            "INSERT OR IGNORE INTO webhook_events(event, call_id, received_at) VALUES (?,?,?)",
            (event, cid, int(time.time())),
        )
        if seen.rowcount == 0:
            return False
        c.execute("INSERT OR IGNORE INTO calls(call_id) VALUES (?)", (cid,))
        c.execute(
            """UPDATE calls SET agent_id=?, status=?,
               started_at=COALESCE(?,started_at), ended_at=COALESCE(?,ended_at),
               duration_ms=COALESCE(?,duration_ms),
               disconnection_reason=COALESCE(?,disconnection_reason),
               transcript=COALESCE(?,transcript), summary=COALESCE(?,summary),
               sentiment=COALESCE(?,sentiment), call_successful=COALESCE(?,call_successful),
               raw_json=?, updated_at=? WHERE call_id=?""",
            (
                call.get("agent_id"), call.get("call_status") or event,
                call.get("start_timestamp"), call.get("end_timestamp"),
                call.get("duration_ms"), call.get("disconnection_reason"),
                call.get("transcript"), analysis.get("call_summary"),
                analysis.get("user_sentiment"),
                None if "call_successful" not in analysis else int(bool(analysis["call_successful"])),
                json.dumps(call), int(time.time()), cid,
            ),
        )
    return True


@app.post("/webhooks/retell")
async def retell_webhook(request: Request):
    body = await verified_body(request)
    event, call = body.get("event"), body.get("call") or {}
    if event not in HANDLED_EVENTS:
        return Response(status_code=204)
    if not call.get("call_id"):
        raise HTTPException(status_code=400, detail="missing call_id")
    stored = store_event(event, call)  # persist first, then acknowledge
    log.info("webhook event=%s call_id=%s stored=%s", event, call["call_id"], stored)
    return Response(status_code=204)


# ---------- log viewer ----------
@app.get("/calls", dependencies=[Depends(require_admin)])
def list_calls(limit: int = Query(20, ge=1, le=200)):
    with db() as c:
        rows = c.execute(
            "SELECT call_id,status,duration_ms,disconnection_reason,sentiment,call_successful,summary"
            " FROM calls ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


@app.get("/calls/{call_id}", dependencies=[Depends(require_admin)])
def get_call(call_id: str):
    with db() as c:
        call = c.execute(
            "SELECT call_id,status,duration_ms,disconnection_reason,transcript,summary,sentiment,call_successful"
            " FROM calls WHERE call_id=?", (call_id,)).fetchone()
        fns = c.execute(
            "SELECT name,args,result,ok,latency_ms,error FROM function_calls WHERE call_id=? ORDER BY id",
            (call_id,)).fetchall()
    if not call:
        raise HTTPException(404, "call not found")
    return {"call": dict(call), "function_calls": [dict(f) for f in fns]}


@app.get("/functions", dependencies=[Depends(require_admin)])
def list_function_calls(limit: int = Query(30, ge=1, le=200)):
    with db() as c:
        rows = c.execute("SELECT * FROM function_calls ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


@app.get("/health")
def health():
    return {"ok": True}
