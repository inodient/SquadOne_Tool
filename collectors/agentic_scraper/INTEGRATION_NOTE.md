# Agentic Scraper — 통합 노트 (Stage0 대안 수집기)

출처: `SquadOne_Tool_Agentic_Scrapper` 를 SquadOne_Tool 로 격리 이식(P5).
ReAct(LangGraph) + Playwright 기반 범용 웹 수집기 + 2-tier 셀렉터 캐시 + MCP.

## 위치/성격
- 본 디렉터리는 **자립형 서브시스템**이다. 내부 절대 임포트(`core.config`,
  `react_agent.tools`, `agents.shared.llm` 등)를 쓰므로 **이 디렉터리를 cwd 로** 실행한다.
  메인 패키지(`steps`, `db`)와 임포트 경계를 공유하지 않는다(의도적 격리).

## 실행
```bash
cd collectors/agentic_scraper
# 1) 의존성(루트 requirements.txt 에 langgraph/langchain-*/playwright 포함)
python -m playwright install chromium    # 최초 1회
# 2) ReAct 단건 수집
python main.py --react --url <URL> --objective "<수집 목표>"
# 3) 배치
python main.py --batch urls.txt --objective "<목표>"
# 4) MCP 서버(stdio / http)
python mcp_server.py
python http_mcp_server.py
```

## 환경변수(.env)
- 루트 통합 `.env` 와 동일 키 사용(LLM_PROVIDER, GEMINI_API_KEY, OLLAMA_BASE_URL 등).
- 본 디렉터리에도 `.env`(gitignore)가 있으면 우선 사용한다. 없으면 `.env.example` 참고.
- 주요: `LLM_PROVIDER`, `LLM_MODEL`, `BROWSER_HEADLESS`, `REACT_MAX_STEPS`,
  `CACHE_STALE_DAYS`, `BATCH_MAX_WORKERS`.

## 파이프라인 연계
- 산출(구조화 기사/링크)을 `data/news/*.xlsx` 또는 중간 CSV 로 떨군 뒤
  `python -m steps.qdrant_ingest` 로 Qdrant 에 적재 → 기존 1~7단계로 연결.
- BigKinds(`collectors/bigkinds_downloader.py`)가 막히거나 BigKinds 외 소스가 필요할 때의
  유연 수집 대안.

## 비고
- 런타임 산출물(cache/sandbox/data)은 이식 시 제외했다(코드만).
- legacy `agents/`(planner→generator→executor→critic) 는 ReAct(`react_agent/`)로 대체됨.
  유지하되 신규 작업은 `react_agent/` 사용 권장.
