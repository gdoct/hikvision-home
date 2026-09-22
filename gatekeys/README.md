# gatekeys

Issues and manages the QR codes that may open the gate, and answers one
question for [`../intercom`](../intercom): *may this code open the gate right
now?*

```
browser ──https──▶ reverse proxy ──http :8097──▶ gatekeys ──▶ /data/gatekeys.db
  (LAN-only + login)                                 ▲
                                                     │ POST /api/verify
intercom camera ──QR──▶ intercom-app ────────────────┘
                              └── opens the gate if the answer is "valid"
```

Hand someone a printed card that works on Tuesday mornings until March, and
revoke it in two clicks when it should not any more.

## Why this is its own service

The intercom app views the cameras and pulses the gate relay, and has to keep
working while something else is being changed. Access rules are the part that
keeps changing: a new cleaner, a window moved by fifteen minutes, a code
revoked in a hurry. Keeping them here means the gate button survives every one
of those edits, and this service never holds the intercom's credentials.

If gatekeys is down, codes stop working and the button does not. That is the
intended failure direction — it never fails open.

You can run `intercom` without this app entirely; then the button is the only
way in.

## A key

| | |
| --- | --- |
| Code | `GK-XXXX-XXXX-XXXX`, 60 random bits, alphabet without I/L/O/U |
| Name and note | who it is for, e.g. "Cleaner — Tue mornings" |
| Date range | inclusive both ends, e.g. 2026-12-01 … 2027-03-01 |
| Windows | weekday + start/end on a 15-minute grid, e.g. Mon, Tue 07:15–13:00 and 15:45–20:30 |
| Max uses | optional; blank means unlimited |
| Cooldown | ignore the same code again within N seconds (default 10), so one presentation is one opening |
| Revoke | switch off without deleting, keeping its history |

Times are evaluated in `TZ`, because that is the clock the person holding the
code reads. `zoneinfo` handles DST: a window keeps its wall-clock times across
the switch, so "mornings" does not shift by an hour in October.

A window may not cross midnight — the GUI refuses it. Use one window on each
day (e.g. Fri 22:00–24:00 and Sat 00:00–02:00), which is also what the person
holding the card can actually read.

Each key prints as an A6 card: the QR at 40 mm, the code in text, who it is
for and when it works — so a card found in a drawer in two years explains
itself.

## Verification

`POST /api/verify` with header `X-Gatekeys-Token: <GATEKEYS_VERIFY_TOKEN>`:

```json
{"code": "GK-XXXX-XXXX-XXXX", "source": "intercom/gate"}
→ {"valid": true, "reason": "ok", "name": "Cleaner", "key_id": 1, "uses_left": null}
```

Refusal reasons, checked in this order: `unknown`, `revoked`, `not yet valid`,
`expired`, `outside its schedule`, `used up`, `cooldown`.

A `valid` answer **counts as a use** — it is the authorisation event, whether
or not the relay then clicks. A code with one use left therefore cannot be
spent twice by retrying. Every call, valid or not, is written to `events`, and
that table survives deleting the key it refers to.

## Layout

| Path | What |
| --- | --- |
| `app/main.py` | API, session login, verification |
| `app/db.py` | SQLite schema and access |
| `app/schedule.py` | windows, date ranges, "next opening" |
| `app/codes.py` | code generation, QR PNG, printable A6 card |
| `app/static/index.html` | the GUI (vanilla JS, same design tokens as ../intercom) |
| `.env.example` | every setting, with comments; copy to `.env` (0600) |

## Quick start

From the repository root, `./build.sh setup gatekeys` copies `.env.example`
into place and generates the two secrets; or by hand:

```sh
cp .env.example .env && chmod 600 .env
$EDITOR .env            # user, password, and two random secrets
docker compose up -d --build
```

`GATEKEYS_VERIFY_TOKEN` must equal `GATEKEYS_TOKEN` in `../intercom/.env`, and
`../intercom` needs `GATEKEYS_URL` pointing at this service — the address the
intercom container can reach it on, which on a single host is the host's own
LAN address and this port, not `localhost`.

Full instructions: [../docs/INSTALL.md](../docs/INSTALL.md).

## Changing the login

`.env` is read when the container starts, so edit it and then:

```sh
docker compose up -d      # in this folder; restarting is not optional
```

Avoid `$` in the password: Compose reads `.env` with variable interpolation,
so `pa$$w0rd` does not arrive as typed. Changing `GATEKEYS_SECRET_KEY` as well
signs every open session out.

## Security posture

- **Put it behind a LAN-only reverse proxy *and* keep the login.** This page
  hands out gate keys, so one compromised device on your network should not be
  enough. See [../SECURITY.md](../SECURITY.md).
- **Publish the port on your trusted address only** (`BIND_ADDR`), never
  `0.0.0.0`, if the host also has an interface on the camera network.
- **The database holds codes in plain text**, because the GUI must be able to
  re-print a card. Treat the `gatekeys-data` volume as a drawer of spare keys.
- **The verify endpoint takes no cookie** and the GUI endpoints take no shared
  secret, so neither can be reached with the other's credentials.
- Sessions are a signed cookie (`GATEKEYS_SECRET_KEY`), valid
  `GATEKEYS_SESSION_HOURS` (12). Changing the secret signs everyone out.

## Not done

- **One account.** There is no per-person login and no rate limit on the login
  form; the LAN-only restriction is doing that work.
- **No notification** when a code opens the gate. If you run Frigate and Home
  Assistant they already notify about a person at the gate; the intercom app's
  `/data/gate.log` records which code opened it (`key_name`).
- **No expiry cleanup.** Expired keys stay in the list, greyed out, until
  deleted by hand — deliberate, so "who had a key last winter" stays
  answerable.
