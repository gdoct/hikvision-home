# Installation

From a bare Docker host to a working gate button. Budget an hour the first
time, most of it spent finding out what your particular Hikvision device
calls things.

- [1. Before you start](#1-before-you-start)
- [2. Get the code](#2-get-the-code)
- [3. Ask the device what it supports](#3-ask-the-device-what-it-supports)
- [4. Configure the intercom app](#4-configure-the-intercom-app)
- [5. Start it](#5-start-it)
- [6. Put a reverse proxy in front](#6-put-a-reverse-proxy-in-front)
- [7. Add the key app](#7-add-the-key-app-optional)
- [8. Turn on the QR scanner](#8-turn-on-the-qr-scanner-optional)
- [9. Updating, backups, uninstalling](#9-updating-backups-uninstalling)
- [10. When it does not work](#10-when-it-does-not-work)

---

## 1. Before you start

You need:

- **A host** that can reach the intercom over IP and can run Docker: a NAS, a
  mini PC, a Raspberry Pi 4 or better. Roughly 1 GB of RAM and a few hundred
  MB of disk. An x86-64 or arm64 CPU (both images are plain Python).
- **Docker Engine with the Compose plugin.** `docker compose version` must
  work; the old `docker-compose` v1 script will not do.
  ```sh
  curl -fsSL https://get.docker.com | sh
  sudo usermod -aG docker "$USER"   # log out and back in
  ```
- **The intercom's address and an account on it.** Create a dedicated ISAPI
  account with only door-unlock and live-view rights, in the device's own web
  interface, rather than handing over the admin password. Make sure RTSP is
  enabled for it.
- **Its address must not change.** Give it a DHCP reservation or a static
  address.

Optional, for QR codes at the gate: [Frigate](https://frigate.video) watching
the intercom's camera and publishing to an MQTT broker. You can install
everything else first and add this later.

---

## 2. Get the code

```sh
git clone https://github.com/gdoct/hikvision-home.git
cd hikvision-home
./build.sh setup
```

`setup` copies both `.env.example` files into place at mode 0600, generates
the two random secrets `gatekeys` needs, and copies the shared verify token
into the intercom app's `.env` so the two agree. It never overwrites a file
that already exists.

---

## 3. Ask the device what it supports

Do not trust the defaults. Hikvision families differ, and the two settings
that differ most are the two you need. Fill in the address and account first:

```sh
$EDITOR intercom/.env     # INTERCOM_HOST, INTERCOM_USER, INTERCOM_PASSWORD
./build.sh probe
```

The probe reads `/ISAPI/System/deviceInfo`, looks through
`/ISAPI/System/capabilities` for anything door-related, asks for the door
control capabilities, and then tries an RTSP `DESCRIBE` against six common
stream paths. It never opens the gate.

You are looking for two things:

- **A stream path that answers `OK`.** Put it in `RTSP_PATH`.
  `/Streaming/Channels/101` is the main stream and `/102` the sub stream; the
  sub stream is lighter and often the better choice for phones.
- **The door id.** Usually `1`. Put it in `INTERCOM_DOOR_ID`.

If every RTSP path returns `401 after digest auth`, the account is right for
HTTP but not enabled for RTSP — fix that in the device's web interface.

If the capabilities show nothing about doors at all, your device is a camera
family rather than a door station, and `PUT
/ISAPI/AccessControl/RemoteControl/door/<id>` is unlikely to exist on it. The
camera view will still work; the gate button will not.

---

## 4. Configure the intercom app

Everything is in `intercom/.env`, and every setting there has a comment. The
ones that matter now:

| Setting | What to put there |
| --- | --- |
| `INTERCOM_HOST` | the device's address |
| `INTERCOM_USER`, `INTERCOM_PASSWORD` | the dedicated ISAPI account |
| `INTERCOM_DOOR_ID`, `RTSP_PATH` | what the probe told you |
| `SITE_NAME` | what the browser tab and the phone home screen say, e.g. `Front gate` |
| `PORT` | host port, default `8099` |
| `BIND_ADDR` | the host address to publish on. If the host also has an interface on your camera network, set this to the trusted LAN address so the app never appears on the camera side |
| `TZ` | your timezone, e.g. `Europe/Amsterdam`, so the gate log reads correctly |
| `GATE_COOLDOWN_SECONDS` | how long to refuse a second press, default 5 |

Avoid `$` in passwords: Compose interpolates `.env`, so `pa$$w0rd` does not
arrive as typed. `./build.sh check` warns about this.

Extra cameras are optional and can wait — see [CAMERAS.md](CAMERAS.md).

---

## 5. Start it

```sh
./build.sh check     # says what is missing, changes nothing
./build.sh up        # builds the image and starts both services
./build.sh status
```

Two containers come up: `intercom-go2rtc`, which pulls RTSP and repackages it
for browsers, and `intercom-app`, which serves the page and talks to the
device. Check them:

```sh
curl -s http://localhost:8099/api/health
curl -s http://localhost:8099/api/stream/status
```

`stream_status` is the useful one. `device_reachable: false` means the host
cannot open port 554 on the intercom — a network or firewall problem, not an
app problem. `go2rtc_up: false` means the go2rtc container is not running or
bound somewhere else.

Now open `http://<your-host>:8099` in a browser on the same network. You
should see video within a few seconds. Hold the button for a second; the
gate should open and the press should appear in the log:

```sh
docker compose -f intercom/docker-compose.yml exec intercom-app tail -n 5 /data/gate.log
```

---

## 6. Put a reverse proxy in front

Do this before you rely on it. Both apps expect to be shielded, and the
intercom app has no authentication at all — see [../SECURITY.md](../SECURITY.md).

What you want, whichever proxy you use:

- TLS on an internal hostname (a wildcard certificate for a domain you own is
  the easy route; Let's Encrypt cannot validate a name that never resolves
  publicly).
- **WebSocket upgrades enabled** — the video will not play without them.
- An **access list allowing only your LAN**, denying everything else.
- `X-Forwarded-For` / `X-Real-IP` passed through, so the gate log records the
  device that pressed rather than the proxy.
- **No public DNS record, and no port forward.**

### nginx-proxy-manager

There is a helper that does it through NPM's API:

```sh
printf 'NPM_EMAIL=you@example.com\nNPM_PASSWORD=...\n' > ~/.npm-admin
chmod 600 ~/.npm-admin

NPM_URL=http://<npm-host>:81 \
DOMAIN=gate.example.lan FORWARD_HOST=<your-host> FORWARD_PORT=8099 \
CERT_NAME=<a certificate already in NPM> ACL_CIDR=192.0.2.0/24 \
    intercom/tools/npm-proxy-host.sh
```

Run it again with `DOMAIN=keys.example.lan FORWARD_PORT=8097` for the key GUI.

### Caddy

```caddyfile
gate.example.lan {
    @lan remote_ip 192.0.2.0/24
    handle @lan {
        reverse_proxy <your-host>:8099
    }
    respond 403
}
```

Caddy upgrades WebSockets and sets the forwarded headers by itself.

### nginx

```nginx
server {
    listen 443 ssl;
    server_name gate.example.lan;

    allow 192.0.2.0/24;
    deny all;

    location / {
        proxy_pass http://<your-host>:8099;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_read_timeout 3600s;
    }
}
```

### On a phone

Open the site in Safari (iPhone) or Chrome (Android) and add it to the home
screen. It then opens full-screen under `SITE_NAME`. There is deliberately no
offline cache — a cached page at a gate would be misleading.

---

## 7. Add the key app (optional)

Skip this unless you want QR codes. Nothing else depends on it.

```sh
$EDITOR gatekeys/.env     # GATEKEYS_USER, GATEKEYS_PASSWORD, TZ, PORT, BIND_ADDR
./build.sh check gatekeys
./build.sh up gatekeys
```

`GATEKEYS_SECRET_KEY` and `GATEKEYS_VERIFY_TOKEN` were generated by
`./build.sh setup`; leave them alone. Set `TZ` to your own timezone: schedules
are evaluated against the clock the person holding the code reads.

Open `http://<your-host>:8097`, sign in, and make a key. Give it a name, a
date range and at least one window, then print the card. Put this page behind
the same LAN-only proxy as the other one — it hands out working gate keys.

Point the intercom app at it, in `intercom/.env`:

```sh
GATEKEYS_URL=http://<your-host>:8097
```

That has to be an address the `intercom-app` **container** can reach, so on a
single host it is the host's own LAN address and the published port — not
`localhost`, which inside the container means the container itself.
`GATEKEYS_TOKEN` already matches; `./build.sh check` confirms it.

---

## 8. Turn on the QR scanner (optional)

The scanner needs to know when someone is standing at the gate, and that
comes from Frigate over MQTT. Without a broker it stays off — there is no
"scan all the time" fallback, deliberately, because decoding video around the
clock to open a gate is a poor trade.

You need:

- Frigate with the intercom's camera configured and a `person` object filter.
- An MQTT broker Frigate publishes to.
- An MQTT account for this app that may **only subscribe** to
  `frigate/events`, so the app cannot send Frigate commands.

In `intercom/.env`:

```sh
MQTT_HOST=<broker address>
MQTT_USER=<read-only account>
MQTT_PASSWORD=<its password>
QR_FRIGATE_CAMERA=gate      # Frigate's name for the gate camera
QR_ACTION=log               # decode and log, but do not open — start here
```

Then `./build.sh up intercom` and check `curl -s
http://localhost:8099/api/qr/status`: `mqtt_connected` should be `true`, and
`persons_at_gate` should rise when someone walks up.

Print the test sheet and hold it up at the gate:

```sh
docker run --rm -v "$PWD:/out" intercom-app:latest \
    python tools/make_test_qr.py /out/gate-qr-test.pdf
```

Print it at 100%, not "fit to page", or the size labels lie. Read the results
at `/api/qr/log`. When codes decode reliably, switch `QR_ACTION=open` and
restart. Print real keys at 40 mm or larger.

If nothing decodes, set `QR_DEBUG_FRAMES=120` for one session: the last 120
frames seen with someone at the gate are written to `/data/qr-frames/`, one a
second, and looking at them usually answers it in seconds (too small, too far,
blown out by the IR lamp, or a stream at the wrong resolution). Set it back to
0 and delete the directory afterwards — those are stills of whoever stands at
your gate.

---

## 9. Updating, backups, uninstalling

**Update:**

```sh
git pull
./build.sh up          # rebuilds and restarts both
```

**Back up.** Two things are worth saving, and neither is in git:

- `intercom/.env` and `gatekeys/.env` — your credentials.
- The `gatekeys` database, which holds every key you have issued:
  ```sh
  docker run --rm -v gatekeys_gatekeys-data:/data -v "$PWD:/backup" \
      alpine tar czf /backup/gatekeys-backup.tar.gz -C /data .
  ```

The gate log lives in the `intercom_intercom-data` volume; back it up the
same way if you want to keep the history.

**Uninstall:**

```sh
./build.sh down
docker volume rm intercom_intercom-data gatekeys_gatekeys-data   # deletes the keys and the log
```

---

## 10. When it does not work

**No video, but the page loads.** Check `/api/stream/status`. If
`device_reachable` is false, the host cannot open port 554 on the intercom.
If it is true but `stream_active` is false, the RTSP path or the credentials
are wrong: run `./build.sh probe` again. Look at `docker compose -f
intercom/docker-compose.yml logs go2rtc` — it says plainly what the device
answered.

**Video on a laptop, not on an old iPhone.** Media Source Extensions arrived
late on iOS. The page falls back to MJPEG automatically; it is heavier and
lower quality, and that is expected.

**The button says "Intercom offline".** The device did not answer within the
timeout. If it says the credentials were rejected, the ISAPI account is wrong
or lacks door rights. If it says the door endpoint was not found, the door id
is wrong — try `./build.sh probe` and the other ids it lists.

**A press does nothing at the gate but the log says `opened`.** `opened`
means the intercom accepted the command and pulsed its relay. Everything past
that is wiring: the relay's contacts, the gate controller's input, and
whatever else shares that circuit. This app cannot see any of it.

**Everything is refused with `cooldown`.** That is `GATE_COOLDOWN_SECONDS`
doing its job, including after failed attempts, so a sulking intercom is not
hammered.

**Codes are refused but the key looks fine.** Check the timezone in
`gatekeys/.env` and the key's window in the GUI — the state column says
exactly which rule refused it, and "next opening" tells you when it would
work. Check `docker compose -f intercom/docker-compose.yml logs intercom-app`
for `gatekeys unreachable`: from inside the container, `GATEKEYS_URL` must be
a real network address, not `localhost`.

**The QR scanner never scans.** `mqtt_connected: false` means the broker or
the credentials are wrong. `persons_at_gate: 0` while someone is there means
`QR_FRIGATE_CAMERA` does not match Frigate's camera name, or Frigate is not
detecting a person on it.

**Compose says a variable is missing.** Run `./build.sh check`; it names the
setting and the file.

Still stuck? Open an issue with your device model, what `./build.sh probe`
printed (with the credentials removed) and the relevant logs.
