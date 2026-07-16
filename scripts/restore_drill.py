#!/usr/bin/env python3
"""Production-sized backup/restore drill.

Seeds a scratch database with ``seed_demo --works 96``, backs it up, restores
into a second scratch database, runs migrate/check/pytest smoke, and prints a
summary line for docs/runbooks/disaster-recovery.md.

Usage (from repo root, with Postgres reachable):

    set DATABASE_URL=postgres://elibrary:elibrary@localhost:5432/postgres
    python scripts/restore_drill.py

Requires ``pg_dump`` and ``pg_restore`` on PATH (PostgreSQL client tools).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse, urlunparse

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKUP_DIR = REPO_ROOT / "backups"
SRC_DB = "elibrary_drill_src"
RESTORE_DB = "elibrary_drill_restore"
SMOKE_TESTS = [
    "library/tests/test_services.py",
    "library/tests/test_billing.py",
    "library/tests/test_onboarding.py",
]


def _admin_url(base_url: str, database: str) -> str:
    parsed = urlparse(base_url)
    return urlunparse(parsed._replace(path=f"/{database}"))


def _run(cmd: list[str], *, env: dict | None = None, cwd: Path | None = None) -> None:
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True, env=env, cwd=cwd or REPO_ROOT)


def _psql(sql: str, admin_url: str) -> None:
    _run(["psql", admin_url, "-v", "ON_ERROR_STOP=1", "-c", sql])


def _drop_create_db(name: str, admin_url: str) -> None:
    _psql(
        f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
        f"WHERE datname = '{name}' AND pid <> pg_backend_pid();",
        admin_url,
    )
    _psql(f'DROP DATABASE IF EXISTS "{name}";', admin_url)
    _psql(f'CREATE DATABASE "{name}";', admin_url)


def _count_rows(db_url: str) -> dict[str, int]:
    import psycopg

    with psycopg.connect(db_url) as conn, conn.cursor() as cur:
        counts = {}
        for table in ("library_work", "library_copy", "library_patronprofile", "library_loan"):
            cur.execute(f"SELECT COUNT(*) FROM {table}")
            counts[table] = cur.fetchone()[0]
        return counts


def main() -> int:
    base_url = os.environ.get(
        "DATABASE_URL", "postgres://elibrary:elibrary@localhost:5432/postgres"
    )
    admin_url = _admin_url(base_url, "postgres")
    src_url = _admin_url(base_url, SRC_DB)
    restore_url = _admin_url(base_url, RESTORE_DB)

    if not shutil.which("pg_dump") or not shutil.which("pg_restore"):
        print("pg_dump/pg_restore not found on PATH", file=sys.stderr)
        return 1

    python = sys.executable
    manage = [python, "manage.py"]
    env_base = {**os.environ, "SECRET_KEY": os.environ.get("SECRET_KEY", "drill-insecure-key"), "DEBUG": "False"}

    print("=== 1. Prepare source database with production-sized seed ===")
    _drop_create_db(SRC_DB, admin_url)
    env_src = {**env_base, "DATABASE_URL": src_url}
    _run(manage + ["migrate", "--noinput"], env=env_src)
    _run(manage + ["seed_demo", "--works", "96"], env=env_src)
    counts = _count_rows(src_url)
    print("row_counts", counts)

    print("=== 2. Backup ===")
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    dump_path = BACKUP_DIR / f"elibrary-drill-{stamp}.dump"
    t0 = time.perf_counter()
    _run(
        ["pg_dump", "--format=custom", "--no-owner", f"--dbname={src_url}", f"--file={dump_path}"],
        env=env_src,
    )

    print("=== 3. Restore into scratch database ===")
    _drop_create_db(RESTORE_DB, admin_url)
    _run(
        ["pg_restore", "--clean", "--if-exists", "--no-owner", f"--dbname={restore_url}", str(dump_path)],
        env=env_src,
    )
    restore_seconds = round(time.perf_counter() - t0, 1)

    print("=== 4. Post-restore migrate, check, pytest smoke ===")
    env_restore = {**env_base, "DATABASE_URL": restore_url}
    _run(manage + ["migrate", "--noinput"], env=env_restore)
    _run(manage + ["check"], env=env_restore)
    _run(
        [python, "-m", "pytest", *SMOKE_TESTS, "-q", "--tb=line"],
        env=env_restore,
    )
    restored_counts = _count_rows(restore_url)

    print("=== DRILL PASS ===")
    print(
        f"stamp={stamp} restore_seconds={restore_seconds} "
        f"works={restored_counts.get('library_work', 0)} "
        f"copies={restored_counts.get('library_copy', 0)} "
        f"patrons={restored_counts.get('library_patronprofile', 0)} "
        f"loans={restored_counts.get('library_loan', 0)} "
        f"dump={dump_path.name}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
