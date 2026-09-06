"""SQLite storage for player records and app settings."""
from __future__ import annotations

import os
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Iterable

STATES = ("trading", "fighting", "afk", "fake")
APP_NAME = "TradeCheck"

ROOT = Path(__file__).resolve().parent.parent


def _user_data_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home())
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / APP_NAME


def legacy_data_dir() -> Path:
    """Where versions before 0.3 kept the database: next to the exe (frozen) or in the project (source)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "data"
    return ROOT / "data"


def data_dir() -> Path:
    """One shared per-user folder, so the source checkout and the packaged exe see the same records.

    Windows: %APPDATA%/TradeCheck   macOS: ~/Library/Application Support/TradeCheck
    Linux: $XDG_DATA_HOME/TradeCheck (default ~/.local/share/TradeCheck).
    Override with the TRADECHECK_DATA_DIR environment variable (e.g. for a portable USB setup).
    """
    override = os.environ.get("TRADECHECK_DATA_DIR")
    if override:
        return Path(override).expanduser()
    return _user_data_dir()


DATA_DIR = data_dir()
DB_PATH = DATA_DIR / "records.sqlite"
LEGACY_DB_PATH = legacy_data_dir() / "records.sqlite"

SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE COLLATE NOCASE,
    state       TEXT NOT NULL,
    notes       TEXT NOT NULL DEFAULT '',
    times_seen  INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    name_key    TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now().isoformat(sep=" ", timespec="seconds")


def normalize_name(name: str) -> str:
    """Collapse whitespace; names are compared case-insensitively in the DB."""
    return " ".join((name or "").split())


def name_key(name: str) -> str:
    """Loose key: lowercase letters/digits only. OCR tends to drop '_', spaces and brackets,
    so 'Dark_Knight42', 'DarkKnight42' and 'dark knight 42' all map to the same record."""
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


class Database:
    def __init__(self, path: Path | None = None, absorb_legacy: bool = True):
        path = Path(path) if path else DB_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_records_key ON records(name_key)")
        self._backfill_keys()
        self.migration_report: dict | None = None
        if absorb_legacy:
            self._absorb_legacy(LEGACY_DB_PATH)

    def _absorb_legacy(self, legacy: Path) -> None:
        """One-time: merge a pre-0.3 database (project data/ or next to the exe) into this shared one.

        Records are merged (newer state wins, times_seen keeps the max), settings this database
        lacks are copied over, and the old file is renamed to *.migrated so it is not read twice.
        """
        try:
            if not legacy.exists() or legacy.resolve() == self.path.resolve():
                return
            other = Database(legacy, absorb_legacy=False)
            try:
                report = self.merge_records(dict(r) for r in other.all())
                for key, value in other.all_settings().items():
                    if self.get_setting(key) is None:
                        self.set_setting(key, value)
            finally:
                other.close()
            report["source"] = str(legacy)
            try:
                legacy.rename(legacy.with_name(legacy.name + ".migrated"))
            except OSError:
                pass  # still open elsewhere; merging is idempotent, so it is retried next start
            self.migration_report = report
        except Exception as exc:  # noqa: BLE001 - never block start-up on a migration hiccup
            print(f"legacy database migration skipped: {exc}", file=sys.stderr)

    def _migrate(self) -> None:
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(records)")}
        if "name_key" not in cols:
            self.conn.execute("ALTER TABLE records ADD COLUMN name_key TEXT NOT NULL DEFAULT ''")
            self.conn.commit()
        row = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'records'"
        ).fetchone()
        if row and "CHECK" in row["sql"]:
            # v0.1 hard-coded the allowed states in a CHECK constraint; SQLite cannot alter it,
            # so rebuild the table (states are validated in Python now).
            self.conn.executescript("""
                BEGIN;
                CREATE TABLE records_new (
                    id          INTEGER PRIMARY KEY,
                    name        TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    state       TEXT NOT NULL,
                    notes       TEXT NOT NULL DEFAULT '',
                    times_seen  INTEGER NOT NULL DEFAULT 1,
                    created_at  TEXT NOT NULL,
                    updated_at  TEXT NOT NULL,
                    name_key    TEXT NOT NULL DEFAULT ''
                );
                INSERT INTO records_new (id, name, state, notes, times_seen, created_at, updated_at, name_key)
                    SELECT id, name, state, notes, times_seen, created_at, updated_at, name_key FROM records;
                DROP TABLE records;
                ALTER TABLE records_new RENAME TO records;
                COMMIT;
            """)

    def _backfill_keys(self) -> None:
        rows = self.conn.execute("SELECT id, name FROM records WHERE name_key = ''").fetchall()
        for r in rows:
            self.conn.execute("UPDATE records SET name_key = ? WHERE id = ?", (name_key(r["name"]), r["id"]))
        if rows:
            self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------ records
    def get(self, name: str, loose: bool = True) -> sqlite3.Row | None:
        """Exact (case-insensitive) match first; with `loose`, fall back to the name_key match."""
        name = normalize_name(name)
        if not name:
            return None
        row = self.conn.execute(
            "SELECT * FROM records WHERE name = ? COLLATE NOCASE", (name,)
        ).fetchone()
        if row is None and loose and name_key(name):
            row = self.conn.execute(
                "SELECT * FROM records WHERE name_key = ? ORDER BY updated_at DESC LIMIT 1",
                (name_key(name),),
            ).fetchone()
        return row

    def upsert(self, name: str, state: str, notes: str | None = None) -> sqlite3.Row:
        """Record an encounter: insert, or overwrite state and bump times_seen."""
        name = normalize_name(name)
        if not name:
            raise ValueError("name is empty")
        if state not in STATES:
            raise ValueError(f"unknown state {state!r}")
        now = _now()
        existing = self.get(name)  # exact or loose match -> overwrite that record, keep its name
        if existing is not None:
            self.conn.execute(
                """
                UPDATE records
                   SET state = ?, notes = COALESCE(?, notes), times_seen = times_seen + 1, updated_at = ?
                 WHERE id = ?
                """,
                (state, notes, now, existing["id"]),
            )
            self.conn.commit()
            return self.get(existing["name"], loose=False)
        self.conn.execute(
            """
            INSERT INTO records (name, state, notes, times_seen, created_at, updated_at, name_key)
            VALUES (?, ?, ?, 1, ?, ?, ?)
            """,
            (name, state, notes or "", now, now, name_key(name)),
        )
        self.conn.commit()
        return self.get(name, loose=False)

    def set_state(self, name: str, state: str) -> None:
        """Change the state of an existing record without counting an encounter."""
        if state not in STATES:
            raise ValueError(f"unknown state {state!r}")
        self.conn.execute(
            "UPDATE records SET state = ?, updated_at = ? WHERE name = ? COLLATE NOCASE",
            (state, _now(), normalize_name(name)),
        )
        self.conn.commit()

    def all(self, query: str = "", state: str | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM records"
        where, params = [], []
        if query:
            where.append("name LIKE ? COLLATE NOCASE")
            params.append(f"%{query}%")
        if state:
            where.append("state = ?")
            params.append(state)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY updated_at DESC"
        return self.conn.execute(sql, params).fetchall()

    def delete(self, name: str) -> None:
        self.conn.execute(
            "DELETE FROM records WHERE name = ? COLLATE NOCASE", (normalize_name(name),)
        )
        self.conn.commit()

    def merge_records(self, records: Iterable[dict]) -> dict[str, int]:
        """Merge exported/imported rows. Keys: name, state, times_seen, first_seen|created_at,
        last_updated|updated_at, notes. Newer state wins, times_seen keeps the max, dates widen."""
        added = updated = unchanged = skipped = 0
        for rec in records:
            name = normalize_name(str(rec.get("name") or ""))
            state = str(rec.get("state") or "").strip().lower()
            if not name or state not in STATES:
                skipped += 1
                continue
            try:
                seen = max(1, int(rec.get("times_seen") or 1))
            except (TypeError, ValueError):
                seen = 1
            first = str(rec.get("first_seen") or rec.get("created_at") or _now())
            last = str(rec.get("last_updated") or rec.get("updated_at") or first)
            notes = str(rec.get("notes") or "")
            existing = self.get(name)
            if existing is None:
                self.conn.execute(
                    "INSERT INTO records (name, state, notes, times_seen, created_at, updated_at, name_key)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (name, state, notes, seen, first, last, name_key(name)),
                )
                added += 1
                continue
            merged = (
                state if last > existing["updated_at"] else existing["state"],
                notes or existing["notes"],
                max(seen, existing["times_seen"]),
                min(first, existing["created_at"]),
                max(last, existing["updated_at"]),
            )
            current = (existing["state"], existing["notes"], existing["times_seen"], existing["created_at"], existing["updated_at"])
            if merged == current:
                unchanged += 1
                continue
            self.conn.execute(
                "UPDATE records SET state = ?, notes = ?, times_seen = ?, created_at = ?, updated_at = ? WHERE id = ?",
                (*merged, existing["id"]),
            )
            updated += 1
        self.conn.commit()
        return {"added": added, "updated": updated, "unchanged": unchanged, "skipped": skipped}

    def counts(self) -> dict[str, int]:
        rows = self.conn.execute("SELECT state, COUNT(*) AS n FROM records GROUP BY state")
        return {r["state"]: r["n"] for r in rows}

    # ----------------------------------------------------------------- settings
    def get_setting(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def all_settings(self) -> dict[str, str]:
        return {r["key"]: r["value"] for r in self.conn.execute("SELECT key, value FROM settings")}

    def set_setting(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )
        self.conn.commit()
