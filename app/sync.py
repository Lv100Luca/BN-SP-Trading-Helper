"""Download the shared, read-only lookup table from the server
(https://github.com/Lv100Luca/BN-SP-Trading-Helper-API).

The whole table comes down in one GET; the app keeps a copy in the local database
(Database.replace_global) and answers lookups from there, so reading a name never touches the
network. Fetched once at start-up, on Refresh in the Settings tab and, when auto-download is on,
every sync interval. Contributors with a key push their queued saves (push_records) on the same
interval.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from . import GLOBAL_TABLE_URL, __version__

TIMEOUT = 15  # seconds


def table_url() -> str:
    """Environment override first (handy for testing against a local server), then the built-in URL."""
    return (os.environ.get("TRADECHECK_GLOBAL_URL") or GLOBAL_TABLE_URL).strip().rstrip("/")


@dataclass
class Fetched:
    status: str                      # "updated" | "unchanged"
    etag: str = ""
    version: int = 0
    updated_at: str = ""
    records: list[dict] = field(default_factory=list)


class SyncError(Exception):
    """Human-readable reason the table could not be fetched or written."""


class AuthError(SyncError):
    """The server rejected the contributor key."""


def fetch_table(url: str, etag: str = "") -> Fetched:
    """Blocking GET of <url>/v1/table. Sends our ETag so an unchanged table costs a 304 and no body.
    Safe to call from a worker thread (no tkinter / sqlite in here)."""
    headers = {"Accept": "application/json", "User-Agent": f"TradeCheck/{__version__}"}
    if etag:
        headers["If-None-Match"] = etag
    req = urllib.request.Request(url + "/v1/table", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
            new_etag = resp.headers.get("ETag", "")
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            return Fetched("unchanged", etag)
        if exc.code == 429:
            wait = exc.headers.get("Retry-After", "a minute")
            raise SyncError(f"server is rate limiting refreshes, try again in {wait}s") from exc
        raise SyncError(f"server answered {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise SyncError(f"could not reach the server ({exc.reason})") from exc
    except (OSError, ValueError) as exc:
        raise SyncError(str(exc)) from exc
    records = payload.get("records") if isinstance(payload, dict) else None
    if not isinstance(records, list):
        raise SyncError("unexpected response from the server")
    return Fetched("updated", new_etag, int(payload.get("version", 0)), str(payload.get("updated_at", "")), records)


def check_key(url: str, key: str, timeout: float = TIMEOUT) -> dict:
    """Blocking GET of <url>/v1/keys/me: {"id", "label"} for a valid contributor key, AuthError for
    an unknown one. Safe to call from a worker thread."""
    req = urllib.request.Request(
        url + "/v1/keys/me",
        headers={"Accept": "application/json", "Authorization": f"Bearer {key}",
                 "User-Agent": f"TradeCheck/{__version__}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise AuthError("the server does not know this contributor key") from exc
        raise SyncError(f"server answered {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise SyncError(f"could not reach the server ({exc.reason})") from exc
    except (OSError, ValueError) as exc:
        raise SyncError(str(exc)) from exc


def push_records(url: str, key: str, records: list[dict], timeout: float = TIMEOUT) -> dict:
    """Blocking POST of saves ({name, state, notes, ts, kind, new_name}) with a contributor key.
    Returns the server's report (added / updated / unchanged / retracted / renamed / skipped /
    version). Safe to call from a worker thread."""
    body = json.dumps({"records": records}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url + "/v1/records", data=body, method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json",
                 "Authorization": f"Bearer {key}", "User-Agent": f"TradeCheck/{__version__}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise AuthError("the server rejected your contributor key") from exc
        if exc.code == 429:
            wait = exc.headers.get("Retry-After", "a minute")
            raise SyncError(f"server is rate limiting uploads, try again in {wait}s") from exc
        raise SyncError(f"server answered {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise SyncError(f"could not reach the server ({exc.reason})") from exc
    except (OSError, ValueError) as exc:
        raise SyncError(str(exc)) from exc
