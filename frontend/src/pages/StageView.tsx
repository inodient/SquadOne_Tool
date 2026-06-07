import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { useWeek } from "../week";
import { STAGE_BY_SLUG } from "../stages";
import RawTable from "../components/RawTable";
import { api, type RerunJob } from "../api/client";
import { useAsync } from "../hooks";
import { Empty } from "../components/ui";

/** 단계 페이지 — raw 출력 + 키워드 제외 + 단계 재실행(제외/기간 반영). */
export default function StageView() {
  const { slug = "1" } = useParams();
  const { week, weeks } = useWeek();
  const stage = STAGE_BY_SLUG[slug];

  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [refreshKey, setRefreshKey] = useState(0);
  const [weekFrom, setWeekFrom] = useState<string>("");
  const [weekTo, setWeekTo] = useState<string>("");
  const [busy, setBusy] = useState(false);
  const [rerun, setRerun] = useState<RerunJob | null>(null);
  const [msg, setMsg] = useState<string>("");

  const exclusions = useAsync(() => api.exclusions(), [refreshKey]);

  // 단계/주차 전환 시 선택 초기화
  useEffect(() => {
    setSelected(new Set());
    setMsg("");
  }, [slug, week]);

  if (!stage) return <Empty>알 수 없는 단계입니다.</Empty>;

  const toggle = (kw: string) =>
    setSelected((s) => {
      const n = new Set(s);
      n.has(kw) ? n.delete(kw) : n.add(kw);
      return n;
    });
  const toggleMany = (kws: string[], checked: boolean) =>
    setSelected((s) => {
      const n = new Set(s);
      kws.forEach((k) => (checked ? n.add(k) : n.delete(k)));
      return n;
    });

  const doExclude = async () => {
    const ks = Array.from(selected);
    if (!ks.length) return;
    setBusy(true);
    setMsg("");
    try {
      const r = await api.addExclusions(ks);
      setMsg(`제외 플래그 ${r.added}건 저장(원천 데이터 보존). 분석 단계(6·7·8) 재실행 시 반영.`);
      setSelected(new Set());
      setRefreshKey((k) => k + 1);
    } catch (e) {
      setMsg("제외 실패: " + (e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const doRestore = async (kw: string) => {
    try {
      await api.removeExclusions([kw]);
      setRefreshKey((k) => k + 1);
    } catch (e) {
      setMsg("복원 실패: " + (e as Error).message);
    }
  };

  const pollRerun = (w: string) => {
    api
      .rerunStatus(w)
      .then((j) => {
        setRerun(j);
        if (j.status === "running") {
          setTimeout(() => pollRerun(w), 1500);
        } else {
          setBusy(false);
          if (j.status === "success") setRefreshKey((k) => k + 1);
        }
      })
      .catch(() => setBusy(false));
  };

  const doRerun = async () => {
    if (!week) return;
    setBusy(true);
    setMsg("");
    setRerun({ week, status: "running", message: "재실행 시작" });
    try {
      await api.rerun(stage.n, week, weekFrom || undefined, weekTo || undefined);
      pollRerun(week);
    } catch (e) {
      setBusy(false);
      setMsg("재실행 실패: " + (e as Error).message);
    }
  };

  const excludedList = exclusions.data?.exclusions ?? [];

  return (
    <div className="stage-view">
      <div className="stage-title">
        <h2>{stage.n}단계 · {stage.title.replace(/^\d+\s/, "")}</h2>
        <p className="stage-desc">{stage.desc}</p>
      </div>

      {/* 툴바: 제외 / 기간 / 재실행 */}
      <div className="stage-toolbar">
        <button className="btn danger" disabled={busy || selected.size === 0} onClick={doExclude}>
          선택 {selected.size}개 제외
        </button>
        <span className="tb-sep" />
        <label className="tb-label">기간(재실행)</label>
        <select value={weekFrom} onChange={(e) => setWeekFrom(e.target.value)}>
          <option value="">{week ?? "주차"}(기본)</option>
          {[...weeks].reverse().map((w) => (
            <option key={w} value={w}>{w}</option>
          ))}
        </select>
        <span>~</span>
        <select value={weekTo} onChange={(e) => setWeekTo(e.target.value)}>
          <option value="">{week ?? "주차"}(기본)</option>
          {[...weeks].reverse().map((w) => (
            <option key={w} value={w}>{w}</option>
          ))}
        </select>
        <button className="btn" disabled={busy} onClick={doRerun}>
          {busy && rerun?.status === "running" ? "재실행 중…" : `${stage.n}단계 재실행`}
        </button>
        {rerun && rerun.status !== "idle" && (
          <span className={"rerun-status " + rerun.status}>
            {rerun.status === "running" ? "⏳ " : rerun.status === "success" ? "✅ " : "❌ "}
            {rerun.message}{rerun.error ? ` — ${rerun.error}` : ""}
            {rerun.elapsed ? ` (${rerun.elapsed}s)` : ""}
          </span>
        )}
      </div>
      {msg && <div className="stage-msg">{msg}</div>}

      {/* 현재 제외 목록 */}
      {excludedList.length > 0 && (
        <details className="excl-panel">
          <summary>제외된 키워드 {excludedList.length}개 (클릭하여 보기/복원)</summary>
          <div className="excl-chips">
            {excludedList.map((e) => (
              <span key={e.keyword} className="excl-chip">
                {e.keyword}
                <button title="복원" onClick={() => doRestore(e.keyword)}>×</button>
              </span>
            ))}
          </div>
        </details>
      )}

      {stage.tables.map((t) => (
        <RawTable
          key={t.name}
          table={t.name}
          label={t.label}
          note={t.note}
          week={week}
          refreshKey={refreshKey}
          selected={selected}
          onToggle={toggle}
          onToggleMany={toggleMany}
        />
      ))}
    </div>
  );
}
