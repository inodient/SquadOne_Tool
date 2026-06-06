"""마이그레이션 러너 — db/migrations/*.sql 을 번호순 적용(원장 기반).

사용:
    python -m db.migrate           # 미적용 마이그레이션만 적용
    python -m db.migrate --check   # 접속 확인 + newstrend 객체 목록만 출력
    python -m db.migrate --seed    # 현재 *.sql 전부를 '적용됨'으로 원장에 기록(재적용 X)

원장(newstrend.schema_migrations)에 적용 이력을 남겨, 이미 적용된 파일은 건너뛴다.
이전에는 매 실행마다 전체를 재적용했는데, CREATE OR REPLACE VIEW 가 후속 마이그레이션
으로 컬럼이 바뀌면 재적용 시 "cannot drop columns from view" 로 깨졌다(0001↔0003 등).
원장 방식은 이를 방지하며, 신규 DB에서는 원장이 비어 0001부터 순서대로 적용된다.
모든 SQL은 IF NOT EXISTS 멱등이라 신규 적용은 안전.
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


def _ensure_ledger(conn: psycopg.Connection) -> None:
    conn.execute("CREATE SCHEMA IF NOT EXISTS newstrend")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS newstrend.schema_migrations ("
        "  filename   text PRIMARY KEY,"
        "  applied_at timestamptz NOT NULL DEFAULT now())"
    )


def _applied_set(conn: psycopg.Connection) -> set[str]:
    rows = conn.execute("SELECT filename FROM newstrend.schema_migrations").fetchall()
    return {r[0] for r in rows}


def apply_all() -> None:
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not files:
        print(f"[migrate] 마이그레이션 파일 없음: {MIGRATIONS_DIR}")
        return
    with _connect() as conn:
        _ensure_ledger(conn)
        applied = _applied_set(conn)
        pending = [f for f in files if f.name not in applied]
        if not pending:
            print(f"[migrate] 적용할 신규 마이그레이션 없음 (총 {len(files)}개 모두 적용됨)")
            return
        for f in pending:
            sql = f.read_text(encoding="utf-8")
            print(f"[migrate] 적용: {f.name}")
            conn.execute(sql)
            conn.execute(
                "INSERT INTO newstrend.schema_migrations (filename) VALUES (%s) "
                "ON CONFLICT (filename) DO NOTHING",
                (f.name,),
            )
    print(f"[migrate] 완료 (신규 {len(pending)}개 적용 / 총 {len(files)}개)")


def seed() -> None:
    """현재 디렉터리의 *.sql 전부를 '적용됨'으로 원장에 기록(SQL 재실행 X).

    이미 수동/초기화로 적용된 가동 DB에 원장을 도입할 때 1회 사용한다.
    """
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    with _connect() as conn:
        _ensure_ledger(conn)
        for f in files:
            conn.execute(
                "INSERT INTO newstrend.schema_migrations (filename) VALUES (%s) "
                "ON CONFLICT (filename) DO NOTHING",
                (f.name,),
            )
    print(f"[migrate] 원장 시드 완료 ({len(files)}개 파일을 적용됨으로 기록)")


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
    elif "--seed" in sys.argv:
        seed()
    else:
        apply_all()


if __name__ == "__main__":
    main()
