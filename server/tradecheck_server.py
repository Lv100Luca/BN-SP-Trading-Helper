#!/usr/bin/env python3
"""Trade Check global table - a minimal HTTP API for the shared, read-only lookup table.

Standard library only (Python 3.9+): nothing to pip install on the VPS. State is one SQLite file.

    GET /v1/table      the whole table as JSON; clients fetch it on start-up and on "Refresh".
                       Sends an ETag, answers 304 to If-None-Match, and is rate limited per IP.
    PUT /v1/records    replace the table with a JSON file exported from the app (Records tab ->
                       Export -> .json). Needs `Authorization: Bearer <admin token>`.
    GET /health        {"ok": true, "version": N, "count": M}

Environment:
    TRADECHECK_ADMIN_TOKEN   required for PUT; when unset, PUT is refused entirely
    TRADECHECK_DB            SQLite path                (default: ./global.sqlite)
    TRADECHECK_HOST / PORT   bind address               (default: 127.0.0.1:8787)
    TRADECHECK_RATE          GETs per IP per minute     (default: 10)
    TRADECHECK_TRUST_PROXY   1 = take the client IP from X-Forwarded-For (set when behind Caddy/nginx)
"""
from __future__ import annotations

import hmac
import json
import os
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

STATES = ("trading", "fighting", "afk", "fake")
MAX_BODY = 32 * 1024 * 1024

SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    name       TEXT PRIMARY KEY COLLATE NOCASE,
    state      TEXT NOT NULL,
    notes      TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
