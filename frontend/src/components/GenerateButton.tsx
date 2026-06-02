import { useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import type { GenerateStatus } from "../api/types";

/**
 * 상품이 없는 주차에서 6~7단계(trend → product)를 온디맨드 실행하는 버튼.
 * POST /generate 로 시작 후 /generate-status 를 폴링한다. 성공 시 onDone()으로 상위 재조회.
 */
export default function GenerateButton({ week, onDone }: { week: string; onDone: () => void }) {
  const [status, setStatus] = useState<GenerateStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const timer = useRef<number | undefined>(undefined);

  const stopPoll = () => {
    if (timer.current) window.clearInterval(timer.current);
    timer.current = undefined;
  };

  const startPoll = () => {
    stopPoll();
    timer.current = window.setInterval(async () => {
      try {
        const s = await api.generateStatus(week);
        setStatus(s);
        if (s.status === "success" || s.status === "failed") {
          stopPoll();
          setBusy(false);
          if (s.status === "success") onDone();
        }
      } catch {
        /* 일시 오류는 다음 폴링에서 회복 */
      }
    }, 2500);
  };

  // 주차 진입 시 진행 중인 잡이 있으면 이어서 폴링.
  useEffect(() => {
    let alive = true;
    setStatus(null);
    setBusy(false);
    stopPoll();
    api
      .generateStatus(week)
      .then((s) => {
        if (!alive) return;
        setStatus(s);
        if (s.status === "running") {
          setBusy(true);
          startPoll();
        }
      })
      .catch(() => {});
    return () => {
      alive = false;
      stopPoll();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [week]);

  const start = async () => {
    setBusy(true);
    try {
      const r = await api.generate(week);
      setStatus(r.job);
      startPoll();
    } catch (e) {
      setStatus({ week, status: "failed", message: (e as Error).message });
      setBusy(false);
    }
  };

  const running = busy || status?.status === "running";

  return (
    <div style={{ marginTop: 16 }}>
      <button className="btn" disabled={running} onClick={start}>
        {running ? "생성 중…" : "이 주차 트렌드·상품 생성하기"}
      </button>
      <div className="muted" style={{ marginTop: 10, fontSize: 13, lineHeight: 1.6 }}>
        {running && (
          <>
            ⏳ {status?.message ?? "실행 중"} {status?.step ? `(${status.step})` : ""}
            <br />
            트렌드 추출(Qdrant 벡터검색)과 상품 추출(LLM)을 거쳐 수 분 걸릴 수 있습니다.
          </>
        )}
        {status?.status === "success" && (
          <span style={{ color: "var(--active)" }}>
            ✅ {status.message ?? "완료"} {status.elapsed ? `· ${status.elapsed}s` : ""}
          </span>
        )}
        {status?.status === "failed" && (
          <span className="error">
            ⚠️ {status.message ?? "실패"}
            {status.error ? <div style={{ marginTop: 4 }}>{status.error}</div> : null}
          </span>
        )}
        {(!status || status.status === "idle") && !running && (
          <>6~7단계(trend_extractor → product_extractor)를 이 주차에 대해 실행합니다. Qdrant·Ollama 가동이 필요합니다.</>
        )}
      </div>
    </div>
  );
}
