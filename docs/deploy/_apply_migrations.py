"""One-shot Supabase migration runner.

Applies the 015/016/017 SQL files against the Supabase Postgres
instance using psycopg3. Each migration is its own BEGIN/COMMIT
transaction inside the SQL file, so a failure in one block doesn't
poison the others. Re-running is safe — every migration uses
``CREATE ... IF NOT EXISTS`` and ``DROP TRIGGER IF EXISTS`` style
guards.

Reads connection from ``SUPABASE_DB_URL`` env var. NEVER hard-code
the password in this file.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg


MIGRATIONS = [
    "robotis_ai_setup/supabase/015_workflow_versions.sql",
    "robotis_ai_setup/supabase/016_tutorial_progress.sql",
    "robotis_ai_setup/supabase/017_vision_quota.sql",
]


def main() -> int:
    db_url = os.environ.get("SUPABASE_DB_URL")
    if not db_url:
        print("ERROR: SUPABASE_DB_URL is not set in the environment.")
        return 2

    repo_root = Path(__file__).resolve().parents[2]
    print(f"Repo root: {repo_root}")
    print(f"Applying {len(MIGRATIONS)} migrations …")

    # autocommit=True lets each migration's BEGIN/COMMIT manage its own
    # transaction independently; psycopg's outer "implicit transaction"
    # mode would otherwise wrap everything in a second-level tx and
    # confuse the BEGIN inside the file.
    with psycopg.connect(db_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT version()")
            ver = cur.fetchone()[0]
            print(f"Connected — {ver.split(',')[0]}")

        for rel in MIGRATIONS:
            path = repo_root / rel
            print()
            print(f"=== {rel} ===")
            if not path.exists():
                print(f"  MISSING: {path}")
                return 3
            sql = path.read_text(encoding="utf-8")
            try:
                with conn.cursor() as cur:
                    cur.execute(sql)
                print(f"  OK ({len(sql)} bytes)")
            except Exception as e:
                print(f"  FAIL: {type(e).__name__}: {e}")
                return 4

        # Quick verification queries — confirm each new object exists.
        with conn.cursor() as cur:
            cur.execute("""
                SELECT tablename FROM pg_tables
                WHERE schemaname='public'
                  AND tablename IN ('workflow_versions','tutorial_progress')
                ORDER BY tablename
            """)
            tables = [r[0] for r in cur.fetchall()]
            cur.execute("""
                SELECT proname FROM pg_proc p
                JOIN pg_namespace n ON n.oid = p.pronamespace
                WHERE n.nspname='public'
                  AND proname IN ('prune_workflow_versions',
                                  'snapshot_workflow_version',
                                  'consume_vision_quota',
                                  'reset_vision_quota_used',
                                  'touch_tutorial_progress_updated_at')
                ORDER BY proname
            """)
            funcs = [r[0] for r in cur.fetchall()]
            cur.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_schema='public' AND table_name='users'
                  AND column_name IN ('vision_quota_per_term','vision_used_per_term')
                ORDER BY column_name
            """)
            cols = [r[0] for r in cur.fetchall()]

    print()
    print("=== Verification ===")
    print(f"  Tables present  : {tables}")
    print(f"  Functions present: {funcs}")
    print(f"  Columns present : users.[{', '.join(cols)}]")
    if (
        "workflow_versions" in tables
        and "tutorial_progress" in tables
        and {"prune_workflow_versions", "snapshot_workflow_version",
             "consume_vision_quota", "reset_vision_quota_used"} <= set(funcs)
        and "vision_quota_per_term" in cols
        and "vision_used_per_term" in cols
    ):
        print()
        print("ALL MIGRATIONS APPLIED + VERIFIED.")
        return 0
    print()
    print("WARN: some objects missing — inspect the output above.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
