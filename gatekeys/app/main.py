"""
gatekeys — issue and manage the QR codes that may open the gate.

Deliberately a separate service from ../intercom: that app views cameras and
pulses the gate relay and must keep working while this one is restarted,
edited or broken. The only contract between them is one call:

    intercom-app  ──POST /api/verify (shared secret)──▶  gatekeys
                  ◀── {"valid": true, "name": "..."} ───

gatekeys never touches the gate itself, and holds no intercom credentials.

Two audiences, two kinds of authentication:
  * people — session cookie from a password login, behind the LAN-only NPM
    access list. This GUI hands out gate keys, so a LAN device on its own is
    not enough.
  * the intercom app — a shared secret in the X-Gatekeys-Token header. No
    cookie, no session, no GUI.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from . import codes, db, schedule

log = logging.getLogger("gatekeys")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def _env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None or value == "":
        raise RuntimeError(f"{name} is not set")
    return value


ADMIN_USER = _env("GATEKEYS_USER")
ADMIN_PASSWORD = _env("GATEKEYS_PASSWORD")
SECRET_KEY = _env("GATEKEYS_SECRET_KEY")          # signs the session cookie
VERIFY_TOKEN = _env("GATEKEYS_VERIFY_TOKEN")      # what intercom-app presents
DB_PATH = Path(os.environ.get("GATEKEYS_DB") or "/data/gatekeys.db")
SESSION_HOURS = float(os.environ.get("GATEKEYS_SESSION_HOURS") or 12)
COOKIE = "gatekeys_session"

STATIC_DIR = Path(__file__).parent / "static"
app = FastAPI(title="gatekeys", docs_url=None, redoc_url=None)
db.connect(DB_PATH)


# --------------------------------------------------------------------------- #
# Session cookie: user|expiry|hmac. No server-side session store to lose.
# --------------------------------------------------------------------------- #

def _sign(payload: str) -> str:
    mac = hmac.new(SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}|{mac}"


def _valid_cookie(value: str | None) -> bool:
    if not value or value.count("|") != 2:
        return False
    user, expiry, mac = value.split("|")
    if not hmac.compare_digest(_sign(f"{user}|{expiry}"), value):
        return False
    try:
        return float(expiry) > time.time()
    except ValueError:
        return False


def logged_in(request: Request) -> bool:
    return _valid_cookie(request.cookies.get(COOKIE))


def needs_login() -> JSONResponse:
    return JSONResponse({"error": "login required"}, status_code=401)


def service_call(request: Request) -> bool:
    presented = request.headers.get("x-gatekeys-token", "")
    return bool(presented) and hmac.compare_digest(presented, VERIFY_TOKEN)


# --------------------------------------------------------------------------- #
# Shapes
# --------------------------------------------------------------------------- #

class WindowIn(BaseModel):
    weekdays: list[int]
    start: str
    end: str


class KeyIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    note: str = Field(default="", max_length=500)
    valid_from: str
    valid_to: str
    max_uses: int | None = None
    cooldown_seconds: int = 10
    windows: list[WindowIn]


class KeyPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    note: str | None = Field(default=None, max_length=500)
    valid_from: str | None = None
    valid_to: str | None = None
    max_uses: int | None = None
    clear_max_uses: bool = False
    cooldown_seconds: int | None = None
    revoked: bool | None = None
    windows: list[WindowIn] | None = None


class VerifyIn(BaseModel):
    code: str
    source: str = "unknown"


# --------------------------------------------------------------------------- #
# Reading keys
# --------------------------------------------------------------------------- #

def _windows_of(key_id: int) -> list[dict[str, Any]]:
    return db.query(
        "SELECT weekday, start_min, end_min FROM windows WHERE key_id = ? "
        "ORDER BY weekday, start_min", (key_id,)
    )


def _key_json(row: dict[str, Any], with_token: bool = True) -> dict[str, Any]:
    windows = _windows_of(row["id"])
    valid_from = schedule.parse_date(row["valid_from"])
    valid_to = schedule.parse_date(row["valid_to"])
    now = schedule.now_local()
    today = now.date()

    if row["revoked"]:
        state = "revoked"
    elif row["max_uses"] is not None and row["uses"] >= row["max_uses"]:
        state = "used up"
    elif today > valid_to:
        state = "expired"
    elif today < valid_from:
        state = "not yet valid"
    elif schedule.windows_allow(windows, now):
        state = "open now"
    else:
        state = "outside window"

    nxt = None
    if state in ("outside window", "not yet valid"):
        moment = schedule.next_opening(windows, valid_from, valid_to, now)
        nxt = moment.strftime("%a %d %b %H:%M") if moment else None

    out = {
        "id": row["id"],
        "name": row["name"],
        "note": row["note"],
        "created_at": row["created_at"],
        "valid_from": row["valid_from"],
        "valid_to": row["valid_to"],
        "max_uses": row["max_uses"],
        "uses": row["uses"],
        "cooldown_seconds": row["cooldown_seconds"],
        "revoked": bool(row["revoked"]),
        "last_used_at": row["last_used_at"],
        "windows": [
            {"weekday": w["weekday"],
             "start": schedule.format_hhmm(w["start_min"]),
             "end": schedule.format_hhmm(w["end_min"])}
            for w in windows
        ],
        "schedule_text": schedule.describe(windows),
        "state": state,
        "next_opening": nxt,
    }
    if with_token:
        out["token"] = row["token"]
    return out


# --------------------------------------------------------------------------- #
# Pages, health, login
# --------------------------------------------------------------------------- #

@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {"ok": True}


@app.post("/api/login")
async def login(request: Request) -> JSONResponse:
    body = await request.json()
    user = str(body.get("username", ""))
    password = str(body.get("password", ""))
    ok = hmac.compare_digest(user, ADMIN_USER) and hmac.compare_digest(password, ADMIN_PASSWORD)
    ip = request.headers.get("x-real-ip") or (request.client.host if request.client else "?")
    log.info("login %s from %s", "ok" if ok else "REFUSED", ip)
    if not ok:
        # Slow down guessing; the GUI shows a generic error either way.
        return JSONResponse({"error": "wrong username or password"}, status_code=401)
    expiry = time.time() + SESSION_HOURS * 3600
    response = JSONResponse({"ok": True, "user": ADMIN_USER})
    response.set_cookie(
        COOKIE, _sign(f"{ADMIN_USER}|{expiry:.0f}"),
        max_age=int(SESSION_HOURS * 3600), httponly=True, samesite="strict",
        secure=request.headers.get("x-forwarded-proto") == "https", path="/",
    )
    return response


@app.post("/api/logout")
async def logout() -> JSONResponse:
    response = JSONResponse({"ok": True})
    response.delete_cookie(COOKIE, path="/")
    return response


@app.get("/api/session")
async def session(request: Request) -> JSONResponse:
    return JSONResponse({"logged_in": logged_in(request), "user": ADMIN_USER if logged_in(request) else None})


# --------------------------------------------------------------------------- #
# Keys
# --------------------------------------------------------------------------- #

@app.get("/api/keys")
async def list_keys(request: Request) -> JSONResponse:
    if not logged_in(request):
        return needs_login()
    rows = db.query("SELECT * FROM keys ORDER BY revoked, name")
    return JSONResponse({"keys": [_key_json(r) for r in rows]}, headers={"Cache-Control": "no-store"})


@app.post("/api/keys")
async def create_key(request: Request, body: KeyIn) -> JSONResponse:
    if not logged_in(request):
        return needs_login()
    try:
        windows = schedule.clean_windows([w.model_dump() for w in body.windows])
        valid_from = schedule.parse_date(body.valid_from)
        valid_to = schedule.parse_date(body.valid_to)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    if valid_to < valid_from:
        return JSONResponse({"error": "the end date is before the start date"}, status_code=400)

    token = codes.new_token()
    key_id = db.execute(
        "INSERT INTO keys (token, name, note, created_at, valid_from, valid_to, max_uses,"
        " cooldown_seconds) VALUES (?,?,?,?,?,?,?,?)",
        (token, body.name.strip(), body.note.strip(),
         schedule.now_local().strftime("%Y-%m-%dT%H:%M:%S%z"),
         valid_from.isoformat(), valid_to.isoformat(),
         body.max_uses if (body.max_uses or 0) > 0 else None,
         max(0, body.cooldown_seconds)),
    )
    with db.transaction() as conn:
        conn.executemany(
            "INSERT INTO windows (key_id, weekday, start_min, end_min) VALUES (?,?,?,?)",
            [(key_id, day, start, end) for day, start, end in windows],
        )
    log.info("key %d created: %s", key_id, body.name)
    row = db.one("SELECT * FROM keys WHERE id = ?", (key_id,))
    return JSONResponse({"key": _key_json(row)}, status_code=201)


@app.patch("/api/keys/{key_id}")
async def update_key(request: Request, key_id: int, body: KeyPatch) -> JSONResponse:
    if not logged_in(request):
        return needs_login()
    row = db.one("SELECT * FROM keys WHERE id = ?", (key_id,))
    if not row:
        return JSONResponse({"error": "no such key"}, status_code=404)

    fields: dict[str, Any] = {}
    if body.name is not None:
        fields["name"] = body.name.strip()
    if body.note is not None:
        fields["note"] = body.note.strip()
    if body.cooldown_seconds is not None:
        fields["cooldown_seconds"] = max(0, body.cooldown_seconds)
    if body.revoked is not None:
        fields["revoked"] = int(body.revoked)
    if body.clear_max_uses:
        fields["max_uses"] = None
    elif body.max_uses is not None:
        fields["max_uses"] = body.max_uses if body.max_uses > 0 else None
    try:
        if body.valid_from is not None:
            fields["valid_from"] = schedule.parse_date(body.valid_from).isoformat()
        if body.valid_to is not None:
            fields["valid_to"] = schedule.parse_date(body.valid_to).isoformat()
        windows = schedule.clean_windows([w.model_dump() for w in body.windows]) if body.windows is not None else None
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    start = fields.get("valid_from", row["valid_from"])
    end = fields.get("valid_to", row["valid_to"])
    if schedule.parse_date(end) < schedule.parse_date(start):
        return JSONResponse({"error": "the end date is before the start date"}, status_code=400)

    with db.transaction() as conn:
        if fields:
            assignments = ", ".join(f"{k} = ?" for k in fields)
            conn.execute(f"UPDATE keys SET {assignments} WHERE id = ?", (*fields.values(), key_id))
        if windows is not None:
            conn.execute("DELETE FROM windows WHERE key_id = ?", (key_id,))
            conn.executemany(
                "INSERT INTO windows (key_id, weekday, start_min, end_min) VALUES (?,?,?,?)",
                [(key_id, day, s, e) for day, s, e in windows],
            )
    log.info("key %d updated: %s", key_id, ", ".join(fields) or "windows")
    return JSONResponse({"key": _key_json(db.one("SELECT * FROM keys WHERE id = ?", (key_id,)))})


@app.delete("/api/keys/{key_id}")
async def delete_key(request: Request, key_id: int) -> JSONResponse:
    if not logged_in(request):
        return needs_login()
    row = db.one("SELECT * FROM keys WHERE id = ?", (key_id,))
    if not row:
        return JSONResponse({"error": "no such key"}, status_code=404)
    db.execute("DELETE FROM keys WHERE id = ?", (key_id,))
    log.info("key %d deleted: %s", key_id, row["name"])
    # Its events stay: the log of who came in must survive the key.
    return JSONResponse({"ok": True})


@app.get("/api/keys/{key_id}/qr.png")
async def key_png(request: Request, key_id: int) -> Response:
    if not logged_in(request):
        return needs_login()
    row = db.one("SELECT * FROM keys WHERE id = ?", (key_id,))
    if not row:
        return JSONResponse({"error": "no such key"}, status_code=404)
    return Response(codes.qr_png(row["token"]), media_type="image/png",
                    headers={"Cache-Control": "no-store"})


@app.get("/api/keys/{key_id}/card.pdf")
async def key_pdf(request: Request, key_id: int) -> Response:
    if not logged_in(request):
        return needs_login()
    row = db.one("SELECT * FROM keys WHERE id = ?", (key_id,))
    if not row:
        return JSONResponse({"error": "no such key"}, status_code=404)
    key = _key_json(row)
    valid = f"Valid {row['valid_from']} to {row['valid_to']}"
    pdf = codes.card_pdf(row["token"], row["name"], key["schedule_text"], valid)
    safe = "".join(c if c.isalnum() else "-" for c in row["name"])[:40]
    return Response(pdf, media_type="application/pdf", headers={
        "Content-Disposition": f'attachment; filename="gatekey-{safe}.pdf"',
        "Cache-Control": "no-store",
    })


# --------------------------------------------------------------------------- #
# Verification — the one thing intercom-app calls
# --------------------------------------------------------------------------- #

def _record(conn, key_id: int | None, name: str | None, token: str | None,
            outcome: str, reason: str | None, source: str) -> None:
    conn.execute(
        "INSERT INTO events (ts, key_id, name, token, outcome, reason, source) VALUES (?,?,?,?,?,?,?)",
        (schedule.now_local().strftime("%Y-%m-%dT%H:%M:%S%z"), key_id, name, token, outcome, reason, source),
    )


@app.post("/api/verify")
async def verify(request: Request, body: VerifyIn) -> JSONResponse:
    """Is this code allowed to open the gate right now?

    Answers {"valid": bool, "reason": str, "name": str|None}. A valid answer
    counts as a use: this is the authorisation event, whether or not the relay
    then clicks, so a code with one use left cannot be spent twice by retrying.
    """
    if not service_call(request):
        return JSONResponse({"error": "not authorised"}, status_code=401)

    presented = body.code.strip().upper()
    now = schedule.now_local()
    with db.transaction() as conn:
        row = conn.execute("SELECT * FROM keys WHERE token = ?", (presented,)).fetchone()
        if row is None:
            _record(conn, None, None, presented[:32], "refused", "unknown", body.source)
            return JSONResponse({"valid": False, "reason": "unknown", "name": None})

        row = dict(row)
        windows = [dict(w) for w in conn.execute(
            "SELECT weekday, start_min, end_min FROM windows WHERE key_id = ?", (row["id"],)
        ).fetchall()]
        name = row["name"]

        reason = None
        if row["revoked"]:
            reason = "revoked"
        elif now.date() < schedule.parse_date(row["valid_from"]):
            reason = "not yet valid"
        elif now.date() > schedule.parse_date(row["valid_to"]):
            reason = "expired"
        elif not schedule.windows_allow(windows, now):
            reason = "outside its schedule"
        elif row["max_uses"] is not None and row["uses"] >= row["max_uses"]:
            reason = "used up"
        elif row["last_used_at"] and row["cooldown_seconds"]:
            try:
                last = datetime.strptime(row["last_used_at"], "%Y-%m-%dT%H:%M:%S%z")
                if (now - last).total_seconds() < row["cooldown_seconds"]:
                    reason = "cooldown"
            except ValueError:
                pass

        if reason:
            _record(conn, row["id"], name, None, "refused", reason, body.source)
            log.info("verify REFUSED %s (%s) from %s", name, reason, body.source)
            return JSONResponse({"valid": False, "reason": reason, "name": name})

        conn.execute(
            "UPDATE keys SET uses = uses + 1, last_used_at = ? WHERE id = ?",
            (now.strftime("%Y-%m-%dT%H:%M:%S%z"), row["id"]),
        )
        _record(conn, row["id"], name, None, "valid", None, body.source)
        log.info("verify VALID %s from %s (use %d)", name, body.source, row["uses"] + 1)
        remaining = None if row["max_uses"] is None else row["max_uses"] - row["uses"] - 1
        return JSONResponse({"valid": True, "reason": "ok", "name": name,
                             "key_id": row["id"], "uses_left": remaining})


@app.get("/api/events")
async def events(request: Request, limit: int = 100) -> JSONResponse:
    if not logged_in(request):
        return needs_login()
    limit = max(1, min(limit, 1000))
    rows = db.query("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))
    return JSONResponse({"events": rows}, headers={"Cache-Control": "no-store"})
