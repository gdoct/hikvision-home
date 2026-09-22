# intercom

A LAN-only page that shows your Hikvision intercom (plus up to two more
cameras) and opens the gate with one hold-to-open button. No login, no
vendor app, no cloud.

```
browser ──https──▶ reverse proxy ──http :8099──▶ intercom-app ──ws──▶ go2rtc ──rtsp──▶ intercom
                                                      │
                                                      └── ISAPI digest PUT (open door)
```

Install instructions live in [../docs/INSTALL.md](../docs/INSTALL.md); the
reasoning behind the split into two services is in
[../docs/ARCHITECTURE.md](../docs/ARCHITECTURE.md). **Read
[../SECURITY.md](../SECURITY.md) before you expose this anywhere** — it opens
a physical gate and asks nobody who they are.

## Layout

| Path | What |
| --- | --- |
| `docker-compose.yml` | `go2rtc` (host network) + `intercom-app` (bridge, port 8099) |
| `go2rtc.yaml` | stream definitions; credentials come from the environment |
| `Dockerfile`, `requirements.txt` | the app image (Python 3.13, FastAPI) |
| `app/main.py` | API: health, stream status, WS/MJPEG proxy, gate open, gate log |
| `app/qr.py` | QR scanner: MQTT-triggered, verifies codes with gatekeys, opens the gate |
| `app/static/index.html` | the page: MSE player, hold-to-open button, status |
| `tools/probe.py` | asks the device which door endpoint and RTSP path are real |
| `tools/make_test_qr.py` | printable A4 sheet of QR codes at 100/60/40/25 mm |
| `tools/make_icons.py` | regenerates the home-screen icons (stdlib only) |
| `tools/npm-proxy-host.sh` | optional: creates an nginx-proxy-manager host and LAN-only access list |
| `.env.example` | every setting, with comments; copy to `.env` (0600) |

## Quick start

From the repository root, `./build.sh setup intercom` copies `.env.example`
into place; or by hand:

```sh
cp .env.example .env && chmod 600 .env
$EDITOR .env      # INTERCOM_HOST / INTERCOM_USER / INTERCOM_PASSWORD at least
```

Confirm the device's endpoints before trusting the defaults — the door path
and the RTSP path differ across Hikvision families, and this is the one step
that saves an evening of guessing:

```sh
docker compose build
docker compose run --rm intercom-app python tools/probe.py
```

Put the working RTSP path in `RTSP_PATH` and the door id in
`INTERCOM_DOOR_ID`. If the capabilities show no door control at all, the
device is a camera family rather than a door station, and the gate endpoint
needs a different approach.

Then start it:

```sh
docker compose up -d
docker compose logs -f
curl -s http://localhost:8099/api/health
```

## API

| Method | Path | Notes |
| --- | --- | --- |
| `GET` | `/` | the page |
| `GET` | `/api/health` | liveness for the compose healthcheck |
| `GET` | `/api/cameras` | camera list, site name and cooldown; drives the UI |
| `GET` | `/api/stream/status` | `device_reachable`, `go2rtc_up`, `stream_active`, `viewers` |
| `WS` | `/api/stream/ws?src=intercom` | go2rtc MSE websocket, proxied |
| `GET` | `/api/stream/mjpeg?src=intercom` | fallback for browsers without MSE |
| `POST` | `/api/gate/open` | `200 opened`, `429 cooldown / rate_limited`, `503 device_unreachable`, `502 device_error` |
| `GET` | `/api/gate/log?limit=50` | newest attempts first |
| `GET` | `/api/qr/status` | `mqtt_connected`, `scanning`, `persons_at_gate`, `frames_scanned` |
| `GET` | `/api/qr/log?limit=50` | decoded QR codes, newest first |

Every gate attempt is appended as one JSON line to `/data/gate.log` in the
`intercom-data` volume, with timestamp, source IP, `X-Forwarded-For`, user
agent and outcome. With no authentication in front of the page, that log is
the only way to ever answer "who opened the gate at ten past two":

```sh
docker compose exec intercom-app tail -n 50 /data/gate.log
```

## Cameras

Slot 1 is the intercom. Slots 2 and 3 are optional extra cameras: set
`CAMERA2_HOST` (and optionally `CAMERA2_NAME`, `CAMERA2_USER`,
`CAMERA2_PASSWORD`, `CAMERA2_RTSP_PATH`, `CAMERA2_RTSP_PORT`) in `.env`, then
`docker compose up -d`. The login defaults to the intercom's. `CAMERA3_*`
works the same way.

With more than one camera the page shows a grid; tap a cell to fill the
screen, and use Back to return. The gate button stays available in every view.
They do not have to be Hikvision — anything with an RTSP URL works; see
[../docs/CAMERAS.md](../docs/CAMERAS.md) for known paths and for how to go
beyond three.

## QR codes at the gate

