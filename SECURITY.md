# Security

These apps open a physical gate. The blast radius of a mistake here is a
stranger in your driveway, not a 500 page. This file says exactly what
protects them, what does not, and how to report a problem.

## The threat model

**The gate page has no authentication, on purpose.** Anyone who can reach
`intercom-app` can open your gate. That is the entire feature: hear the bell,
open a browser, see who it is, let them in — no login, no app, no password
typed one-handed on a phone.

Everything that actually protects the gate is therefore *outside* the app:

| Protects it | Does not protect it |
| --- | --- |
| No public DNS record for the hostname | A name that only resolves internally — split-horizon DNS stops accidents, not attackers |
| No port forward to the reverse proxy | An obscure port number |
| A reverse-proxy access list allowing only your LAN | "It is behind a reverse proxy" on its own |
| A network where cameras cannot reach the app | The device's own password, which the app has to know |
| Credentials that stay server-side, never in the browser | The gate log, which records but prevents nothing |

`gatekeys` is different: it has a password login, because that page hands out
working gate keys. Put it behind the same LAN-only restriction *as well*. One
compromised device on your network should not be enough to mint a key.

## Rules

1. **Never create a public DNS record** for either hostname.
2. **Never port-forward** to the reverse proxy in front of them.
3. **If you want access from outside, use a VPN into your LAN.** Do not add a
   login to the gate page and call it internet-safe.
4. **Give the app its own ISAPI account** on the intercom, with only
   door-unlock and live-view rights — not the admin account. That password
   ends up in a file on a box that runs other things.
5. **Keep `.env` at mode 0600** and out of git. `./build.sh check` verifies
   both.
6. **Treat the gatekeys database as a drawer of spare keys.** It holds the
   codes in plain text, because the GUI must be able to re-print a card
   months later.
7. **Bind the published ports to your trusted address** (`BIND_ADDR`) if the
   host also has an interface on the camera network.
8. **Leave `QR_DEBUG_FRAMES` at 0** except while debugging. Those frames are
   stills of whoever stands at your gate.

## What the apps do for you

- The intercom's credentials never reach the browser. Only the app talks to
  the device; the page talks only to the app.
- go2rtc's API is bound to a private bridge address, because that API hands
  out stream URLs with the RTSP credentials in them.
- Every gate attempt is logged with source IP, forwarded-for, user agent and
  outcome, durably, in a volume.
- Server-side cooldown and rate limits (per IP and global) apply to the
  button and to codes alike, under one lock — so a person pressing the button
  while showing a code cannot pulse the relay twice.
- `gatekeys` counts a valid verdict as a use, so a code with one use left
  cannot be spent twice by retrying, and refuses everything when it cannot
  reach its database. It never fails open.
- The verify endpoint accepts no session cookie and the GUI endpoints accept
  no shared secret, so neither can be reached with the other's credentials.

## Known limitations

- One account in `gatekeys`, with no rate limit on the login form.
- No per-person identity anywhere: the gate log answers "which device and
  which code", never "which person".
- The apps trust `X-Forwarded-For` from the proxy in front of them. That is
  fine behind your own proxy and wrong if anything else can reach the port
  directly.
- No CSRF token on `POST /api/gate/open`. It is same-origin and
  unauthenticated by design; a LAN-only access list is what stands in the way.

## Reporting a vulnerability

Please **do not open a public issue** for something that would let a stranger
open someone's gate.

Use GitHub's private reporting on this repository
(Security → Report a vulnerability), which reaches the maintainer directly.
Include what you did, what happened, and what you think the consequence is.

This is a hobby project maintained by one person: expect a reply within a
week or two, not within hours. Fixes land in `main`; there are no release
branches to backport to.
