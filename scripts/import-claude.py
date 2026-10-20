#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx>=0.28,<1"]
# ///
"""Explicitly transfer current-user Claude credentials to one dashboard source.

Stop every other refresh writer yourself before acknowledging single ownership.
This helper never changes Claude/OMP settings, refreshes credentials, or uses the clipboard.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import pwd
import re
import stat
import subprocess
import sys
import warnings
from pathlib import Path
from urllib.parse import urlsplit

import httpx

COOKIE = "codex_lb_dashboard_session"
MAX_CREDENTIAL_BYTES = 64 * 1024


def tls_origin(value: str) -> str:
    if any(ord(c) < 33 for c in value) or "\\" in value:
        raise ValueError("A TLS dashboard origin is required")
    url = urlsplit(value)
    if url.scheme != "https" or not url.hostname or url.username is not None or url.password is not None or url.path not in ("", "/") or url.query or url.fragment:
        raise ValueError("A TLS dashboard origin is required")
    _ = url.port
    return f"https://{url.netloc}"


def parse_credentials(data: bytes | str) -> dict:
    if len(data) > MAX_CREDENTIAL_BYTES:
        raise ValueError("Credential record too large")
    try:
        document = json.loads(data)
    except (ValueError, UnicodeError) as exc:
        raise ValueError("Credential record is not JSON") from exc
    if not isinstance(document, dict):
        raise ValueError("Invalid credential record")
    credentials = document.get("claudeAiOauth", document)
    if not isinstance(credentials, dict):
        raise ValueError("Invalid Claude OAuth record")
    # Select only OAuth credentials; unrelated settings/analytics are never uploaded.
    result = {}
    for field in ("accessToken", "refreshToken"):
        value = credentials.get(field)
        if not isinstance(value, str) or not value or not re.fullmatch(r"[\x21-\x7e]+", value):
            raise ValueError("Missing live Claude OAuth credential")
        result[field] = value
    expires = credentials.get("expiresAt")
    if type(expires) is not int or expires <= 0:
        raise ValueError("Missing Claude OAuth expiry")
    result["expiresAt"] = expires
    if "scopes" in credentials:
        scopes = credentials["scopes"]
        if not isinstance(scopes, list) or any(not isinstance(scope, str) or not re.fullmatch(r"[A-Za-z0-9:_-]{1,100}", scope) for scope in scopes):
            raise ValueError("Invalid OAuth scopes")
        result["scopes"] = scopes
    return result


def read_local_credentials(platform: str = sys.platform, home: Path | None = None) -> dict:
    current_user = pwd.getpwuid(os.getuid())
    home = Path(current_user.pw_dir) if home is None else home
    if platform == "darwin":
        # Always ask the live current-user login Keychain first. Never fall back to
        # a stale .credentials.json file on macOS or search another user's keychain.
        result = subprocess.run(
            ["/usr/bin/security", "find-generic-password", "-a", current_user.pw_name,
             "-s", "Claude Code-credentials", "-w", str(home / "Library/Keychains/login.keychain-db")],
            capture_output=True, check=False, timeout=30,
        )
        if result.returncode:
            raise ValueError("Current-user Claude Keychain credential is unavailable")
        return parse_credentials(result.stdout)
    if platform.startswith("linux"):
        path = home / ".claude" / ".credentials.json"
        for directory in (home, path.parent):
            info = directory.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
                raise ValueError("Credential directory must be current-user owned and not writable by others")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_mode & 0o077 or info.st_size > MAX_CREDENTIAL_BYTES:
                raise ValueError("Credential file must be private and current-user owned")
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                return parse_credentials(stream.read(MAX_CREDENTIAL_BYTES + 1))
        finally:
            os.close(descriptor)
    raise ValueError("Only current-user macOS Keychain or Linux credentials are supported")


def hidden(prompt: str, *, allow_empty: bool = False) -> str:
    # getpass otherwise falls back to echoing stdin on a noninteractive terminal.
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        value = getpass.getpass(prompt)
    if (not value and not allow_empty) or "\r" in value or "\n" in value:
        raise ValueError("Empty authentication input")
    return value


def response_json(response: httpx.Response) -> dict:
    if response.status_code != 200:
        # Upstream errors may echo credentials. Never display response text.
        raise ValueError("Dashboard request was refused; check session and source permissions")
    document = response.json()
    if not isinstance(document, dict):
        raise ValueError("Invalid dashboard response")
    return document


def authenticate(client: httpx.Client, base_origin: str, mode: str) -> None:
    base_origin = tls_origin(base_origin)
    headers = {"Origin": base_origin, "Sec-Fetch-Site": "same-origin"}
    if mode == "session":
        cookie = hidden("Existing dashboard session cookie value (hidden): ")
        if not re.fullmatch(r"[\x21-\x7e]+", cookie) or ";" in cookie:
            raise ValueError("Invalid session cookie")
        client.cookies.set(COOKIE, cookie, domain=urlsplit(base_origin).hostname, path="/")
        session = response_json(client.get(base_origin + "/api/dashboard-auth/session", headers=headers, follow_redirects=False))
    else:
        username = hidden("Dashboard username (hidden; blank for single-user): ", allow_empty=True)
        session = response_json(client.post(base_origin + "/api/dashboard-auth/password/login", headers=headers, json={"username": username or None, "password": hidden("Dashboard password: ")}, follow_redirects=False))
        if not session.get("authenticated") and session.get("totpRequiredOnLogin"):
            session = response_json(client.post(base_origin + "/api/dashboard-auth/totp/verify", headers=headers, json={"code": hidden("Dashboard TOTP code: ")}, follow_redirects=False))
    if session.get("authenticated") is not True:
        raise ValueError("An authenticated dashboard session is required")


def upload_credentials(client: httpx.Client, base_url: str, source_id: str, credentials: dict, *, single_writer_confirmed: bool) -> None:
    if not single_writer_confirmed:
        raise ValueError("Explicit single-writer acknowledgement is required")
    base_origin = tls_origin(base_url)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", source_id):
        raise ValueError("An explicit source id is required")
    # Existing dashboard middleware enforces same-origin Origin/Sec-Fetch-Site.
    # Do not use a gateway bearer key: management requires dashboard identity.
    response = client.post(
        base_origin + f"/api/model-sources/{source_id}/credentials/import",
        headers={"Origin": base_origin, "Sec-Fetch-Site": "same-origin"},
        json={"credentials": credentials, "singleWriterConfirmed": True},
        follow_redirects=False,
    )
    if response.status_code not in (200, 201, 204):
        raise ValueError("Credential import refused; no server error body was displayed")
    # Do not print even a successful body: old or malicious servers may echo tokens.


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--single-writer-confirmed", action="store_true", help="I have stopped every other refresh writer for this credential")
    parser.add_argument("--dashboard-auth", choices=("session", "password"), default="session")
    args = parser.parse_args()
    try:
        if not args.single_writer_confirmed:
            raise ValueError("Single-writer confirmation required")
        base_origin = tls_origin(args.base_url)
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", args.source_id):
            raise ValueError("Invalid source id")
        # Read live credentials explicitly, without probing or refreshing them.
        credentials = read_local_credentials()
        with httpx.Client(timeout=30, follow_redirects=False, trust_env=False,
                          headers={"Origin": base_origin, "Sec-Fetch-Site": "same-origin"}) as client:
            authenticate(client, base_origin, args.dashboard_auth)
            upload_credentials(client, base_origin, args.source_id, credentials, single_writer_confirmed=True)
    except (ValueError, OSError, httpx.HTTPError, subprocess.SubprocessError, getpass.GetPassWarning, EOFError, KeyboardInterrupt):
        print("Import refused. Check current-user credentials, TLS dashboard authentication, explicit source id and single-writer acknowledgement. No local settings were changed.", file=sys.stderr)
        return 1
    print("Credential import accepted. Local native-client settings were not changed; keep other refresh writers stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
