import sys
import threading
import time

sys.path.insert(0, sys.argv[1])

import psycopg2

from scripts.swarm_ledger.repository import postgres, sqlite

URL = sys.argv[2]
LIMIT = 90


def idle_holders():
    with psycopg2.connect(URL) as watcher, watcher.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM pg_stat_activity WHERE state = 'idle in transaction'"
            " AND query LIKE 'SELECT pg_advisory_xact_lock%'"
        )
        return cursor.fetchone()[0]


holder = postgres.Connection(URL)
holder.execute(sqlite.BEGIN_IMMEDIATE)
start = time.monotonic()
result = {}


def write():
    try:
        writer = postgres.Connection(URL)
        with writer:
            writer.execute(sqlite.BEGIN_IMMEDIATE)
            writer.execute("SELECT 1")
        result["written_after_s"] = round(time.monotonic() - start, 1)
    except Exception as exc:
        result["error"] = repr(exc)


thread = threading.Thread(target=write, daemon=True)
thread.start()
time.sleep(5)
print(f"idle writer lock holders at 5s: {idle_holders()}", flush=True)
thread.join(LIMIT)
print(f"idle writer lock holders at end: {idle_holders()}", flush=True)
if "written_after_s" in result:
    print(f"PASS second writer committed after {result['written_after_s']}s; idle holder ended by the server", flush=True)
    sys.exit(0)
print(f"FAIL second writer {result.get('error') or f'still waiting after {LIMIT}s behind an idle holder'}", flush=True)
sys.exit(1)
