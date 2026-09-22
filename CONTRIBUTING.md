# Contributing

Thanks for looking. This project exists because one Hikvision intercom needed
a decent web page, and it is shared so the next person does not have to redo
the ISAPI guesswork. Reports from *other* hardware are the most valuable
thing you can send.

## Ways to help, roughly in order of usefulness

1. **Tell us what your device needed.** Which model, which door id, which
   RTSP path, and what `./build.sh probe` printed. Hikvision families differ
   enough that this is real documentation, not small talk.
2. **Report a bug** with the logs around it (`./build.sh logs`) and what you
   expected instead.
3. **Improve the docs.** If something in [docs/INSTALL.md](docs/INSTALL.md)
   only made sense to the person who wrote it, say so.
4. **Send a patch.** See below.

Please do not open a public issue for a security problem — see
[SECURITY.md](SECURITY.md).

## Running it while you work on it

You need Docker with the Compose plugin, and something to point it at. A real
intercom is ideal; without one, most of the app still runs — the page, the
gate endpoint (which will report `device_unreachable`), the gate log, and all
of `gatekeys`.

```sh
./build.sh setup
$EDITOR intercom/.env gatekeys/.env
./build.sh check
./build.sh up
./build.sh logs
```

Both apps are plain Python with no build step, so the loop for a code change
is `./build.sh up` again (it rebuilds). For the frontend, the page is a single
static file per app; edit it and reload — but note that `intercom/app/main.py`
substitutes `__SITE_NAME__` into the HTML as it serves it, so keep that
placeholder intact.

If you already run these apps for real on the same host, test against a copy
in a different directory **and** change `container_name`, `name:` and the
published ports in the compose files first. Otherwise Compose will happily
recreate your live containers and reuse your live volumes.

To work without Docker:

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -r intercom/requirements.txt
cd intercom && set -a && . ./.env && set +a
GO2RTC_URL=http://127.0.0.1:1984 GATE_LOG_PATH=./gate.log \
    uvicorn app.main:app --reload --port 8099
```

## House style

The existing code is the specification for new code. In short:

- **Python 3.13, FastAPI, no ORM, no framework on the frontend.** Adding a
  build step is a bigger decision than any feature it would enable; open an
  issue first.
- **Comments explain *why*, not *what*.** There are a lot of them, and they
  are load-bearing: "the cooldown starts on every attempt that reached the
  device, so a failing intercom is not hammered" is the kind of thing that
  gets deleted by a well-meaning refactor otherwise.
- **Lines around 100 characters**, four-space indent in Python, two in
  YAML/HTML/JS. See [.editorconfig](.editorconfig).
- **Every setting goes in `.env.example`** with a comment saying what it does
  and what happens if you get it wrong, plus the matching line in
  `docker-compose.yml`.
- **Never widen what the browser can see.** Host addresses, credentials and
  stream URLs stay server-side; `/api/cameras` strips the host on purpose.
- **Fail closed.** Anything that cannot be checked is refused. If you find
  yourself writing a fallback that opens the gate when a dependency is down,
  that is the bug.
- **Do not claim the gate is open.** The relay is a pulse on a circuit shared
  with other openers; the app reports what it sent, never what the gate did.

## Pull requests

- One subject per PR. A bug fix and a refactor in one branch is two PRs.
- Say which hardware you tested on, and what you could not test. "Untested on
  a real device" is an acceptable note, not a reason to withhold the patch.
- Update the docs in the same PR — `.env.example`, the app README and
  `docs/` where relevant.
- No secrets in the diff, ever: no addresses from your own network, no
  hostnames, no tokens, no sample QR codes that are real keys. Check with
  `git diff --staged` before you push.
- There is no CI gate on formatting. There is a workflow that builds both
  images and byte-compiles the Python; keep it green.

## What is deliberately out of scope

Two-way audio, answering intercom calls, recording and motion history, any
form of remote access, per-person logins, and a camera grid beyond three
cells. Patches for these are likely to be declined, not because they are bad
ideas but because they are a different project. Adding a fourth camera to
your own copy is documented in [docs/CAMERAS.md](docs/CAMERAS.md).

By contributing, you agree that your work is licensed under the
[MIT License](LICENSE).
