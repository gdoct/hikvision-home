"""
QR scanner — decodes QR codes held up to the intercom and, if gatekeys says
the code is valid right now, opens the gate.

This module decides nothing about access. It reads a code and asks gatekeys
(../gatekeys), which owns the codes, their schedules and the audit trail:

    decode ──POST /api/verify──▶ gatekeys ──{"valid": true}──▶ open the gate

Consequences of that split, on purpose:
  * gatekeys being down means codes stop working — the button does not.
  * this app never learns which codes exist, only the verdict on the one in
    front of the camera.
  * QR_ACTION=log turns opening off while keeping the log, which is how this
    was first proven at the gate.

Scanning is driven by Frigate, over MQTT:

    frigate ──frigate/events──▶ MQTT broker ──▶ this module ──▶ scan go2rtc MJPEG

  * A person on Frigate's gate camera (new/update event) starts a scan
    session; the session runs while at least one such person is tracked and
    stops when Frigate sends 'end' for all of them.
  * No MQTT, no scanning. There is deliberately no "scan all the time"
    fallback: if the broker is down or the credentials are wrong, the scanner
    is simply off. A lost connection also ends any running session, because
    without the event stream there is no way to know when the person left.
  * The gate button does not depend on any of this.

Decoded codes are written as JSON lines to QR_LOG_PATH and to stdout.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import time
from collections import deque
from pathlib import Path
from typing import Any

import aiomqtt
import httpx
import zxingcpp
from PIL import Image

log = logging.getLogger("intercom.qr")

MQTT_HOST = os.environ.get("MQTT_HOST", "").strip()
MQTT_PORT = int(os.environ.get("MQTT_PORT") or 1883)
MQTT_USER = os.environ.get("MQTT_USER", "").strip()
MQTT_PASSWORD = os.environ.get("MQTT_PASSWORD", "")
MQTT_TOPIC = "frigate/events"

# Frigate's name for the intercom camera, and go2rtc's name for its stream.
QR_FRIGATE_CAMERA = os.environ.get("QR_FRIGATE_CAMERA") or "gate"
QR_STREAM = os.environ.get("QR_STREAM") or "intercom"
# Frames decoded per second while a session runs. The MJPEG stream arrives
# faster; frames in between are dropped, never queued.
QR_SCAN_FPS = float(os.environ.get("QR_SCAN_FPS") or 8)
# Keep the MJPEG stream open (and keep decoding) this long after the last
# person leaves. go2rtc starts an ffmpeg transcode per session and the first
# frame takes ~2.4s: on a 5s visit that is half the session spent blind.
# Lingering means a second approach scans from the first frame.
QR_LINGER_SECONDS = float(os.environ.get("QR_LINGER_SECONDS") or 90)
# A tracked person Frigate has said nothing about for this long is presumed
# gone (guards against a missed 'end' keeping the scanner on forever).
QR_STALE_SECONDS = float(os.environ.get("QR_STALE_SECONDS") or 300)
# The same code seen again within this window is not logged again.
QR_REPEAT_SECONDS = float(os.environ.get("QR_REPEAT_SECONDS") or 30)
QR_LOG_PATH = Path(os.environ.get("QR_LOG_PATH") or "/data/qr.log")

# gatekeys: where to verify a code, and the shared secret it expects. Without
# a URL or token, nothing is verified and nothing opens — decoded codes are
# only logged, which is also what QR_ACTION=log does deliberately.
GATEKEYS_URL = (os.environ.get("GATEKEYS_URL") or "").rstrip("/")
GATEKEYS_TOKEN = os.environ.get("GATEKEYS_TOKEN", "")
QR_ACTION = (os.environ.get("QR_ACTION") or "open").strip().lower()
GATEKEYS_TIMEOUT = float(os.environ.get("GATEKEYS_TIMEOUT") or 3)
# Debug aid while proving this works: keep the last QR_DEBUG_FRAMES frames
# seen while someone is at the gate (about one a second), so a failed read can
# be looked at instead of guessed about. 0 disables it.
QR_DEBUG_FRAMES = int(os.environ.get("QR_DEBUG_FRAMES") or 0)
QR_DEBUG_DIR = Path(os.environ.get("QR_DEBUG_DIR") or "/data/qr-frames")

ENABLED = bool(MQTT_HOST and MQTT_USER)


class QrScanner:
    def __init__(self, go2rtc_url: str) -> None:
        self.go2rtc_url = go2rtc_url
        self.mqtt_connected = False
        self.scanning = False
        self.last_error: str | None = None
        self.last_code_at: str | None = None
        # Frames decoded in the current (or last) session, and their size:
        # proof that scanning actually looked at the camera.
        self.frames_scanned = 0
        self.frame_size: str | None = None
        # Set by main.py; called when gatekeys accepts a code.
        self.open_gate = None
        self.last_verdict: str | None = None
        self._debug_seq = 0
        self._debug_last = 0.0
        # Frigate event id -> monotonic time of its last new/update message.
        self._persons: dict[str, float] = {}
        self._wake = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._log_lock = asyncio.Lock()

    # ------------------------------------------------------------------ state

    def _prune(self) -> None:
        now = time.monotonic()
        for eid, seen in list(self._persons.items()):
            if now - seen > QR_STALE_SECONDS:
                log.info("person %s stale after %ds, dropping", eid, QR_STALE_SECONDS)
                del self._persons[eid]

    def wanted(self) -> bool:
        self._prune()
        return self.mqtt_connected and bool(self._persons)

    def status(self) -> dict[str, Any]:
        return {
            "enabled": ENABLED,
            "mqtt_connected": self.mqtt_connected,
            "scanning": self.scanning,
            "persons_at_gate": len(self._persons),
            "frames_scanned": self.frames_scanned,
            "debug_frames": QR_DEBUG_FRAMES,
            "frame_size": self.frame_size,
            "last_code_at": self.last_code_at,
            "last_verdict": self.last_verdict,
            "action": QR_ACTION if (GATEKEYS_URL and GATEKEYS_TOKEN) else "log (gatekeys not configured)",
            "last_error": self.last_error,
        }

    # -------------------------------------------------------------- lifecycle

    def start(self) -> None:
        if not ENABLED:
            log.info("QR scanner disabled: MQTT_HOST / MQTT_USER not set")
            return
        self._tasks = [
            asyncio.create_task(self._mqtt_loop(), name="qr-mqtt"),
            asyncio.create_task(self._scan_loop(), name="qr-scan"),
        ]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    # ------------------------------------------------------------------- MQTT

    async def _mqtt_loop(self) -> None:
        backoff = 2.0
        while True:
            try:
                async with aiomqtt.Client(
                    MQTT_HOST, MQTT_PORT, username=MQTT_USER, password=MQTT_PASSWORD,
                    identifier="intercom-app", keepalive=30,
                ) as client:
                    await client.subscribe(MQTT_TOPIC)
                    self.mqtt_connected = True
                    self.last_error = None
                    backoff = 2.0
                    log.info("MQTT connected to %s:%d, listening on %s", MQTT_HOST, MQTT_PORT, MQTT_TOPIC)
                    async for message in client.messages:
                        self._on_event(message.payload)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # aiomqtt.MqttError, OSError, ...
                self.last_error = f"mqtt: {exc}"
                if self.mqtt_connected:
                    log.warning("MQTT connection lost: %s", exc)
                else:
                    log.warning("MQTT connect failed: %s (retry in %ds)", exc, backoff)
            # No broker, no event stream: nothing may keep a session alive.
            self.mqtt_connected = False
            self._persons.clear()
            self._wake.set()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)

    def _on_event(self, payload: Any) -> None:
        try:
            event = json.loads(payload)
            after = event.get("after") or {}
            kind = event.get("type")
        except (ValueError, TypeError, AttributeError):
            return
        if after.get("camera") != QR_FRIGATE_CAMERA or after.get("label") != "person":
            return
        eid = after.get("id")
        if not eid:
            return
        if kind == "end":
            if self._persons.pop(eid, None) is not None:
                log.info("person %s left the gate (%d still there)", eid, len(self._persons))
        elif kind in ("new", "update"):
            if eid not in self._persons:
                log.info("person %s at the gate", eid)
            self._persons[eid] = time.monotonic()
        self._wake.set()

    # ------------------------------------------------------------------- scan

    async def _scan_loop(self) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            if not self.wanted():
                continue
            self.scanning = True
            log.info("QR scan session started")
            try:
                await self._session()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = f"scan: {exc}"
                log.warning("QR scan session failed: %s", exc)
                await asyncio.sleep(2)
                self._wake.set()  # retry if still wanted
            finally:
                self.scanning = False
                log.info("QR scan session stopped")

    async def _session(self) -> None:
        """Read go2rtc's MJPEG stream, decode the newest frame at QR_SCAN_FPS,
        until no one is at the gate any more."""
        interval = 1.0 / QR_SCAN_FPS
        recent: dict[str, float] = {}
        last_wanted = time.monotonic()
        latest: list[bytes | None] = [None]
        done = asyncio.Event()

        async def reader() -> None:
            timeout = httpx.Timeout(10.0, read=15.0)
            async with httpx.AsyncClient(timeout=timeout) as client:
                async with client.stream(
                    "GET", f"{self.go2rtc_url}/api/stream.mjpeg", params={"src": QR_STREAM}
                ) as r:
                    r.raise_for_status()
                    buf = bytearray()
                    async for chunk in r.aiter_raw():
                        buf += chunk
                        # Keep only the newest complete JPEG. Baseline JPEG
                        # byte-stuffs 0xFF in entropy data, so FFD9 is EOI.
                        end = buf.rfind(b"\xff\xd9")
                        if end != -1:
                            start = buf.rfind(b"\xff\xd8", 0, end)
                            if start != -1:
                                latest[0] = bytes(buf[start:end + 2])
                            del buf[:end + 2]
                        elif len(buf) > 8_000_000:
                            buf.clear()
                        if done.is_set():
                            return

        self.frames_scanned = 0
        read_task = asyncio.create_task(reader())
        try:
            while True:
                now = time.monotonic()
                if self.wanted():
                    last_wanted = now
                    self.scanning = True
                elif not self.mqtt_connected or now - last_wanted > QR_LINGER_SECONDS:
                    # MQTT gone, or nobody has been at the gate for a while.
                    break
                else:
                    self.scanning = False  # lingering: stream warm, still decoding
                if read_task.done():
                    read_task.result()  # surface the error
                    raise RuntimeError("MJPEG stream ended")
                frame, latest[0] = latest[0], None
                if frame is not None:
                    size, codes = await asyncio.to_thread(_decode, frame)
                    self.frames_scanned += 1
                    self.frame_size = size
                    if QR_DEBUG_FRAMES and self._persons and now - self._debug_last >= 1.0:
                        self._debug_last = now
                        await asyncio.to_thread(self._save_debug_frame, frame)
                    for text, fmt in codes:
                        seen = time.monotonic()
                        if seen - recent.get(text, -1e9) >= QR_REPEAT_SECONDS:
                            await self._handle_code(text, fmt)
                        recent[text] = seen
                await asyncio.sleep(interval)
        finally:
            done.set()
            read_task.cancel()
            await asyncio.gather(read_task, return_exceptions=True)

    def _save_debug_frame(self, jpeg: bytes) -> None:
        """Keep the last QR_DEBUG_FRAMES frames, oldest overwritten. Runs in a
        worker thread. Best effort: a full disk must not stop scanning."""
        try:
            QR_DEBUG_DIR.mkdir(parents=True, exist_ok=True)
            name = QR_DEBUG_DIR / f"{self._debug_seq % QR_DEBUG_FRAMES:04d}.jpg"
            name.write_bytes(jpeg)
            (QR_DEBUG_DIR / "latest.txt").write_text(
                f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {name.name}\n"
            )
            self._debug_seq += 1
        except OSError as exc:
            log.warning("could not save debug frame: %s", exc)

    async def _handle_code(self, text: str, fmt: str) -> None:
        """Ask gatekeys about one decoded code, and act on the answer."""
        verdict: dict[str, Any] = {"valid": False, "reason": "not checked"}
        action = "none"
        if GATEKEYS_URL and GATEKEYS_TOKEN:
            try:
                async with httpx.AsyncClient(timeout=GATEKEYS_TIMEOUT) as client:
                    r = await client.post(
                        f"{GATEKEYS_URL}/api/verify",
                        json={"code": text, "source": f"intercom/{QR_FRIGATE_CAMERA}"},
                        headers={"X-Gatekeys-Token": GATEKEYS_TOKEN},
                    )
                    r.raise_for_status()
                    verdict = r.json()
            except (httpx.HTTPError, ValueError) as exc:
                # gatekeys down: the code does not open anything. Say so in the
                # log rather than failing open.
                verdict = {"valid": False, "reason": f"gatekeys unreachable: {exc.__class__.__name__}"}
                log.warning("gatekeys verify failed: %s", exc)

        if verdict.get("valid"):
            if QR_ACTION == "open" and self.open_gate is not None:
                action = await self.open_gate(verdict.get("name") or "?", text)
            else:
                action = "not opened (QR_ACTION=log)"

        self.last_verdict = f"{verdict.get('name') or text}: " \
                            f"{'valid' if verdict.get('valid') else verdict.get('reason')}"
        await self._log_code(text, fmt, verdict, action)

    async def _log_code(self, text: str, fmt: str, verdict: dict[str, Any], action: str) -> None:
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "camera": QR_FRIGATE_CAMERA,
            "format": fmt,
            "text": text,
            "frigate_events": sorted(self._persons),
            "valid": bool(verdict.get("valid")),
            "key_name": verdict.get("name"),
            "reason": verdict.get("reason"),
            "action": action,
        }
        self.last_code_at = entry["ts"]
        line = json.dumps(entry, ensure_ascii=False)
        log.info("qr %s", line)
        async with self._log_lock:
            try:
                QR_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
                with QR_LOG_PATH.open("a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError as exc:
                log.error("could not write QR log %s: %s", QR_LOG_PATH, exc)

    def read_log(self, limit: int) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        try:
            with QR_LOG_PATH.open("r", encoding="utf-8") as f:
                for line in deque(f, maxlen=limit):
                    try:
                        entries.append(json.loads(line))
                    except ValueError:
                        continue
        except FileNotFoundError:
            pass
        entries.reverse()
        return entries


def _decode(jpeg: bytes) -> tuple[str, list[tuple[str, str]]]:
    """Decode every QR code in one JPEG frame. Runs in a worker thread.
    Returns ("WxH", [(text, format), ...])."""
    img = Image.open(io.BytesIO(jpeg)).convert("L")
    results = zxingcpp.read_barcodes(img, formats=zxingcpp.BarcodeFormat.QRCode)
    return f"{img.width}x{img.height}", [(r.text, r.format.name) for r in results if r.valid and r.text]
