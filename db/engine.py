"""psycopg3 연결 풀 — 싱글톤.

search_path는 db.config.pg_connect_kwargs()에서 newstrend로 격리한다.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator, Optional

import psycopg
from psycopg_pool import ConnectionPool

from db.config import pg_connect_kwargs

_pool: Optional[ConnectionPool] = None


def get_pool(min_size: int = 1, max_size: int = 5) -> ConnectionPool:
    """공유 연결 풀 반환(최초 호출 시 생성)."""
    global _pool
    if _pool is None:
        kwargs = pg_connect_kwargs()
        _pool = ConnectionPool(
            conninfo=kwargs["conninfo"],
            min_size=min_size,
            max_size=max_size,
            kwargs={"options": kwargs["options"]},
            # 체크아웃 시 죽은 커넥션을 검증·교체(SELECT 1). 장시간 유휴 후에도 살아있는 커넥션 보장.
            check=ConnectionPool.check_connection,
            max_idle=300.0,
            open=True,
        )
    return _pool


@contextmanager
def get_conn() -> Iterator[psycopg.Connection]:
    """풀에서 커넥션을 빌려 컨텍스트로 제공(반납은 자동)."""
    pool = get_pool()
    with pool.connection() as conn:
        yield conn


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None