Optional. A code held up to the intercom opens the gate if
[`../gatekeys`](../gatekeys) says it is valid at that moment. This app decides
nothing: it decodes, asks, and acts on the answer.

```
frigate ──frigate/events──▶ mosquitto ──▶ intercom-app ──MJPEG──▶ go2rtc
         (person on 'gate')                    │  decode 8 fps
                                               ├──POST /api/verify──▶ gatekeys
                                               └── valid? open the gate (same
                                                   lock and cooldown as the button)
```

It needs [Frigate](https://frigate.video) with an MQTT broker, because that is
what says "someone is standing at the gate". Without them the scanner stays
off and everything else keeps working.

- `GATEKEYS_URL` / `GATEKEYS_TOKEN` in `.env` point at gatekeys; the token
  must match `GATEKEYS_VERIFY_TOKEN` there.
- `QR_ACTION=log` decodes and logs the verdict but never opens — prove it at
  your own gate this way first. `QR_ACTION=open` (the default) opens.
- gatekeys unreachable means no code works; the button is unaffected. It
  never fails open.
- An opening by code is in `/data/gate.log` like any other, with `ip: "qr"`
  and the key's name. The refusal reason lives in `/data/qr.log` and in
  gatekeys' own event log.
- A Frigate `new`/`update` event for a `person` on camera `QR_FRIGATE_CAMERA`
  starts a scan session; `end` for every such person stops it (or
  `QR_STALE_SECONDS` of silence, in case an `end` was missed).
- **No MQTT, no scanning.** There is deliberately no "scan all the time"
  fallback. A lost broker connection ends any running session and it resumes
  on the next person after reconnecting.
- Give the app an MQTT account that can only *read* `frigate/events`, so it
  cannot send Frigate commands.
- Each code is logged once per 30 s (`QR_REPEAT_SECONDS`).
- After the last person leaves, the stream stays open and keeps decoding for
  `QR_LINGER_SECONDS` (90). go2rtc starts an ffmpeg transcode per session and
  the first frame takes ~2.4 s — on a five-second visit that is half the
  session spent blind, so a warm stream is worth the idle CPU.

### How big does a printed code have to be?

Print a test sheet — the same code at four sizes, each labelled, the size
encoded in the text so the log says which tile was read:

```sh
docker run --rm -v "$PWD:/out" intercom-app:latest \
    python tools/make_test_qr.py /out/gate-qr-test.pdf
```

Measured on a 4 MP intercom in daylight, held at arm's length: all four sizes
read, including 25 mm, which covered only **61x54 px** in frame. Take that as
the floor and print real codes at 40 mm or larger — which is what gatekeys'
printable card does.

`QR_DEBUG_FRAMES` keeps the last N frames seen with someone at the gate in
`/data/qr-frames/`, one a second, oldest overwritten. That is how to find out
*why* a code did not read instead of guessing. Those frames are stills of
whoever stands at your gate: switch it on for a debugging session, then set it
back to 0 and delete the directory.

Test the whole path without anyone at the gate (send an `update` rather than a
`new`, so Home Assistant sends no notification):

```sh
docker run --rm --network host eclipse-mosquitto:2 mosquitto_pub \
  -h "$MQTT_HOST" -u "$MQTT_USER" -P "$MQTT_PASSWORD" -t frigate/events \
  -m '{"type":"update","after":{"id":"test","camera":"gate","label":"person"}}'
# ...then the same with "type":"end" to stop it.
```

## Home-screen icon

The site is installable. iPhone: open it in Safari, Share, "Add to Home
Screen". Android: Chrome menu, "Add to Home screen" or "Install app". It then
opens full-screen under `SITE_NAME`. There is deliberately no service worker
and no offline cache: a cached page at a gate would be misleading. Icons are
generated by `tools/make_icons.py` into `app/static/icons/`.

## Behaviour notes

- Hold-to-open (one second) is the confirm gesture; a tap does nothing. A
  mis-tap on a phone should not open the gate to the street.
- The gate's position is never shown, because it cannot be read: the intercom
  pulses a relay that other openers share, and a typical gate controller steps
  open → stop → close on each pulse. A successful press reports "signal sent"
  (`opened` in the API and the log means exactly that); the video is the truth.
- Phone on its side: the active camera goes full screen with the gate button
  and status floating over it; tap the video for real browser fullscreen where
  supported. Swipe sideways to step through cameras.
- Cooldown (`GATE_COOLDOWN_SECONDS`, default 5) and rate limits (6/min per IP,
  20/min global) are enforced server-side; the button mirrors them.
- The cooldown starts on every attempt that reached the device, including
  failures, so an unhappy intercom is not hammered.
- Stream loss shows an overlay within about five seconds instead of a frozen
  frame; the page reconnects with backoff and re-syncs to live.
- go2rtc's API is bound to the compose bridge gateway (`172.30.9.1:1984`), so
  only `intercom-app` can reach it. That API exposes the stream URL with the
  credentials in it, which is why it must not be on the LAN.
