// 트렌드 통합 대시보드 — 좌: 주차 트렌드 랭킹 / 우: 스레드 타임라인.
// 데이터는 base 테이블 3개를 raw 로 받아 클라이언트에서 조인(백엔드 변경 불필요):
//   weekly_trend_clusters(주차 트렌드) · trend_threads(멤버) · trend_threads_meta(스레드 요약).
// 랭킹의 트렌드를 클릭하면 그 트렌드가 속한 스레드를 타임라인에서 강조한다(A반복/B진화 추적).
import { Fragment, useMemo, useState } from "react";
import { api } from "../api/client";
import { useWeek } from "../week";
import { useAsync } from "../hooks";
import { Loading, Empty, ErrorBox } from "../components/ui";

const KIND_COLOR: Record<string, string> = {
  반복: "#4f9cf9", // A: 동일 반복/지속
  진화: "#f5a623", // B: 1심→2심→3심 진화
  단발: "#9aa3b2", // 단일 주차
};

const s = (v: unknown): string => (v == null ? "" : String(v));
const num = (v: unknown): number => {
  const x = Number(v);
  return Number.isFinite(x) ? x : 0;
};
// "YYYY-Www" 정렬키(연도×100+주). 2024·2025 혼재해도 올바르게 정렬.
const weekKey = (w: string): number => {
  const m = /(\d{4})-W(\d{2})/.exec(w);
  return m ? Number(m[1]) * 100 + Number(m[2]) : 0;
};

interface Cluster {
  week: string;
  cluster_id: number;
  label: string;
  weight: number;
  size: number;
  top_keywords: string;
  rep_sentence: string;
}
interface Member {
  thread_id: number;
  week: string;
  cluster_id: number;
  link_type: string;
}
interface ThreadMeta {
  thread_id: number;
  kind: string;
  label: string;
  label_path: string;
  span_weeks: number;
  n_weeks: number;
  start_week: string;
  end_week: string;
  peak_weight: number;
  drift: number;
  top_keywords: string;
}

