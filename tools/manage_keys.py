#!/usr/bin/env python3
"""Manage contributor keys of the global table server
(https://github.com/Lv100Luca/BN-SP-Trading-Helper-API).

    python tools/manage_keys.py https://table.example.com list
    python tools/manage_keys.py https://table.example.com create "Alice"
    python tools/manage_keys.py https://table.example.com revoke 3

The key is printed once on create; hand it to the player, who pastes it into Settings ->
Contribute. The admin token is read, in order: --token, TRADECHECK_ADMIN_TOKEN env var, the
.admin_token file in the project root.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

TOKEN_FILE = Path(__file__).resolve().parent.parent / ".admin_token"


def call(url: str, token: str, path: str, method: str = "GET", body: dict | None = None) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url.rstrip("/") + path, data=data, method=method,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url", help="server base URL")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--token", default=os.environ.get("TRADECHECK_ADMIN_TOKEN", ""))
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", parents=[common])
    sub.add_parser("create", parents=[common]).add_argument("label", help="who gets the key")
    sub.add_parser("revoke", parents=[common]).add_argument("id", type=int, help="key id from `list`")
    args = ap.parse_args()
    if not args.token and TOKEN_FILE.exists():
        args.token = TOKEN_FILE.read_text(encoding="utf-8").strip()
    if not args.token:
        print("no admin token: pass --token, set TRADECHECK_ADMIN_TOKEN, or put it in .admin_token", file=sys.stderr)
        return 2
    try:
        if args.cmd == "list":
            keys = call(args.url, args.token, "/v1/keys")["keys"]
            if not keys:
                print("no contributor keys")
            for k in keys:
                print(f"{k['id']:>3}  {k['label']:<24} created {k['created_at'][:10]}  "
                      f"last used {k['last_used'][:16] or '-':<16}  uploads {k['uploads']}  changes {k['changes']}")
        elif args.cmd == "create":
            k = call(args.url, args.token, "/v1/keys", "POST", {"label": args.label})
            print(f"key {k['id']} for {k['label']} (shown once, not stored on the server):\n{k['key']}")
        else:
            call(args.url, args.token, f"/v1/keys/{args.id}", "DELETE")
            print(f"key {args.id} revoked")
    except urllib.error.HTTPError as exc:
        print(f"server answered {exc.code}: {exc.read().decode('utf-8', 'replace')}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, OSError) as exc:
        print(f"could not reach {args.url}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
