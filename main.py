"""Retell dental-clinic receptionist backend.

Endpoints
  POST /functions/check_availability   custom function called mid-call by the agent
  POST /functions/book_appointment     custom function called mid-call by the agent
  POST /webhooks/retell                call_started / call_ended / call_analyzed
  GET  /calls, /calls/{id}, /functions  tiny log viewer (JSON)
"""
import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from retell import Retell

load_dotenv()
API_KEY = os.environ["RETELL_API_KEY"]
CLINIC_TZ = ZoneInfo(os.getenv("CLINIC_TZ", "America/Los_Angeles"))
FAIL_MODE = os.getenv("FAIL_MODE", "none")
VERIFY_FUNCTIONS = os.getenv("VERIFY_FUNCTIONS", "true").lower() == "true"
DB_PATH = os.getenv("DB_PATH", "clinic.db")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("clinic")
retell = Retell(api_key=API_KEY)
app = FastAPI(title="Retell clinic demo")


# ---------- storage ----------
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


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
        log.warning("signature check FAILED path=%s sig_present=%s", request.url.path, bool(sig))
        raise HTTPException(status_code=401, detail="invalid signature")
    return json.loads(raw)


def log_function_call(call_id, name, args, result, ok, latency_ms, error=None) -> None:
    with db() as c:
        c.execute(
            "INSERT INTO function_calls(call_id,name,args,result,ok,latency_ms,error,ts)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (call_id, name, json.dumps(args), json.dumps(result), int(ok), latency_ms, error, int(time.time())),
        )


def open_slots(day: datetime) -> list[datetime]:
    """Hourly slots 09:00-16:00 clinic time, Mon-Fri, minus anything booked."""
    if day.weekday() >= 5:
        return []
    tz = CLINIC_TZ
    if FAIL_MODE == "tz_bug":  # INCIDENT: naive UTC instead of clinic time
        tz = ZoneInfo("UTC")
    slots = [datetime(day.year, day.month, day.day, h, 0, tzinfo=tz) for h in range(9, 17)]
    with db() as c:
        taken = {r["slot_iso"] for r in c.execute("SELECT slot_iso FROM appointments")}
    return [s for s in slots if s.isoformat() not in taken]


def human(s: datetime) -> str:
    return s.strftime("%A %B %d at %I:%M %p").replace(" 0", " ")


def apply_fail_mode() -> None:
    if FAIL_MODE == "slow":
        time.sleep(12)  # INCIDENT: exceeds the agent's function timeout
    elif FAIL_MODE == "error":
        raise RuntimeError("simulated upstream scheduling system failure")


# ---------- custom functions ----------
@app.post("/functions/check_availability")
async def check_availability(request: Request):
    body = await verified_body(request, enforce=VERIFY_FUNCTIONS)
    call_id = body.get("call", {}).get("call_id", "unknown")
    args = body.get("args", {})
    t0 = time.time()
    try:
        apply_fail_mode()
        day = datetime.strptime(args["date"], "%Y-%m-%d")
        if day.date() < datetime.now(CLINIC_TZ).date():
            result = {"available": [], "message": "That date is in the past."}
        else:
            slots = open_slots(day)
            result = {
                "available": [human(s) for s in slots],
                "slot_ids": [s.isoformat() for s in slots],
                "message": "No openings that day." if not slots else "Openings found.",
            }
        log_function_call(call_id, "check_availability", args, result, True, int((time.time() - t0) * 1000))
        return result
    except Exception as e:  # never return a bare 500 to a live call
        log.exception("check_availability failed call_id=%s", call_id)
        log_function_call(call_id, "check_availability", args, None, False, int((time.time() - t0) * 1000), str(e))
        return {"error": "scheduling system unavailable", "message": "Apologise and offer a callback."}


@app.post("/functions/book_appointment")
async def book_appointment(request: Request):
    body = await verified_body(request, enforce=VERIFY_FUNCTIONS)
    call_id = body.get("call", {}).get("call_id", "unknown")
    args = body.get("args", {})
    t0 = time.time()
    try:
        apply_fail_mode()
        slot = args["slot_id"]
        with db() as c:
            try:
                c.execute(
                    "INSERT INTO appointments(call_id,patient,slot_iso,reason,created_at) VALUES (?,?,?,?,?)",
                    (call_id, args["patient_name"], slot, args.get("reason", ""), int(time.time())),
                )
                result = {"booked": True, "message": f"Booked {args['patient_name']} for {human(datetime.fromisoformat(slot))}."}
            except sqlite3.IntegrityError:
                result = {"booked": False, "message": "That slot was just taken. Offer another time."}
        log_function_call(call_id, "book_appointment", args, result, True, int((time.time() - t0) * 1000))
        return result
    except Exception as e:
        log.exception("book_appointment failed call_id=%s", call_id)
        log_function_call(call_id, "book_appointment", args, None, False, int((time.time() - t0) * 1000), str(e))
        return {"booked": False, "message": "Scheduling system unavailable. Offer a callback."}


# ---------- webhook ----------
def store_event(event: str, call: dict) -> None:
    cid = call["call_id"]
    analysis = call.get("call_analysis") or {}
    with db() as c:
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
    log.info("stored event=%s call_id=%s", event, cid)


@app.post("/webhooks/retell")
async def retell_webhook(request: Request, background: BackgroundTasks):
    body = await verified_body(request)
    event, call = body.get("event"), body.get("call", {})
    background.add_task(store_event, event, call)  # ack fast (10s limit), process after
    return {"ok": True}


# ---------- log viewer ----------
@app.get("/calls")
def list_calls(limit: int = 20):
    with db() as c:
        rows = c.execute(
            "SELECT call_id,status,duration_ms,disconnection_reason,sentiment,call_successful,summary"
            " FROM calls ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


@app.get("/calls/{call_id}")
def get_call(call_id: str):
    with db() as c:
        call = c.execute("SELECT call_id,status,duration_ms,disconnection_reason,transcript,summary,sentiment,call_successful FROM calls WHERE call_id=?", (call_id,)).fetchone()
        fns = c.execute("SELECT name,args,result,ok,latency_ms,error FROM function_calls WHERE call_id=? ORDER BY id", (call_id,)).fetchall()
    if not call:
        raise HTTPException(404, "call not found")
    return {"call": dict(call), "function_calls": [dict(f) for f in fns]}


@app.get("/functions")
def list_function_calls(limit: int = 30):
    with db() as c:
        rows = c.execute("SELECT * FROM function_calls ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


@app.get("/health")
def health():
    return {"ok": True, "fail_mode": FAIL_MODE}
