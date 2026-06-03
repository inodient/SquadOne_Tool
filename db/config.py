"""DB/Qdrant 접속 설정 — 프로젝트 .env 로딩 + psycopg3 DSN.

자격증명은 SquadOne_Tool 루트 .env의 POSTGRES_* / QDRANT_* 변수를 사용한다.
- 로딩 경로는 환경변수 SQUADONE_ENV_FILE로 주입(기본값 프로젝트 루트 .env).
- 이미 export된 변수는 덮어쓰지 않는다(override=False).
- 자격증명은 코드에 하드코딩하지 않는다.
"""

from __future__ import annotations

import os
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - 설치 전 폴백
    load_dotenv = None

# 본 프로젝트 격리 스키마
SCHEMA = "newstrend"

# 기본 .env 경로: SquadOne_Tool 프로젝트 루트
_DEFAULT_ENV = Path(__file__).resolve().parent.parent / ".env"

_loaded = False


def load_shared_env() -> None:
    """공유 .env를 1회 로딩(멱등). 이미 설정된 환경변수는 보존."""
    global _loaded
    if _loaded:
        return
    env_path = Path(os.environ.get("SQUADONE_ENV_FILE", str(_DEFAULT_ENV)))
    if load_dotenv is not None and env_path.is_file():
        load_dotenv(dotenv_path=str(env_path), override=False)
    _loaded = True


def pg_dsn() -> str:
    """psycopg3용 동기 DSN. SquadOne_AI _dsn()과 동일 규약."""
    load_shared_env()
    user = os.environ["POSTGRES_USER"]
    password = os.environ["POSTGRES_PASSWORD"]
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = os.environ.get("POSTGRES_PORT", "5432")
    db = os.environ["POSTGRES_DB"]
    # TCP keepalive: trend 단계처럼 수십 분간 DB 쓰기가 없을 때 원격/Tailscale 이
    # 유휴 커넥션을 끊어 'server closed the connection unexpectedly' 가 나는 것을 방지.
    ka = "keepalives=1&keepalives_idle=30&keepalives_interval=10&keepalives_count=5"
    return f"postgresql://{user}:{password}@{host}:{port}/{db}?{ka}"


def pg_connect_kwargs() -> dict:
    """psycopg.connect()용 kwargs. search_path를 newstrend로 격리."""
    return {
        "conninfo": pg_dsn(),
        "options": f"-c search_path={SCHEMA},public",
    }


def qdrant_url() -> str:
    load_shared_env()
    host = os.environ.get("QDRANT_HOST", "localhost")
    port = os.environ.get("QDRANT_PORT", "6333")
    return f"http://{host}:{port}"
