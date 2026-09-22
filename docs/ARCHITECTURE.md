# Architecture

Why this is shaped the way it is. Read this before changing anything
structural; most of what looks like an odd choice below is load-bearing.

## The whole picture

```
                     phone / laptop on the LAN
                               │
                               │ https, LAN-only access list
                               ▼
                     reverse proxy (TLS)
                      │                   │
       http :8099     │                   │  http :8097
                      ▼                   ▼
          ┌───────────────────┐   ┌──────────────────┐
          │   intercom-app    │   │     gatekeys     │
          │                   │   │                  │
          │  page + API       │   │  GUI + API       │
          │  stream proxy     │   │  SQLite          │
          │  gate endpoint    │   │                  │
          │  QR scanner       │   └──────────────────┘
          └───────────────────┘            ▲
            │        │      │              │
            │        │      └── POST /api/verify (shared secret)
            │        │
   MSE/WS   │        │ ISAPI digest PUT
   MJPEG    │        │ (open door)
            ▼        ▼
       ┌─────────┐  ┌──────────────────────────────┐
       │ go2rtc  │─▶│  intercom  ·  camera2  ·  …  │
       └─────────┘  └──────────────────────────────┘
              rtsp
```

Four processes in three containers, plus whatever proxy you already run.

## Why two apps and not one

`intercom-app` views cameras and pulses a gate relay. `gatekeys` decides who
may do that with a code. They are separate because **the rules change far more
often than the mechanism**: a new cleaner, a window moved by fifteen minutes, a
code revoked in a hurry. Every one of those edits is a restart of the app that
holds them — and none of them should be able to stop the button in the hall
from working.

Consequences, all intended:

- The gate button keeps working while `gatekeys` is restarted, edited or
  broken.
- `gatekeys` never holds the intercom's credentials, so the page that is
  exposed to a password login is not the page that can open your gate
  directly.
- `intercom-app` never learns which codes exist — only the verdict on the one
  in front of the camera.
- If `gatekeys` is unreachable, codes stop working. It never fails open.

The entire contract between them is one call:

```
intercom-app ──POST /api/verify, X-Gatekeys-Token──▶ gatekeys
             ◀── {"valid": true, "name": "Cleaner"} ──
```

## Why go2rtc

Browsers cannot play RTSP. Something has to pull the camera's stream and
repackage it, and go2rtc does that with the least fuss — it also gives us an
MJPEG rendition of the same stream, which is what the QR scanner decodes and
what old iOS falls back to.

It runs on the host network so WebRTC can be added later without re-plumbing.
Its API is bound to the **gateway address of the app's private bridge network**
(`172.30.9.1:1984` by default), which is the single most important line in the
compose file: that API hands out stream URLs *with the RTSP credentials in
them*, so it must be reachable from `intercom-app` and from nothing else.

The browser never talks to go2rtc directly either. `intercom-app` proxies the
WebSocket, so everything is same-origin behind your reverse proxy and there is
one door to guard rather than two.

## Video path

MSE over WebSocket, proxied. Latency lands around one to two seconds, which is
fine for recognising a face at a gate.

WebRTC would get under a second but needs UDP reachable from the browser and
correct ICE candidates behind a reverse proxy — fiddly, and the gate button
does not need it. MJPEG is the fallback for browsers without Media Source
Extensions: heavier, lower quality, automatic.

## The gate endpoint

```
PUT /ISAPI/AccessControl/RemoteControl/door/<id>
Content-Type: application/xml
Authorization: Digest ...

<RemoteControlDoor><cmd>open</cmd></RemoteControlDoor>
```

Around that one request:

- **One lock, one cooldown, shared by the button and the QR scanner.** A
  person pressing the button while showing a code must not pulse the relay
  twice.
- **The cooldown starts on every attempt that reached the device**, including
  failures, so an unhappy intercom is not hammered.
- **Rate limits** per source IP and global, in a sliding window, server-side.
  The button mirrors them; it does not enforce them.
- **`opened` means the device accepted the command and pulsed its relay** —
  nothing more. A gate controller typically steps open → stop → close on each
  pulse, and other openers share that relay, so the gate's position cannot be
  known from here. The page never claims one; the video is the truth.
