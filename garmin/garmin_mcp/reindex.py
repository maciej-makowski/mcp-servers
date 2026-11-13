#!/usr/bin/env python3
"""Rebuild the archive's index.db from the store's files (design §5.4).

The files are primary (§3.1); index.db is a derived, rebuildable index. Run:

    python reindex.py /home/mcp/garmin-store

after a restore, a repair, or any manual store surgery, BEFORE flipping
GARMIN_STORE_MODE back to read_through (the startup init checks require a
consistent index). Prints the rebuilt row counts.
"""
from __future__ import annotations

import sys

from garmin_mcp.store import Store


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1:
        print("usage: reindex.py <store_dir>", file=sys.stderr)
        return 2
    store = Store(argv[0])
    stats = store.reindex()
    print(f"reindexed {argv[0]}: {stats}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
