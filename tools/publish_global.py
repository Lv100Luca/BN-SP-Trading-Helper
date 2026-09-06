#!/usr/bin/env python3
"""Upload a Trade Check export to the global table server (see server/README.md).

    python tools/publish_global.py https://table.example.com records.json [--token TOKEN]

Accepts the same files the app's Import understands (.json, .csv or a records.sqlite). The
upload replaces the server table with the file's contents.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.export import read_records  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url", help="server base URL, e.g. https://table.example.com")
    ap.add_argument("file", help="records export (.json / .csv / .sqlite)")
    ap.add_argument("--token", default=os.environ.get("TRADECHECK_ADMIN_TOKEN", ""),
                    help="admin token (default: TRADECHECK_ADMIN_TOKEN env var)")
    args = ap.parse_args()
    if not args.token:
        print("no admin token: pass --token or set TRADECHECK_ADMIN_TOKEN", file=sys.stderr)
        return 2

    records = read_records(args.file)
    body = json.dumps({"records": records}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        args.url.rstrip("/") + "/v1/records", data=body, method="PUT",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {args.token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        print(f"server answered {exc.code}: {exc.read().decode('utf-8', 'replace')}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, OSError) as exc:
        print(f"could not reach {args.url}: {exc}", file=sys.stderr)
        return 1
    print(f"uploaded {len(records)} rows -> table v{result['version']} with {result['count']} names "
          f"(added {result['added']}, updated {result['updated']}, removed {result['removed']}, "
          f"skipped {result['skipped']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
