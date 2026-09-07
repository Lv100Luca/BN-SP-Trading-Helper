"""SQLite storage for player records and app settings."""
from __future__ import annotations

import json
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
CREATE TABLE IF NOT EXISTS readings (
    id           INTEGER PRIMARY KEY,
    ts           TEXT NOT NULL,
    image        TEXT NOT NULL DEFAULT '',   -- file name inside the readings folder (see app/readings.py)
    engine       TEXT NOT NULL DEFAULT '',
    read_name    TEXT NOT NULL DEFAULT '',   -- what OCR produced ('' = nothing readable)
    confidence   REAL NOT NULL DEFAULT 0,
    alternatives TEXT NOT NULL DEFAULT '[]', -- JSON [[text, confidence], ...] of every row OCR found
    region       TEXT NOT NULL DEFAULT '',   -- JSON [x, y, w, h] of the capture region at the time
    preprocess   TEXT NOT NULL DEFAULT '',   -- JSON PreprocessConfig used for this read
    source       TEXT NOT NULL DEFAULT '',   -- 'manual' (button / F5) or 'auto'
    saved_name   TEXT NOT NULL DEFAULT '',   -- record name a state was saved under from this reading
    saved_state  TEXT NOT NULL DEFAULT '',
    fixed_name   TEXT NOT NULL DEFAULT '',   -- the name a human confirmed ('' = unchecked)
    fixed_at     TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS global_records (   -- read-only copy of the shared table (see app/sync.py)
    name       TEXT PRIMARY KEY COLLATE NOCASE,
    name_key   TEXT NOT NULL,
    state      TEXT NOT NULL,
    notes      TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_global_key ON global_records(name_key);
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

    def lookup_preference(self) -> str:
        """Which source wins when a name is in both tables: "local" (default) or "global"."""
        return "global" if self.get_setting("lookup_prefer") == "global" else "local"

    def find_both(self, name: str) -> tuple[sqlite3.Row | None, sqlite3.Row | None]:
        return self.get(name), self.get_global(name)

    def find(self, name: str, prefer: str | None = None) -> tuple[sqlite3.Row | None, str]:
        """The record from the preferred source, the other one as fallback.
        Returns (row, "local" | "global" | "")."""
        local, glob = self.find_both(name)
        prefer = prefer or self.lookup_preference()
        order = ((glob, "global"), (local, "local")) if prefer == "global" else ((local, "local"), (glob, "global"))
        for rec, source in order:
            if rec is not None:
                return rec, source
        return None, ""

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

    # Every row carries a `source` column. Global rows have no encounter count (times_seen = 0)
    # and no first-seen time (created_at = updated_at), so exports and the list treat them alike.
    _LOCAL_SQL = "SELECT id, name, state, notes, times_seen, created_at, updated_at, 'local' AS source FROM records"
    _GLOBAL_SQL = ("SELECT NULL AS id, name, state, notes, 0 AS times_seen, updated_at AS created_at, updated_at, "
                   "'global' AS source FROM global_records")
    SOURCES = ("both", "local", "global")

    def all(self, query: str = "", state: str | None = None, source: str = "local") -> list[sqlite3.Row]:
        """Records from your own table, the shared global table, or both unified."""
        if source not in self.SOURCES:
            raise ValueError(f"unknown source {source!r}")
        inner = {"local": self._LOCAL_SQL, "global": self._GLOBAL_SQL,
                 "both": f"{self._LOCAL_SQL} UNION ALL {self._GLOBAL_SQL}"}[source]
        sql = f"SELECT * FROM ({inner})"
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

    def rename_record(self, old: str, new: str) -> dict:
        """Give the record `old` the name `new` (used to repair a state saved under an OCR misread).

        If another record already answers to `new` (exact or loose match, like upsert), the two
        are merged into one named `new`: newer state wins, times_seen add up, dates widen, notes
        are joined. Readings that were saved under `old` follow the record. Returns
        {"action": "renamed" | "merged" | "unchanged", "old": ..., "name": ...}.
        """
        old, new = normalize_name(old), normalize_name(new)
        if not new:
            raise ValueError("name is empty")
        src = self.get(old, loose=False)
        if src is None:
            raise LookupError(f"no record named {old!r}")
        dst = self.get(new)
        if dst is None or dst["id"] == src["id"]:
            if src["name"] == new:
                action = "unchanged"
            else:
                self.conn.execute(
                    "UPDATE records SET name = ?, name_key = ? WHERE id = ?", (new, name_key(new), src["id"])
                )
                action = "renamed"
        else:
            newer = src if src["updated_at"] >= dst["updated_at"] else dst  # tie: the record being fixed
            notes = [n for n in (dst["notes"], src["notes"]) if n]
            if len(notes) == 2 and notes[0] == notes[1]:
                notes = notes[:1]
            self.conn.execute("DELETE FROM records WHERE id = ?", (src["id"],))
            self.conn.execute(
                "UPDATE records SET name = ?, name_key = ?, state = ?, notes = ?, times_seen = ?,"
                " created_at = ?, updated_at = ? WHERE id = ?",
                (
                    new, name_key(new), newer["state"], " | ".join(notes),
                    src["times_seen"] + dst["times_seen"],
                    min(src["created_at"], dst["created_at"]), max(src["updated_at"], dst["updated_at"]),
                    dst["id"],
                ),
            )
            action = "merged"
        self.conn.execute(
            "UPDATE readings SET saved_name = ? WHERE saved_name = ? COLLATE NOCASE", (new, src["name"])
        )
        self.conn.commit()
        return {"action": action, "old": src["name"], "name": new}

    def counts(self, source: str = "local") -> dict[str, int]:
        table = "global_records" if source == "global" else "records"
        rows = self.conn.execute(f"SELECT state, COUNT(*) AS n FROM {table} GROUP BY state")
        return {r["state"]: r["n"] for r in rows}

    # ------------------------------------------------------------- global table
    # A copy of the shared read-only table the server hands out (app/sync.py fetches it; only
    # replace_global() writes here). find() consults it before or after your own records
    # depending on the lookup preference.
    def get_global(self, name: str) -> sqlite3.Row | None:
        name = normalize_name(name)
        if not name:
            return None
        row = self.conn.execute(
            "SELECT * FROM global_records WHERE name = ? COLLATE NOCASE", (name,)
        ).fetchone()
        if row is None and name_key(name):
            row = self.conn.execute(
                "SELECT * FROM global_records WHERE name_key = ? ORDER BY updated_at DESC LIMIT 1",
                (name_key(name),),
            ).fetchone()
        return row

    def replace_global(self, records: Iterable[dict], version: int, updated_at: str, etag: str = "") -> int:
        """Swap in a freshly downloaded table. Returns the number of rows kept."""
        rows = []
        for r in records:
            name = normalize_name(str(r.get("name", "")))
            state = str(r.get("state", "")).lower()
            if name and state in STATES:
                rows.append((name, name_key(name), state, str(r.get("notes") or ""), str(r.get("updated_at") or "")))
        with self.conn:  # one transaction: readers never see an empty table
            self.conn.execute("DELETE FROM global_records")
            self.conn.executemany(
                "INSERT OR REPLACE INTO global_records (name, name_key, state, notes, updated_at) VALUES (?, ?, ?, ?, ?)",
                rows,
            )
            for key, value in (("global_version", version), ("global_updated_at", updated_at),
                               ("global_etag", etag), ("global_synced_at", _now())):
                self.conn.execute(
                    "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, str(value)),
                )
        return len(rows)

    def touch_global(self) -> None:
        """The server said our copy is still current: only bump the sync time."""
        self.set_setting("global_synced_at", _now())

    def global_info(self) -> dict:
        count = self.conn.execute("SELECT COUNT(*) FROM global_records").fetchone()[0]
        return {
            "count": count,
            "version": int(self.get_setting("global_version", "0") or 0),
            "updated_at": self.get_setting("global_updated_at", "") or "",
            "synced_at": self.get_setting("global_synced_at", "") or "",
            "etag": self.get_setting("global_etag", "") or "",
        }

    # ----------------------------------------------------------------- readings
    # One row per capture the app took a name from; the image itself lives in a folder next to
    # the database (app/readings.py owns the files, this is just the metadata).
    def add_reading(self, *, engine: str, read_name: str, confidence: float, alternatives: list,
                    region, preprocess: dict, source: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO readings (ts, engine, read_name, confidence, alternatives, region, preprocess, source)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (_now(), engine, read_name, float(confidence), json.dumps(alternatives, ensure_ascii=False),
             json.dumps(list(region)) if region else "", json.dumps(preprocess or {}), source),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def set_reading_image(self, rid: int, image: str) -> None:
        self.conn.execute("UPDATE readings SET image = ? WHERE id = ?", (image, rid))
        self.conn.commit()

    def mark_reading_saved(self, rid: int, saved_name: str, saved_state: str) -> None:
        self.conn.execute(
            "UPDATE readings SET saved_name = ?, saved_state = ? WHERE id = ?", (saved_name, saved_state, rid)
        )
        self.conn.commit()

    def fix_reading(self, rid: int, fixed_name: str) -> None:
        """Store the human-verified name (equal to read_name = 'the read was right')."""
        self.conn.execute(
            "UPDATE readings SET fixed_name = ?, fixed_at = ? WHERE id = ?", (normalize_name(fixed_name), _now(), rid)
        )
        self.conn.commit()

    def reading(self, rid: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM readings WHERE id = ?", (rid,)).fetchone()

    def readings(self, query: str = "", limit: int = 1000) -> list[sqlite3.Row]:
        """Newest first. `query` matches the read, saved or fixed name."""
        sql, params = "SELECT * FROM readings", []
        if query:
            sql += " WHERE read_name LIKE ? COLLATE NOCASE OR saved_name LIKE ? COLLATE NOCASE OR fixed_name LIKE ? COLLATE NOCASE"
            params = [f"%{query}%"] * 3
        sql += " ORDER BY id DESC LIMIT ?"
        return self.conn.execute(sql, [*params, int(limit)]).fetchall()

    def delete_readings(self, ids: Iterable[int]) -> list[str]:
        """Remove rows; returns their image file names so the caller can delete the files."""
        ids = [int(i) for i in ids]
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        images = [r["image"] for r in self.conn.execute(f"SELECT image FROM readings WHERE id IN ({marks})", ids)]
        self.conn.execute(f"DELETE FROM readings WHERE id IN ({marks})", ids)
        self.conn.commit()
        return [i for i in images if i]

    def stale_reading_ids(self, keep: int) -> list[int]:
        """Ids of the oldest readings beyond the newest `keep` that nobody saved a state from or
        checked -- the ones that can be thrown away to bound disk use."""
        rows = self.conn.execute(
            "SELECT id FROM readings WHERE saved_name = '' AND fixed_name = '' ORDER BY id DESC LIMIT -1 OFFSET ?",
            (int(keep),),
        ).fetchall()
        return [r["id"] for r in rows]

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
