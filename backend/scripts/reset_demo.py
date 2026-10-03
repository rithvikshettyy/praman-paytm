"""Wipe the demo store and seed the Part 9 example cases again. Takes a second or two.

    python scripts/reset_demo.py        (from backend/)

Deletes every case in the MongoDB database MONGO_DB (default praman) at MONGO_URI,
including any made live during a rehearsal, and every kept original. Then seeds and
prints the console headline. A running server needs no restart.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, console, store  # noqa: E402
from scripts import seed_demo  # noqa: E402


def main() -> int:
    started = time.perf_counter()
    conn = store.connect()
    try:
        wiped = store.wipe_all(conn)
        seed_demo.seed(conn)
        headline = console.metrics(conn)["headline"]["text"]
    finally:
        conn.close()
    print(f"wiped {wiped} case(s) from MongoDB database {config.MONGO_DB}")
    print(f"seeded {', '.join(seed_demo.EXAMPLES)}")
    print(f"console: {headline}")
    print(f"done in {time.perf_counter() - started:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
