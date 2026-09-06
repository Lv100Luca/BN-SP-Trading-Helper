"""Export records to CSV or JSON."""
from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Iterable

# DB column -> exported column name
COLUMNS = {
    "name": "name",
    "state": "state",
    "times_seen": "times_seen",
    "created_at": "first_seen",
    "updated_at": "last_updated",
    "notes": "notes",
}


def rows_to_dicts(rows: Iterable) -> list[dict]:
    return [{out: row[col] for col, out in COLUMNS.items()} for row in rows]


def export_csv(rows: Iterable, path: Path) -> int:
    """UTF-8 with BOM so Excel opens non-ASCII names correctly."""
    dicts = rows_to_dicts(rows)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=list(COLUMNS.values()))
        writer.writeheader()
        writer.writerows(dicts)
    return len(dicts)


def export_json(rows: Iterable, path: Path) -> int:
    dicts = rows_to_dicts(rows)
    payload = {"exported_at": datetime.now().isoformat(timespec="seconds"), "count": len(dicts), "records": dicts}
    Path(path).write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return len(dicts)


def export_records(rows: Iterable, path: str | Path) -> int:
    """Pick the format from the file extension (.json -> JSON, anything else -> CSV). Returns the row count."""
    path = Path(path)
    if path.suffix.lower() == ".json":
        return export_json(rows, path)
    return export_csv(rows, path)


# ------------------------------------------------------------------------ import
def read_records(path: str | Path) -> list[dict]:
    """Read rows from a CSV/JSON export or another Trade Check SQLite database, as dicts for
    Database.merge_records()."""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        rows = payload.get("records", []) if isinstance(payload, dict) else payload
        return [dict(r) for r in rows]
    if suffix in (".sqlite", ".db", ".sqlite3"):
        import sqlite3

        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute("SELECT * FROM records")]
        finally:
            conn.close()
    with open(path, newline="", encoding="utf-8-sig") as f:
        return [dict(r) for r in csv.DictReader(f)]
