#!/bin/zsh
# UAT(PID) 종료를 자동 감지한 뒤 Ollama 도달성을 확인하고 6단계를 실행한다.
# 원격(.env) → 로컬(localhost) 순으로 폴백. 둘 다 불가하면 실행하지 않고 보고만 남긴다.
set -u
ROOT=/Users/inodient/Desktop/SquadOne_Tool
PY=$ROOT/.venv/bin/python
UAT_PID=83640
REMOTE=http://100.106.14.57:11434
LOCAL=http://localhost:11434
LOG=/tmp/step6_rerun.log

echo "[watcher] start $(date '+%F %T') | UAT_PID=$UAT_PID 대기" > "$LOG"

# 1) UAT 종료까지 폴링(60초 간격)
while kill -0 "$UAT_PID" 2>/dev/null; do
  sleep 60
done
echo "[watcher] UAT(PID $UAT_PID) 종료 감지 $(date '+%F %T')" >> "$LOG"

# 2) 리소스 안정 대기
sleep 30

# 3) Ollama 도달성 확인(원격 우선 → 로컬 폴백)
reach() { curl -s --max-time 8 "$1/api/tags" 2>/dev/null | grep -q "llama3.1:8b"; }
BASE=""
if reach "$REMOTE"; then
  BASE="$REMOTE"; echo "[watcher] 원격 Ollama 사용: $REMOTE" >> "$LOG"
elif reach "$LOCAL"; then
  BASE="$LOCAL"; echo "[watcher] 원격 불가 → 로컬 Ollama 폴백: $LOCAL" >> "$LOG"
else
  echo "[watcher] ABORT: 원격/로컬 Ollama 모두 llama3.1:8b 응답 없음. 6단계 실행 안 함." >> "$LOG"
  exit 3
fi

# 4) 6단계 실행 (CLI env 가 .env override=False 보다 우선)
echo "[watcher] 6단계 시작 $(date '+%F %T') | OLLAMA_BASE_URL=$BASE" >> "$LOG"
cd "$ROOT"
PYTHONPATH="$ROOT" OLLAMA_BASE_URL="$BASE" "$PY" scripts/run_step.py 6 >> "$LOG" 2>&1
echo "[watcher] 6단계 종료 EXIT=$? $(date '+%F %T')" >> "$LOG"
