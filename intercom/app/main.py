"""
intercom — LAN-only page that shows the gate intercom and opens the gate.

See ../docs/ARCHITECTURE.md. This is the only component that holds the
intercom's credentials: the browser talks to this app, and only this app talks
to the intercom (ISAPI) and to go2rtc (the stream).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import deque
from contextlib import asynccontextmanager
from html import escape as html_escape
from pathlib import Path
from typing import Any

import httpx
import websockets
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .qr import QrScanner

log = logging.getLogger("intercom")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


# --------------------------------------------------------------------------- #
# Configuration (from the environment; see .env.example)
# --------------------------------------------------------------------------- #

def _env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None or value == "":
        raise RuntimeError(f"{name} is not set")
    return value


INTERCOM_HOST = _env("INTERCOM_HOST")
INTERCOM_USER = _env("INTERCOM_USER")
INTERCOM_PASSWORD = _env("INTERCOM_PASSWORD")
INTERCOM_DOOR_ID = _env("INTERCOM_DOOR_ID", "1")
GATE_COOLDOWN_SECONDS = float(_env("GATE_COOLDOWN_SECONDS", "5"))
GO2RTC_URL = _env("GO2RTC_URL", "http://172.30.9.1:1984").rstrip("/")
GO2RTC_WS_URL = GO2RTC_URL.replace("http://", "ws://", 1).replace("https://", "wss://", 1)
GATE_LOG_PATH = Path(_env("GATE_LOG_PATH", "/data/gate.log"))
# Name shown in the browser tab, on the home screen and in the manifest.
SITE_NAME = os.environ.get("SITE_NAME") or "Gate"

# Rate limits, server-side. Cooldown already stops double taps; these stop
# someone (or something) hammering the device.
RATE_LIMIT_PER_IP = (6, 60.0)     # 6 attempts per minute per source IP
RATE_LIMIT_GLOBAL = (20, 60.0)    # 20 attempts per minute in total

# Cameras this app knows about. Slot 1 is the intercom: always present, and
# the only one with a gate. Slots 2..MAX_CAMERAS are optional and come from
# CAMERA<n>_HOST in .env — a slot with no host is simply not there, so the
# page shows one, two or three cells without any other change.
#
# Raising MAX_CAMERAS needs three matching edits, all marked "camera slots":
# here, a stream block in go2rtc.yaml and the CAMERA<n>_* lines in
# docker-compose.yml. The grid in app/static/index.html lays out up to three.
MAX_CAMERAS = int(os.environ.get("MAX_CAMERAS") or 3)

CAMERAS: list[dict[str, Any]] = [
    {"id": "intercom", "name": os.environ.get("INTERCOM_NAME") or "Gate intercom", "gate": True, "host": INTERCOM_HOST},
]
for _n in range(2, MAX_CAMERAS + 1):
    _host = (os.environ.get(f"CAMERA{_n}_HOST") or "").strip()
    if _host:
        CAMERAS.append({
            "id": f"camera{_n}",
            "name": os.environ.get(f"CAMERA{_n}_NAME") or f"Camera {_n}",
            "gate": False,
            "host": _host,
        })
log.info("%d camera(s) configured: %s", len(CAMERAS), ", ".join(c["name"] for c in CAMERAS))
CAMERA_BY_ID = {c["id"]: c for c in CAMERAS}
STREAM_IDS = set(CAMERA_BY_ID)

# QR scanner: scans while Frigate reports a person at the gate, has gatekeys
# check what it decodes, and opens the gate on a valid code. See app/qr.py.
qr_scanner = QrScanner(GO2RTC_URL)


@asynccontextmanager
async def lifespan(_: FastAPI):
    qr_scanner.start()
    yield
    await qr_scanner.stop()


app = FastAPI(title="intercom", docs_url=None, redoc_url=None, lifespan=lifespan)
STATIC_DIR = Path(__file__).parent / "static"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def client_ip(request: Request) -> tuple[str, str | None]:
    """Return (effective ip, raw forwarded-for header).

    NPM sets X-Forwarded-For / X-Real-IP. uvicorn is started with
    --proxy-headers so request.client already reflects X-Forwarded-For when
    present; the raw header is logged too so nothing is lost if that changes.
    """
    peer = request.client.host if request.client else "?"
    forwarded = request.headers.get("x-forwarded-for")
    real = request.headers.get("x-real-ip")
    return (real or peer), forwarded


class SlidingWindow:
    """Tiny sliding-window rate limiter keyed by string."""

    def __init__(self, limit: int, window: float) -> None:
        self.limit = limit
        self.window = window
        self._hits: dict[str, deque[float]] = {}

    def check(self, key: str, now: float) -> float | None:
        """Return None if allowed (and record the hit), else seconds to wait."""
        q = self._hits.setdefault(key, deque())
        while q and now - q[0] > self.window:
            q.popleft()
        if len(q) >= self.limit:
            return self.window - (now - q[0])
        q.append(now)
        return None


per_ip_limiter = SlidingWindow(*RATE_LIMIT_PER_IP)
global_limiter = SlidingWindow(*RATE_LIMIT_GLOBAL)
gate_lock = asyncio.Lock()
last_open_at: float = 0.0
log_lock = asyncio.Lock()


async def append_gate_log(entry: dict[str, Any]) -> None:
    """Append one JSON line to the durable gate log, and mirror it to stdout."""
    line = json.dumps(entry, ensure_ascii=False)
    log.info("gate %s", line)
    async with log_lock:
        try:
            GATE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with GATE_LOG_PATH.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError as exc:  # never let logging failure hide the outcome
            log.error("could not write gate log %s: %s", GATE_LOG_PATH, exc)


# --------------------------------------------------------------------------- #
# Pages and health
# --------------------------------------------------------------------------- #

@app.get("/")
async def index() -> HTMLResponse:
    """The page, with SITE_NAME substituted into the title.

    The name has to be in the HTML rather than set by script: iOS reads
    <title> and apple-mobile-web-app-title when the page is added to the home
    screen, before any of our JavaScript has had a say.
    """
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(
        html.replace("__SITE_NAME__", html_escape(SITE_NAME)),
        headers={"Cache-Control": "no-store"},
    )


@app.get("/manifest.webmanifest")
async def manifest() -> JSONResponse:
    """Web app manifest: lets phones add the site to the home screen.

    Built here rather than served as a file so SITE_NAME reaches it. Sent
    no-store: browsers cache the manifest apart from the page, so without
    that a rename still installs under the old name.
    """
    template = json.loads((STATIC_DIR / "manifest.webmanifest").read_text(encoding="utf-8"))
    template["name"] = SITE_NAME
    template["short_name"] = SITE_NAME[:12]
    return JSONResponse(
        template,
        media_type="application/manifest+json",
        headers={"Cache-Control": "no-store"},
    )


app.mount("/icons", StaticFiles(directory=STATIC_DIR / "icons"), name="icons")


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {"ok": True}


@app.get("/api/cameras")
async def cameras() -> dict[str, Any]:
    # The host addresses stay server-side; the page only needs ids and names.
    public = [{k: v for k, v in c.items() if k != "host"} for c in CAMERAS]
    return {"cameras": public, "cooldown_seconds": GATE_COOLDOWN_SECONDS, "site_name": SITE_NAME}


# --------------------------------------------------------------------------- #
# Stream: status, MSE-over-WebSocket proxy, MJPEG fallback
# --------------------------------------------------------------------------- #

async def tcp_reachable(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
        return True
    except (OSError, asyncio.TimeoutError):
        return False


@app.get("/api/stream/status")
async def stream_status(src: str = "intercom") -> JSONResponse:
    if src not in STREAM_IDS:
        return JSONResponse({"error": "unknown stream"}, status_code=404)

    reachable_task = asyncio.create_task(tcp_reachable(CAMERA_BY_ID[src]["host"], 554))
    go2rtc_up = False
    stream_active = False
    consumers = 0
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(f"{GO2RTC_URL}/api/streams")
            r.raise_for_status()
            go2rtc_up = True
            info = (r.json() or {}).get(src) or {}
            producers = info.get("producers") or []
            consumers = len(info.get("consumers") or [])
            # go2rtc reports bytes_recv per producer once it is actually
            # pulling from the device (idle producers list only their url).
            # Never return the producer itself — its url carries the credentials.
            stream_active = any((p or {}).get("bytes_recv") for p in producers)
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("go2rtc status failed: %s", exc)

    return JSONResponse({
        "src": src,
        "gate": CAMERA_BY_ID[src]["gate"],
        "device_reachable": await reachable_task,
        "go2rtc_up": go2rtc_up,
        "stream_active": stream_active,
        "viewers": consumers,
    }, headers={"Cache-Control": "no-store"})


@app.websocket("/api/stream/ws")
async def stream_ws(ws: WebSocket) -> None:
    """Bidirectional proxy to go2rtc's WebSocket API, so the browser stays
    same-origin behind NPM and go2rtc is never exposed directly."""
    src = ws.query_params.get("src", "intercom")
    if src not in STREAM_IDS:
        await ws.close(code=4404)
        return
    await ws.accept()

    upstream_url = f"{GO2RTC_WS_URL}/api/ws?src={src}"
    try:
        async with websockets.connect(upstream_url, max_size=None, open_timeout=5) as up:

            async def client_to_upstream() -> None:
                while True:
                    msg = await ws.receive()
                    if msg["type"] == "websocket.disconnect":
                        return
                    if msg.get("text") is not None:
                        await up.send(msg["text"])
                    elif msg.get("bytes") is not None:
                        await up.send(msg["bytes"])

            async def upstream_to_client() -> None:
                async for frame in up:
                    if isinstance(frame, (bytes, bytearray)):
                        await ws.send_bytes(bytes(frame))
                    else:
                        await ws.send_text(frame)

            tasks = [
                asyncio.create_task(client_to_upstream()),
                asyncio.create_task(upstream_to_client()),
            ]
            try:
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for t in pending:
                    t.cancel()
                for t in done:
                    exc = t.exception()
                    if exc and not isinstance(exc, (websockets.ConnectionClosed,)):
                        log.info("stream ws ended: %r", exc)
            finally:
                for t in tasks:
                    if not t.done():
                        t.cancel()
    except (OSError, websockets.WebSocketException, asyncio.TimeoutError) as exc:
        log.warning("go2rtc websocket unavailable: %s", exc)
        try:
            await ws.send_text(json.dumps({"type": "error", "value": "stream service unavailable"}))
        except Exception:
            pass
    finally:
        try:
            await ws.close()
        except Exception:
            pass


@app.get("/api/stream/mjpeg")
async def stream_mjpeg(src: str = "intercom") -> StreamingResponse:
    """Fallback for browsers without Media Source Extensions (older iOS)."""
    if src not in STREAM_IDS:
        return JSONResponse({"error": "unknown stream"}, status_code=404)  # type: ignore[return-value]

    client = httpx.AsyncClient(timeout=httpx.Timeout(10.0, read=None))
    req = client.build_request("GET", f"{GO2RTC_URL}/api/stream.mjpeg", params={"src": src})
    try:
        upstream = await client.send(req, stream=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        return JSONResponse({"error": f"stream unavailable: {exc}"}, status_code=503)  # type: ignore[return-value]

    async def body():
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    content_type = upstream.headers.get("content-type", "multipart/x-mixed-replace")
    return StreamingResponse(body(), media_type=content_type, headers={"Cache-Control": "no-store"})


# --------------------------------------------------------------------------- #
# Gate
# --------------------------------------------------------------------------- #

ISAPI_DOOR_URL = f"http://{INTERCOM_HOST}/ISAPI/AccessControl/RemoteControl/door/{INTERCOM_DOOR_ID}"
ISAPI_DOOR_BODY = "<RemoteControlDoor><cmd>open</cmd></RemoteControlDoor>"


async def isapi_open_door() -> tuple[str, str | None, int | None]:
    """Ask the intercom to open the door.

    Returns (outcome, detail, http_status) where outcome is one of
    'opened', 'device_unreachable', 'device_error'.

    'opened' means the intercom accepted the command and pulsed its relay —
    nothing more. The gate controller steps open → stop → close on each
    pulse and other openers share that relay, so the gate's position cannot
    be known from here; the page must not claim one.
    """
    timeout = httpx.Timeout(6.0, connect=3.0)
    try:
        async with httpx.AsyncClient(
            auth=httpx.DigestAuth(INTERCOM_USER, INTERCOM_PASSWORD), timeout=timeout
        ) as client:
            r = await client.put(
                ISAPI_DOOR_URL, content=ISAPI_DOOR_BODY, headers={"Content-Type": "application/xml"}
            )
    except (httpx.ConnectError, httpx.ConnectTimeout):
        return "device_unreachable", "intercom did not answer", None
    except httpx.HTTPError as exc:
        return "device_error", f"request failed: {exc.__class__.__name__}", None

    text = r.text or ""
    if r.status_code == 200 and ("<statusCode>1</statusCode>" in text or "OK" in text):
        return "opened", None, r.status_code
    if r.status_code == 401:
        return "device_error", "intercom rejected the credentials", r.status_code
    if r.status_code == 404:
        return "device_error", "door endpoint not found on this device", r.status_code
    snippet = " ".join(text.split())[:160]
    return "device_error", f"intercom answered {r.status_code}: {snippet or 'no body'}", r.status_code


async def open_gate_for_qr(key_name: str, code: str) -> str:
    """Open the gate because gatekeys accepted a code held up to the camera.

    Shares the lock and the cooldown with the button on purpose: a person at
    the gate pressing and showing a code must not pulse the relay twice. The
    per-IP rate limit does not apply (there is no IP), the global one does.
    """
    global last_open_at

    entry: dict[str, Any] = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "ip": "qr",
        "forwarded_for": None,
        "user_agent": f"qr code {code}",
        "key_name": key_name,
    }
    async with gate_lock:
        now = time.monotonic()
        remaining = GATE_COOLDOWN_SECONDS - (now - last_open_at)
        if last_open_at and remaining > 0:
            entry.update(outcome="refused", reason="cooldown", retry_after=round(remaining, 1))
            await append_gate_log(entry)
            return "cooldown"
        wait = global_limiter.check("*", now)
        if wait is not None:
            entry.update(outcome="refused", reason="rate_limited", retry_after=round(wait, 1))
            await append_gate_log(entry)
            return "rate_limited"
        started = time.monotonic()
        outcome, detail, http_status = await isapi_open_door()
        last_open_at = time.monotonic()

    entry.update(outcome=outcome, detail=detail, device_http_status=http_status,
                 latency_ms=int((time.monotonic() - started) * 1000))
    await append_gate_log(entry)
    return outcome


qr_scanner.open_gate = open_gate_for_qr


@app.post("/api/gate/open")
async def gate_open(request: Request) -> JSONResponse:
    global last_open_at

    ip, forwarded = client_ip(request)
    user_agent = request.headers.get("user-agent", "")
    now = time.monotonic()
    entry: dict[str, Any] = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "ip": ip,
        "forwarded_for": forwarded,
        "user_agent": user_agent[:200],
    }

    def refuse(reason: str, retry_after: float, status: int = 429) -> JSONResponse:
        entry.update(outcome="refused", reason=reason, retry_after=round(retry_after, 1))
        asyncio.create_task(append_gate_log(entry))
        return JSONResponse(
            {"ok": False, "outcome": "refused", "reason": reason, "retry_after": round(retry_after, 1)},
            status_code=status,
            headers={"Retry-After": str(max(1, int(retry_after + 0.999)))},
        )

    # Serialise: one press at a time, cooldown checked under the lock.
    async with gate_lock:
        remaining = GATE_COOLDOWN_SECONDS - (now - last_open_at)
        if last_open_at and remaining > 0:
            return refuse("cooldown", remaining)
        wait = per_ip_limiter.check(ip, now)
        if wait is not None:
            return refuse("rate_limited", wait)
        wait = global_limiter.check("*", now)
        if wait is not None:
            return refuse("rate_limited", wait)

        started = time.monotonic()
        outcome, detail, http_status = await isapi_open_door()
        latency_ms = int((time.monotonic() - started) * 1000)
        # Cooldown applies to every attempt that reached the device, so a
        # failing device is not hammered either.
        last_open_at = time.monotonic()

    entry.update(outcome=outcome, detail=detail, device_http_status=http_status, latency_ms=latency_ms)
    await append_gate_log(entry)

    if outcome == "opened":
        return JSONResponse({"ok": True, "outcome": "opened", "cooldown": GATE_COOLDOWN_SECONDS})
    status = 503 if outcome == "device_unreachable" else 502
    return JSONResponse(
        {"ok": False, "outcome": "failed", "reason": outcome, "detail": detail, "cooldown": GATE_COOLDOWN_SECONDS},
        status_code=status,
    )


@app.get("/api/gate/log")
async def gate_log(limit: int = 50) -> JSONResponse:
    """Most recent gate attempts, newest first. Answers 'who opened the gate
    at ten past two' without shelling into the box."""
    limit = max(1, min(limit, 500))
    entries: list[dict[str, Any]] = []
    try:
        with GATE_LOG_PATH.open("r", encoding="utf-8") as f:
            lines = deque(f, maxlen=limit)
        for line in lines:
            try:
                entries.append(json.loads(line))
            except ValueError:
                continue
    except FileNotFoundError:
        pass
    entries.reverse()
    return JSONResponse({"entries": entries}, headers={"Cache-Control": "no-store"})


# --------------------------------------------------------------------------- #
# QR scanner (status and log; gatekeys owns the codes)
# --------------------------------------------------------------------------- #

@app.get("/api/qr/status")
async def qr_status() -> JSONResponse:
    return JSONResponse(qr_scanner.status(), headers={"Cache-Control": "no-store"})


@app.get("/api/qr/log")
async def qr_log(limit: int = 50) -> JSONResponse:
    """Most recent decoded QR codes, newest first."""
    limit = max(1, min(limit, 500))
    return JSONResponse({"entries": qr_scanner.read_log(limit)}, headers={"Cache-Control": "no-store"})