INSERT OR IGNORE INTO meta (key, value) VALUES ('version', '0');
INSERT OR IGNORE INTO meta (key, value) VALUES ('updated_at', '');
"""


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize_name(name: str) -> str:
    return " ".join((name or "").split())


# ------------------------------------------------------------------------- storage
class Table:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.lock = threading.Lock()
        self._cache: tuple[int, bytes] | None = None  # (version, encoded /v1/table body)

    def meta(self) -> dict:
        rows = {r["key"]: r["value"] for r in self.conn.execute("SELECT key, value FROM meta")}
        count = self.conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
        return {"version": int(rows.get("version", "0")), "updated_at": rows.get("updated_at", ""), "count": count}

    def dump_bytes(self) -> tuple[int, bytes]:
        """Encoded table body, cached until the next write."""
        with self.lock:
            meta = self.meta()
            if self._cache and self._cache[0] == meta["version"]:
                return self._cache
            rows = self.conn.execute(
                "SELECT name, state, notes, updated_at FROM records ORDER BY name COLLATE NOCASE"
            ).fetchall()
            meta["records"] = [dict(r) for r in rows]
            body = json.dumps(meta, ensure_ascii=False).encode("utf-8")
            self._cache = (meta["version"], body)
            return self._cache

    def replace(self, records: list[dict]) -> dict:
        """Replace the whole table with the rows of an app export ({name, state, notes?, ...}).

        Rows whose state or notes did not change keep their updated_at; new and changed rows get
        the current time. Names missing from the upload are removed - the export is the truth.
        """
        ts = now()
        added = updated = unchanged = skipped = 0
        seen: dict[str, dict] = {}
        for rec in records:
            name = normalize_name(str(rec.get("name", "")))
            state = str(rec.get("state", "")).strip().lower()
            if not name or state not in STATES:
                skipped += 1
                continue
            seen[name.lower()] = {"name": name, "state": state, "notes": str(rec.get("notes") or "")}
        with self.lock:
            before = {r["name"].lower(): r for r in self.conn.execute("SELECT * FROM records")}
            removed = len(before) - len(before.keys() & seen.keys())
            self.conn.execute("DELETE FROM records")
            for key, rec in seen.items():
                old = before.get(key)
                if old is None:
                    added += 1
                    stamp = ts
                elif old["state"] != rec["state"] or old["notes"] != rec["notes"]:
                    updated += 1
                    stamp = ts
                else:
                    unchanged += 1
                    stamp = old["updated_at"]
                self.conn.execute(
                    "INSERT INTO records (name, state, notes, updated_at) VALUES (?, ?, ?, ?)",
                    (rec["name"], rec["state"], rec["notes"], stamp),
                )
            if added or updated or removed:
                self.conn.execute(
                    "UPDATE meta SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT) WHERE key = 'version'"
                )
                self.conn.execute("UPDATE meta SET value = ? WHERE key = 'updated_at'", (ts,))
            self.conn.commit()
            meta = self.meta()
        return {"added": added, "updated": updated, "removed": removed, "unchanged": unchanged,
                "skipped": skipped, **meta}


# ------------------------------------------------------------------------- rate limit
class RateLimiter:
    """Fixed window per client: at most `per_minute` requests in any running 60 s window."""

    def __init__(self, per_minute: int):
        self.per_minute = per_minute
        self.hits: dict[str, list[float]] = {}
        self.lock = threading.Lock()

    def allow(self, client: str) -> tuple[bool, int]:
        """Returns (allowed, seconds until the window frees up)."""
        t = time.monotonic()
        with self.lock:
            if len(self.hits) > 10_000:  # forget idle clients now and then
                self.hits = {k: v for k, v in self.hits.items() if v and t - v[-1] < 60}
            window = [h for h in self.hits.get(client, []) if t - h < 60]
            if len(window) >= self.per_minute:
                self.hits[client] = window
                return False, int(60 - (t - window[0])) + 1
            window.append(t)
            self.hits[client] = window
            return True, 0


# ------------------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "TradeCheckTable/1.0"
    table: Table
    limiter: RateLimiter
    admin_token: str
    trust_proxy: bool

    def _send(self, status: int, body: bytes, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload, extra: dict | None = None) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"), extra)

    def _error(self, status: int, msg: str, extra: dict | None = None) -> None:
        self._json(status, {"error": msg}, extra)

    def _client(self) -> str:
        if self.trust_proxy:
            fwd = self.headers.get("X-Forwarded-For", "")
            if fwd:
                return fwd.split(",")[0].strip()
        return self.client_address[0]

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        if not self.admin_token or not header.lower().startswith("bearer "):
            return False
        return hmac.compare_digest(header[7:].strip(), self.admin_token)

    def log_message(self, fmt, *args):
        sys.stdout.write("%s - %s\n" % (self._client(), fmt % args))
        sys.stdout.flush()

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/health":
            return self._json(HTTPStatus.OK, {"ok": True, **self.table.meta()})
        if path != "/v1/table":
            return self._error(HTTPStatus.NOT_FOUND, "unknown route")
        ok, retry = self.limiter.allow(self._client())
        if not ok:
            return self._error(HTTPStatus.TOO_MANY_REQUESTS, "rate limited", {"Retry-After": str(retry)})
        version, body = self.table.dump_bytes()
        etag = '"%d"' % version
        if self.headers.get("If-None-Match") == etag:
            self.send_response(HTTPStatus.NOT_MODIFIED)
            self.send_header("ETag", etag)
            self.end_headers()
            return None
        return self._send(HTTPStatus.OK, body, {"ETag": etag})

    def do_PUT(self):
        if urlsplit(self.path).path != "/v1/records":
            return self._error(HTTPStatus.NOT_FOUND, "unknown route")
        if not self._authorized():
            return self._error(HTTPStatus.UNAUTHORIZED, "admin token required")
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY:
            return self._error(HTTPStatus.BAD_REQUEST, "missing or oversized body")
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            return self._error(HTTPStatus.BAD_REQUEST, f"bad JSON: {exc}")
        records = payload.get("records") if isinstance(payload, dict) else payload
        if not isinstance(records, list):
            return self._error(HTTPStatus.BAD_REQUEST, 'expected an app export: {"records": [...]}')
        return self._json(HTTPStatus.OK, self.table.replace(records))


def main() -> None:
    host = os.environ.get("TRADECHECK_HOST", "127.0.0.1")
    port = int(os.environ.get("TRADECHECK_PORT", "8787"))
    db = os.environ.get("TRADECHECK_DB", "global.sqlite")
    token = os.environ.get("TRADECHECK_ADMIN_TOKEN", "").strip()
    if not token:
        print("warning: TRADECHECK_ADMIN_TOKEN is not set - uploads are refused", file=sys.stderr)

    Handler.table = Table(db)
    Handler.limiter = RateLimiter(int(os.environ.get("TRADECHECK_RATE", "10")))
    Handler.admin_token = token
    Handler.trust_proxy = os.environ.get("TRADECHECK_TRUST_PROXY", "") == "1"
    srv = ThreadingHTTPServer((host, port), Handler)
    meta = Handler.table.meta()
    print(f"trade check table on http://{host}:{port}  db={db}  version={meta['version']} records={meta['count']}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