export default function TrendDashboard() {
  const { week, ready } = useWeek();
  const [selectedThread, setSelectedThread] = useState<number | null>(null);
  const [showSingles, setShowSingles] = useState(false);

  const state = useAsync(async () => {
    // 전부 넓은 범위로 받는다(week 컬럼 있는 표는 미지정 시 서버가 최신주차로 기본필터 → 빈 결과).
    // clusters 도 전 주차를 받아야 스레드별 주차 상세(대표문장)를 보여줄 수 있다. 랭킹은 클라이언트서 주차필터.
    const ALL_FROM = "2000-W01", ALL_TO = "2099-W52";
    const [clu, mem, meta] = await Promise.all([
      api.raw("weekly_trend_clusters", undefined, 5000, ALL_FROM, ALL_TO),
      api.raw("trend_threads", undefined, 5000, ALL_FROM, ALL_TO),
      api.raw("trend_threads_meta", undefined, 2000),
    ]);
    const clusters: Cluster[] = clu.rows.map((r) => ({
      week: s(r.week),
      cluster_id: num(r.cluster_id),
      label: s(r.label),
      weight: num(r.weight),
      size: num(r.size),
      top_keywords: s(r.top_keywords),
      rep_sentence: s(r.rep_sentence),
    }));
    const members: Member[] = mem.rows.map((r) => ({
      thread_id: num(r.thread_id),
      week: s(r.week),
      cluster_id: num(r.cluster_id),
      link_type: s(r.link_type),
    }));
    const metas: ThreadMeta[] = meta.rows.map((r) => ({
      thread_id: num(r.thread_id),
      kind: s(r.kind),
      label: s(r.label),
      label_path: s(r.label_path),
      span_weeks: num(r.span_weeks),
      n_weeks: num(r.n_weeks),
      start_week: s(r.start_week),
      end_week: s(r.end_week),
      peak_weight: num(r.peak_weight),
      drift: num(r.drift),
      top_keywords: s(r.top_keywords),
    }));
    return { clusters, members, metas };
  }, [week]);

  // 조인 인덱스: (week|cluster_id) → thread_id, thread_id → 주차별 멤버.
  const joined = useMemo(() => {
    const data = state.data;
    if (!data) return null;
    const clusterToThread = new Map<string, number>();
    const threadMembers = new Map<number, Map<string, Member>>();
    for (const m of data.members) {
      clusterToThread.set(`${m.week}|${m.cluster_id}`, m.thread_id);
      if (!threadMembers.has(m.thread_id)) threadMembers.set(m.thread_id, new Map());
      threadMembers.get(m.thread_id)!.set(m.week, m);
    }
    // (week|cluster_id) → cluster: 스레드 상세에서 주차별 라벨·대표문장 조회용.
    const clusterByKey = new Map<string, Cluster>();
    for (const c of data.clusters) clusterByKey.set(`${c.week}|${c.cluster_id}`, c);
    // 타임라인 주차 축 = 스레드 멤버에 등장한 모든 주차(정렬).
    const weekSet = new Set<string>();
    for (const m of data.members) weekSet.add(m.week);
    const axis = [...weekSet].sort((a, b) => weekKey(a) - weekKey(b));
    // 방어: 현재 멤버가 존재하는 스레드만(과거 실행의 잔존 meta 행 제외).
    const liveThreads = new Set(data.members.map((m) => m.thread_id));
    const liveMetas = data.metas.filter((m) => liveThreads.has(m.thread_id));
    // 스레드 정렬: 다주(멤버≥2) 먼저, 정점가중 내림차순.
    const metaSorted = [...liveMetas].sort((a, b) => {
      const am = a.n_weeks >= 2 ? 1 : 0;
      const bm = b.n_weeks >= 2 ? 1 : 0;
      if (am !== bm) return bm - am;
      return b.peak_weight - a.peak_weight;
    });
    const maxPeak = Math.max(1, ...liveMetas.map((m) => m.peak_weight));
    return { clusterToThread, threadMembers, axis, metaSorted, maxPeak, clusterByKey };
  }, [state.data]);

  if (!ready || state.loading) return <Loading />;
  if (state.error) return <ErrorBox message={state.error} />;
  if (!state.data || !joined) return <Empty>트렌드 데이터가 없습니다.</Empty>;

  const { clusters } = state.data;
  const { clusterToThread, threadMembers, axis, metaSorted, maxPeak, clusterByKey } = joined;

  const ranking = clusters
    .filter((c) => c.week === week)
    .sort((a, b) => b.weight - a.weight)
    .slice(0, 60);
  const selectedMeta = selectedThread != null ? metaSorted.find((m) => m.thread_id === selectedThread) : undefined;
  const maxWeight = Math.max(1, ...ranking.map((c) => c.weight));
  const threads = metaSorted.filter((m) => showSingles || m.n_weeks >= 2);

  const dotSize = (w: number): number => 9 + Math.round((w / maxPeak) * 18);

  return (
    <div>
      <div style={{ display: "flex", alignItems: "baseline", gap: 12, marginBottom: 12 }}>
        <h2 style={{ margin: 0 }}>🧵 트렌드 대시보드</h2>
        <span className="muted" style={{ fontSize: 13 }}>
          좌: {week} 주차 트렌드 랭킹 · 우: 스레드 타임라인 — 클릭하면 아래에 <b>사건 전개(라벨궤적·주차별 대표문장)</b>가 펼쳐집니다
        </span>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "minmax(320px, 0.9fr) 1.4fr", gap: 16, alignItems: "start" }}>
        {/* ── 좌: 주차 트렌드 랭킹 ── */}
        <div>
          <div style={{ fontWeight: 600, marginBottom: 8 }}>
            {week} 트렌드 랭킹 <span className="muted">(상위 {ranking.length})</span>
          </div>
          {ranking.length === 0 ? (
            <Empty>이 주차의 트렌드가 없습니다.</Empty>
          ) : (
            <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
              {ranking.map((c, i) => {
                const tid = clusterToThread.get(`${c.week}|${c.cluster_id}`);
                const meta = tid != null ? metaSorted.find((m) => m.thread_id === tid) : undefined;
                const active = tid != null && tid === selectedThread;
                return (
                  <button
                    key={c.cluster_id}
                    onClick={() => setSelectedThread(tid ?? null)}
                    title={c.rep_sentence}
                    style={{
                      textAlign: "left",
                      border: active ? "1px solid #4f9cf9" : "1px solid #e5e8ee",
                      background: active ? "#eef5ff" : "#fff",
                      borderRadius: 8,
                      padding: "8px 10px",
                      cursor: tid != null ? "pointer" : "default",
                    }}
                  >
                    <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                      <span className="muted" style={{ width: 24, fontVariantNumeric: "tabular-nums" }}>#{i + 1}</span>
                      <span style={{ fontWeight: 600, flex: 1 }}>{c.label || "(라벨없음)"}</span>
                      {meta && (
                        <span style={{ fontSize: 11, color: "#fff", background: KIND_COLOR[meta.kind] ?? "#9aa3b2", borderRadius: 6, padding: "1px 6px" }}>
                          🧵 {meta.kind}·{meta.n_weeks}주
                        </span>
                      )}
                      <span style={{ fontVariantNumeric: "tabular-nums", fontWeight: 600 }}>{c.weight}</span>
                    </div>
                    <div style={{ height: 4, background: "#eef0f4", borderRadius: 3, margin: "5px 0 4px" }}>
                      <div style={{ width: `${(c.weight / maxWeight) * 100}%`, height: "100%", background: "#4f9cf9", borderRadius: 3 }} />
                    </div>
                    {c.top_keywords && <div className="muted" style={{ fontSize: 12 }}>{c.top_keywords}</div>}
                    {c.rep_sentence && (
                      <div className="muted" style={{ fontSize: 12, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
                        “{c.rep_sentence}”
                      </div>
                    )}
                  </button>
                );
              })}
            </div>
          )}
        </div>

        {/* ── 우: 스레드 타임라인 ── */}
        <div>
          <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 8 }}>
            <div style={{ fontWeight: 600 }}>
              스레드 타임라인 <span className="muted">({axis[0]} ~ {axis[axis.length - 1]}, {threads.length}개)</span>
            </div>
            <label className="muted" style={{ fontSize: 12, display: "flex", alignItems: "center", gap: 4, cursor: "pointer" }}>
              <input type="checkbox" checked={showSingles} onChange={(e) => setShowSingles(e.target.checked)} />
              단발 포함
            </label>
            <span style={{ marginLeft: "auto", display: "flex", gap: 10, fontSize: 12 }}>
              {Object.entries(KIND_COLOR).map(([k, col]) => (
                <span key={k} style={{ display: "flex", alignItems: "center", gap: 4 }}>
                  <span style={{ width: 10, height: 10, borderRadius: "50%", background: col, display: "inline-block" }} /> {k}
                </span>
              ))}
            </span>
          </div>

          {/* 주차 헤더 */}
          <div style={{ display: "grid", gridTemplateColumns: `240px 1fr`, gap: 8, paddingBottom: 6, borderBottom: "1px solid #e5e8ee", position: "sticky", top: 0, background: "#fff" }}>
            <div className="muted" style={{ fontSize: 12 }}>스레드 (라벨 궤적)</div>
            <div style={{ display: "grid", gridTemplateColumns: `repeat(${axis.length}, 1fr)` }}>
              {axis.map((w) => (
                <div key={w} style={{ textAlign: "center", fontSize: 11, fontWeight: week === w ? 700 : 400, color: week === w ? "#4f9cf9" : "#888" }}>
                  {w.replace(/^\d{4}-/, "")}
                </div>
              ))}
            </div>
          </div>

          {/* 스레드 행 */}
          <div style={{ display: "flex", flexDirection: "column", maxHeight: "70vh", overflowY: "auto" }}>
            {threads.map((t) => {
              const mem = threadMembers.get(t.thread_id) ?? new Map<string, Member>();
              const idxs = axis.map((w, i) => (mem.has(w) ? i : -1)).filter((i) => i >= 0);
              const firstIdx = idxs.length ? idxs[0] : 0;
              const lastIdx = idxs.length ? idxs[idxs.length - 1] : 0;
              const col = KIND_COLOR[t.kind] ?? "#9aa3b2";
              const active = t.thread_id === selectedThread;
              const leftPct = ((firstIdx + 0.5) / axis.length) * 100;
              const widthPct = ((lastIdx - firstIdx) / axis.length) * 100;
              return (
                <div
                  key={t.thread_id}
                  onClick={() => setSelectedThread(t.thread_id)}
                  style={{
                    display: "grid",
                    gridTemplateColumns: `240px 1fr`,
                    gap: 8,
                    alignItems: "center",
                    padding: "6px 0",
                    borderBottom: "1px solid #f1f3f7",
                    background: active ? "#eef5ff" : "transparent",
                    cursor: "pointer",
                  }}
                >
                  <div style={{ overflow: "hidden" }}>
                    <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                      <span style={{ fontSize: 10, color: "#fff", background: col, borderRadius: 5, padding: "1px 5px", flexShrink: 0 }}>{t.kind}</span>
                      <span style={{ fontWeight: 600, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }} title={t.label_path}>
                        {t.label_path || t.label}
                      </span>
                    </div>
                  </div>
                  <div style={{ position: "relative", display: "grid", gridTemplateColumns: `repeat(${axis.length}, 1fr)`, minHeight: 30 }}>
                    {/* 연결선 */}
                    {idxs.length >= 2 && (
                      <div style={{ position: "absolute", top: "50%", left: `${leftPct}%`, width: `${widthPct}%`, height: 2, background: col, opacity: 0.5, transform: "translateY(-50%)" }} />
                    )}
                    {/* 주차별 점 */}
                    {axis.map((w) => {
                      const m = mem.get(w);
                      if (!m) return <div key={w} />;
                      const c = clusterByKey.get(`${w}|${m.cluster_id}`);
                      const wgt = c ? c.weight : t.peak_weight;
                      const d = dotSize(wgt);
                      return (
                        <div key={w} style={{ display: "flex", justifyContent: "center", alignItems: "center", zIndex: 1 }}>
                          <span
                            title={`${w} · ${c?.label ?? ""} · 가중 ${wgt}${m.link_type ? ` · ${m.link_type}` : ""}`}
                            style={{ width: d, height: d, borderRadius: "50%", background: col, border: week === w ? "2px solid #1f6fe0" : "2px solid #fff", boxShadow: "0 0 0 1px #d7dce5" }}
                          />
                        </div>
                      );
                    })}
                  </div>
                </div>
              );
            })}
            {threads.length === 0 && <Empty>표시할 스레드가 없습니다.</Empty>}
          </div>
        </div>
      </div>

      {/* ── 사건 전개 상세 패널 (선택 스레드) — describability 전면화 ── */}
      {selectedMeta && (() => {
        const mem = threadMembers.get(selectedMeta.thread_id) ?? new Map<string, Member>();
        const segs = (selectedMeta.label_path || selectedMeta.label).split("→").map((x) => x.trim()).filter(Boolean);
        const col = KIND_COLOR[selectedMeta.kind] ?? "#9aa3b2";
        const rows = [...mem.entries()].sort((a, b) => weekKey(a[0]) - weekKey(b[0]));
        return (
          <div style={{ marginTop: 16, border: `1px solid ${col}`, borderRadius: 10, padding: 14, background: "#fafbfd" }}>
            <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 12 }}>
              <span style={{ fontSize: 11, color: "#fff", background: col, borderRadius: 6, padding: "2px 8px" }}>
                {selectedMeta.kind} · {selectedMeta.n_weeks}주 · {selectedMeta.start_week}~{selectedMeta.end_week}
              </span>
              <span style={{ fontWeight: 700, fontSize: 15 }}>{selectedMeta.label}</span>
              <span className="muted" style={{ fontSize: 12 }}>정점가중 {selectedMeta.peak_weight}</span>
              <button onClick={() => setSelectedThread(null)} style={{ marginLeft: "auto", border: "1px solid #e5e8ee", background: "#fff", borderRadius: 6, padding: "2px 8px", cursor: "pointer" }}>✕ 닫기</button>
            </div>

            {/* 사건 전개(라벨궤적) — 명사+동사 말묶음의 주 간 흐름 */}
            <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 6, marginBottom: 14 }}>
              {segs.map((seg, i) => (
                <Fragment key={i}>
                  <span style={{ background: "#eef5ff", border: "1px solid #cfe0fb", borderRadius: 14, padding: "3px 11px", fontWeight: 600, fontSize: 13 }}>{seg}</span>
                  {i < segs.length - 1 && <span style={{ color: col, fontWeight: 700 }}>→</span>}
                </Fragment>
              ))}
            </div>

            {/* 주차별 대표문장(이 트렌드가 '무슨 일'인지) */}
            <div style={{ display: "flex", flexDirection: "column", gap: 5 }}>
              {rows.map(([w, m]) => {
                const c = clusterByKey.get(`${w}|${m.cluster_id}`);
                return (
                  <div key={w} style={{ display: "grid", gridTemplateColumns: "62px 150px 1fr", gap: 10, fontSize: 13, alignItems: "baseline", padding: "3px 0", borderTop: "1px solid #f1f3f7" }}>
                    <span style={{ fontWeight: 600, color: week === w ? "#1f6fe0" : "#888" }}>{w.replace(/^\d{4}-/, "")}</span>
                    <span style={{ fontWeight: 600 }}>{c?.label ?? "-"}</span>
                    <span className="muted" style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }} title={c?.rep_sentence}>
                      {c?.rep_sentence ? `“${c.rep_sentence}”` : ""}
                    </span>
                  </div>
                );
              })}
            </div>

            {selectedMeta.top_keywords && (
              <div className="muted" style={{ fontSize: 12, marginTop: 12 }}>키워드: {selectedMeta.top_keywords}</div>
            )}
          </div>
        );
      })()}
    </div>
  );
}