- **Every attempt is appended as one JSON line** to `/data/gate.log`, mirrored
  to stdout. With no authentication in front of the page, that file is the
  only way to answer "who opened the gate at ten past two".

## The QR path

```
frigate ──frigate/events──▶ mosquitto ──▶ intercom-app ──MJPEG──▶ go2rtc
         (person on 'gate')                    │  decode 8 fps
                                               ├──POST /api/verify──▶ gatekeys
                                               └── valid? open the gate
```

- **Frigate drives it.** A `new`/`update` event for a `person` on the gate
  camera starts a session; `end` for all of them stops it. No broker, no
  scanning, and no fallback — a lost connection ends the session, because
  without the event stream there is no way to know when the person left.
- **Only the newest frame is decoded.** Frames arriving faster than
  `QR_SCAN_FPS` are dropped, never queued: a queue at a gate means decoding
  what someone held up ten seconds ago.
- **Decoding runs in a worker thread**, so the event loop keeps serving the
  page and the button while it works. One 1280x720 frame costs about 10 ms.
- **The stream lingers** for `QR_LINGER_SECONDS` after the last person leaves.
  go2rtc starts an ffmpeg transcode per session and the first frame takes
  ~2.4 s; on a five-second visit that is half the session spent blind. A warm
  stream is worth the idle CPU.

## Data

| Where | What | Why it is there |
| --- | --- | --- |
| `intercom_intercom-data:/data/gate.log` | every gate attempt, JSON lines | the only audit trail an unauthenticated page can have |
| `intercom_intercom-data:/data/qr.log` | every decoded code and its verdict | why a code did not work |
| `gatekeys_gatekeys-data:/data/gatekeys.db` | keys, windows, events | SQLite, one file, no ORM |

`gatekeys` stores codes in **plain text**, because the GUI has to re-print a
card months later. That makes the file as sensitive as a drawer of spare keys,
which is why the GUI has a password and the volume is worth backing up
carefully.

Verification runs in a transaction: a valid verdict increments the use count
in the same lock that read it, so two codes presented at once cannot both slip
past a `max_uses` of 1. A valid answer **counts as a use** whether or not the
relay then clicks — it is the authorisation event, and a code with one use
left must not be spendable twice by retrying.

## Schedules

A key carries a date range (inclusive both ends) and weekly windows: weekday
plus start and end on a 15-minute grid, end exclusive. Everything is evaluated
in local time (`TZ`), because that is the clock the person holding the code
reads. `zoneinfo` handles DST, so a window keeps its wall-clock times across
the switch — "mornings" does not shift by an hour in October.

Windows may not cross midnight. The GUI refuses it and asks for one window on
each day instead, which is also what the person holding the card can read.

## Stack, and what it costs

Python 3.13, FastAPI, SQLite, and a single static HTML file per app with no
build step and no framework. Two `pip install`s and a `docker compose up` is
the whole toolchain.

That is a deliberate trade. There is no type-checked frontend, no component
library, no hot reload — and equally no npm tree to audit, no build to break
in two years, and nothing between you and the page when you want to change a
colour at eleven at night. Both `index.html` files are readable top to bottom.

If you are about to add a build step, open an issue first.

## Camera slots

Slot 1 is the intercom: always present, the only one with a gate. Slots 2 and
up are optional and driven by `CAMERA<n>_HOST`. A slot with no host does not
appear anywhere — the app never asks go2rtc for it, so go2rtc never connects.

The count is dynamic, with `MAX_CAMERAS` (default 3) as the ceiling. Three
places have to agree on the slots that exist, and each is marked `camera
slots`: the loop in `intercom/app/main.py`, the stream blocks in
`go2rtc.yaml`, and the environment lines in `docker-compose.yml`. The page's
grid is laid out for up to three. See [CAMERAS.md](CAMERAS.md) for how to go
further.

## Failure behaviour, in one table

| What breaks | What still works |
| --- | --- |
| `gatekeys` down | the button, the video, the log. Codes are refused |
| MQTT broker down | everything except QR scanning |
| go2rtc down | the button and the log; the page says the stream is down |
| the intercom unreachable | the page and the log; the button reports it plainly |
| the database unwritable | nothing in `gatekeys` — it refuses rather than guesses |

The rule behind that table: **anything that cannot be verified is refused, and
the button is the last thing to lose.**
