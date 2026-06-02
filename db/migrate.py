"""마이그레이션 러너 — db/migrations/*.sql 을 번호순 적용(멱등).

사용:
    python -m db.migrate           # 모든 마이그레이션 적용
    python -m db.migrate --check   # 접속 확인 + newstrend 객체 목록만 출력

기존 가동 DB(docker-entrypoint-initdb.d 초기화 이미 끝남)에 수동 적용하기 위한 용도.
모든 SQL은 IF NOT EXISTS 멱등이라 반복 실행 안전.
"""

from __future__ import annotations

import sys
from pathlib import Path

import psycopg

from db.config import pg_connect_kwargs

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def _connect() -> psycopg.Connection:
    kwargs = pg_connect_kwargs()
    # DDL은 autocommit으로 각 파일 단위 적용
    return psycopg.connect(kwargs["conninfo"], options=kwargs["options"], autocommit=True)


def apply_all() -> None:
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not files:
        print(f"[migrate] 마이그레이션 파일 없음: {MIGRATIONS_DIR}")
        return
    with _connect() as conn:
        for f in files:
            sql = f.read_text(encoding="utf-8")
            print(f"[migrate] 적용: {f.name}")
            conn.execute(sql)
    print(f"[migrate] 완료 ({len(files)}개 파일)")


def check() -> None:
    with _connect() as conn:
        ver = conn.execute("SELECT 1").fetchone()[0]
        print(f"[migrate] 접속 OK (SELECT 1 -> {ver})")
        rows = conn.execute(
            "SELECT table_type, table_name FROM information_schema.tables "
            "WHERE table_schema = 'newstrend' "
            "UNION ALL "
            "SELECT 'MATVIEW', matviewname FROM pg_matviews WHERE schemaname = 'newstrend' "
            "ORDER BY 1, 2"
        ).fetchall()
        print(f"[migrate] newstrend 객체 {len(rows)}개:")
        for t, n in rows:
            print(f"  - {t:12} {n}")


def main() -> None:
    if "--check" in sys.argv:
        check()
    else:
        apply_all()


if __name__ == "__main__":
    main()
