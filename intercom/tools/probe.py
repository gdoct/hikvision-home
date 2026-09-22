#!/usr/bin/env python3
"""
Ask the device what it actually supports, before trusting any default:

  - GET /ISAPI/System/deviceInfo        what the device says it is
  - GET /ISAPI/System/capabilities      does it advertise door control, and where
  - GET /ISAPI/AccessControl/RemoteControl/door/capabilities   door ids, if any
  - RTSP DESCRIBE with digest auth      which stream paths actually exist

Run inside the container so it sees the same .env:

    docker compose run --rm intercom-app python tools/probe.py

It never opens the gate.
"""

from __future__ import annotations

import hashlib
import os
import re
import socket
import sys
import uuid

import httpx

HOST = os.environ.get("INTERCOM_HOST", "")
USER = os.environ.get("INTERCOM_USER", "")
PASSWORD = os.environ.get("INTERCOM_PASSWORD", "")
RTSP_CANDIDATES = [
    "/Streaming/Channels/101",
    "/Streaming/Channels/102",
    "/ch1/main/av_stream",
    "/ch1/sub/av_stream",
    "/h264/ch1/main/av_stream",
    "/live",
]


def section(title: str) -> None:
    print(f"\n=== {title}")


def isapi_get(client: httpx.Client, path: str) -> None:
    section(f"GET {path}")
    try:
        r = client.get(f"http://{HOST}{path}")
    except httpx.HTTPError as exc:
        print(f"  request failed: {exc}")
        return
    print(f"  HTTP {r.status_code}")
    text = r.text.strip()
    if path.endswith("capabilities") and len(text) > 4000:
        # Show only the parts that matter for door control.
        hits = [m.start() for m in re.finditer(r"(?i)door|remotecontrol|accesscontrol|isSupport\w*Door", text)]
        if hits:
            print("  door-related fragments:")
            seen = set()
            for h in hits:
                start = max(0, h - 80)
                if start in seen:
                    continue
                seen.add(start)
                print("   …", " ".join(text[start:h + 120].split()), "…")
        else:
            print("  no door / remote-control / access-control words in capabilities")
        print(f"  ({len(text)} chars total; run with --full to print everything)")
        if "--full" in sys.argv:
            print(text)
    else:
        print("  " + "\n  ".join(text.splitlines()[:60]))


def rtsp_describe(path: str) -> str:
    """Minimal RTSP DESCRIBE with Digest auth. Returns a one-line verdict."""
    uri = f"rtsp://{HOST}:554{path}"

    def send(sock: socket.socket, cseq: int, auth: str | None) -> tuple[int, str]:
        req = f"DESCRIBE {uri} RTSP/1.0\r\nCSeq: {cseq}\r\nAccept: application/sdp\r\nUser-Agent: intercom-probe\r\n"
        if auth:
            req += f"Authorization: {auth}\r\n"
        sock.sendall((req + "\r\n").encode())
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
        head, _, body = data.partition(b"\r\n\r\n")
        headers = head.decode(errors="replace")
        m = re.search(r"Content-Length:\s*(\d+)", headers, re.I)
        if m:
            need = int(m.group(1)) - len(body)
            while need > 0:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                body += chunk
                need -= len(chunk)
        status = int(headers.split(" ", 2)[1]) if headers.startswith("RTSP/") else 0
        return status, headers + "\r\n\r\n" + body.decode(errors="replace")

    try:
        with socket.create_connection((HOST, 554), timeout=4) as sock:
            status, resp = send(sock, 1, None)
            if status == 401:
                m = re.search(r'WWW-Authenticate:\s*Digest\s+(.*)', resp, re.I)
                if not m:
                    return f"401 without a Digest challenge: {resp.splitlines()[0]}"
                params = dict(re.findall(r'(\w+)="([^"]*)"', m.group(1)))
                realm, nonce = params.get("realm", ""), params.get("nonce", "")
                ha1 = hashlib.md5(f"{USER}:{realm}:{PASSWORD}".encode()).hexdigest()
                ha2 = hashlib.md5(f"DESCRIBE:{uri}".encode()).hexdigest()
                response = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
                auth = (f'Digest username="{USER}", realm="{realm}", nonce="{nonce}", '
                        f'uri="{uri}", response="{response}"')
                status, resp = send(sock, 2, auth)
            if status == 200:
                media = re.findall(r"^m=(\w+)", resp, re.M)
                codecs = re.findall(r"a=rtpmap:\d+ ([\w\-\.]+)", resp)
                return f"OK  media={media} codecs={codecs}"
            if status == 401:
                return "401 after digest auth — wrong credentials, or RTSP is disabled for this account"
            if status == 404:
                return "404 — path does not exist"
            return f"{status} — {resp.splitlines()[0] if resp else 'no response'}"
    except (OSError, ValueError) as exc:
        return f"error: {exc}"


def main() -> None:
    if not HOST or not USER or not PASSWORD:
        print("INTERCOM_HOST / INTERCOM_USER / INTERCOM_PASSWORD not set; fill .env first.")
        sys.exit(2)

    print(f"intercom probe — {HOST} as {USER}")
    with httpx.Client(auth=httpx.DigestAuth(USER, PASSWORD), timeout=6.0) as client:
        isapi_get(client, "/ISAPI/System/deviceInfo")
        isapi_get(client, "/ISAPI/System/capabilities")
        isapi_get(client, "/ISAPI/AccessControl/RemoteControl/door/capabilities")

    section("RTSP DESCRIBE")
    for path in RTSP_CANDIDATES:
        print(f"  {path:32s} {rtsp_describe(path)}")

    print("\nNext: put the working RTSP path in .env as RTSP_PATH and the door id as INTERCOM_DOOR_ID.")
    print(f"probe id {uuid.uuid4().hex[:8]} done.")


if __name__ == "__main__":
    main()
