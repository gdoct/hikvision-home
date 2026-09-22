## What this changes

<!-- One or two sentences. One subject per PR, please. -->

## Why

<!-- What was wrong, or what you could not do before. -->

## Tested on

- Hardware: <!-- e.g. Hikvision DS-KV8113, or "no device — untested against real hardware" -->
- Host: <!-- e.g. Debian 12, Docker 27.x -->
- What I actually ran: <!-- e.g. ./build.sh check && ./build.sh up, held the button, read the gate log -->

## Checklist

- [ ] No secrets in the diff — no addresses from my own network, no hostnames, no tokens, no real QR codes
- [ ] New settings are in `.env.example` **and** `docker-compose.yml`, with a comment
- [ ] Docs updated where this changes behaviour (`README.md`, `docs/`)
- [ ] Nothing here makes the gate fail *open*
