import { useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { api } from "../api/client";
import { useWeek } from "../week";
import { useAsync } from "../hooks";
import { Empty, ErrorBox, Loading, PALETTE, STATUS_COLOR, STATUS_LABEL, STATUS_ORDER } from "../components/ui";
import type { ProductRangeRow } from "../api/types";

const AXIS = { fontSize: 11, fill: "#8b97a6" };
const TOOLTIP_STYLE = { background: "#1a2029", border: "1px solid #2c3744" };

/** 기간 기본값: 종료=최신 주차, 시작=그보다 11주 전(목록 기준, 없으면 첫 주차). */
function defaultRange(weeks: string[]): { from: string; to: string } {
  if (weeks.length === 0) return { from: "", to: "" };
  const to = weeks[weeks.length - 1];
  const fromIdx = Math.max(0, weeks.length - 12);
  return { from: weeks[fromIdx], to };
}

export default function PeriodTracker() {
  const { weeks, ready } = useWeek();
  const def = useMemo(() => defaultRange(weeks), [weeks]);
  const [from, setFrom] = useState<string>("");
  const [to, setTo] = useState<string>("");

  // weeks 로딩 후 기본 구간 1회 세팅(사용자가 바꾸면 유지)
  const f = from || def.from;
  const t = to || def.to;
  const validRange = f && t && f <= t;

  const lifecycle = useAsync(
    () => (validRange ? api.rangeLifecycle(f, t) : Promise.resolve(null)),
    [f, t]
  );
  const zscore = useAsync(
    () => (validRange ? api.rangeZScore(f, t, 8) : Promise.resolve(null)),
    [f, t]
  );
  const products = useAsync(
    () => (validRange ? api.rangeProducts(f, t) : Promise.resolve(null)),
    [f, t]
  );
  const keysentence = useAsync(
    () => (validRange ? api.rangeKeySentence(f, t) : Promise.resolve(null)),
    [f, t]
  );

  if (!ready) return <Loading />;

  return (
    <div>
      <h1 className="page-title">기간 추적</h1>
      <p className="page-sub">선택 기간의 5~7단계 변화 · 생명주기 · 상품군 흐름</p>

      <RangeBar weeks={weeks} from={f} to={t} onFrom={setFrom} onTo={setTo} invalid={!validRange} />

      {!validRange ? (
        <Empty>시작 주차가 종료 주차보다 앞서도록 선택하세요.</Empty>
      ) : (
        <>
          {/* ZONE A · 6단계 트렌드 생명주기 */}
          <section className="section">
            <h2>① 트렌드 생명주기 추이 (6단계)</h2>
            <div className="panel">
              {lifecycle.loading ? (
                <Loading />
              ) : lifecycle.error ? (
                <ErrorBox message={lifecycle.error} />
              ) : (
                <LifecycleArea rows={lifecycle.data?.rows ?? []} />
              )}
            </div>
          </section>

          <section className="section">
            <h2>주요 키워드 z-score 추이 (상위 8)</h2>
            <div className="panel">
              {zscore.loading ? (
                <Loading />
              ) : zscore.error ? (
                <ErrorBox message={zscore.error} />
              ) : (
                <ZScoreLines
                  keywords={zscore.data?.keywords ?? []}
                  rows={zscore.data?.rows ?? []}
                />
              )}
            </div>
          </section>

          {/* ZONE B · 7단계 상품군 변화 */}
          <section className="section">
            <h2>② 상품군 변화 흐름 (7단계)</h2>
            <div className="panel" style={{ marginBottom: 16 }}>
              <h3 style={{ marginTop: 0, fontSize: 15 }}>주차별 신규/유지/탈락</h3>
              {products.loading ? (
                <Loading />
              ) : products.error ? (
                <ErrorBox message={products.error} />
              ) : (
                <ProductDiffBar rows={products.data?.rows ?? []} />
              )}
            </div>
            <div className="panel">
              <h3 style={{ marginTop: 0, fontSize: 15 }}>상품군 × 주차 히트맵 (숫자=rank)</h3>
              {products.loading ? (
                <Loading />
              ) : (
                <ProductHeatmap rows={products.data?.rows ?? []} />
              )}
            </div>
          </section>

          {/* ZONE C · 5단계 근거 추이 */}
          <section className="section">
            <h2>③ 핵심문장·근거 추이 (5단계)</h2>
            <div className="panel">
              {keysentence.loading ? (
                <Loading />
              ) : keysentence.error ? (
                <ErrorBox message={keysentence.error} />
              ) : (
                <KeySentenceLine rows={keysentence.data?.rows ?? []} />
              )}
            </div>
          </section>
        </>
      )}
    </div>
  );
}

function RangeBar({
  weeks,
  from,
  to,
  onFrom,
  onTo,
  invalid,
}: {
  weeks: string[];
  from: string;
  to: string;
  onFrom: (w: string) => void;
  onTo: (w: string) => void;
  invalid: boolean;
}) {
  const opts = [...weeks].reverse();
  return (
    <div
      className="panel"
      style={{ display: "flex", gap: 16, alignItems: "center", flexWrap: "wrap", marginBottom: 20 }}
    >
      <label style={{ display: "flex", gap: 8, alignItems: "center" }}>
        <span className="muted">시작 주차</span>
        <select value={from} onChange={(e) => onFrom(e.target.value)}>
          {opts.map((w) => (
            <option key={w} value={w}>
              {w}
            </option>
          ))}
        </select>
      </label>
      <span className="muted">~</span>
      <label style={{ display: "flex", gap: 8, alignItems: "center" }}>
        <span className="muted">종료 주차</span>
        <select value={to} onChange={(e) => onTo(e.target.value)}>
          {opts.map((w) => (
            <option key={w} value={w}>
              {w}
            </option>
          ))}
        </select>
      </label>
      {invalid && (
        <span style={{ color: "var(--fading)", fontSize: 13 }}>시작 ≤ 종료 여야 합니다</span>
      )}
    </div>
  );
}

// ── ZONE A ───────────────────────────────────────────────────────

function LifecycleArea({ rows }: { rows: { week: string; status: string | null; count: number }[] }) {
  const data = useMemo(() => {
    const byWeek: Record<string, Record<string, number | string>> = {};
    for (const r of rows) {
      const w = (byWeek[r.week] ||= { week: r.week });
      const s = r.status ?? "Archived";
      w[s] = ((w[s] as number) ?? 0) + r.count;
    }
    return Object.values(byWeek).sort((a, b) => String(a.week).localeCompare(String(b.week)));
  }, [rows]);

  if (data.length === 0) return <Empty>이 기간에 적재된 트렌드가 없습니다 (6단계 미실행).</Empty>;

  return (
    <ResponsiveContainer width="100%" height={300}>
      <AreaChart data={data} margin={{ top: 10, right: 16, bottom: 4, left: -8 }}>
        <CartesianGrid stroke="#2c3744" />
        <XAxis dataKey="week" tick={AXIS} minTickGap={16} />
        <YAxis tick={AXIS} allowDecimals={false} />
        <Tooltip contentStyle={TOOLTIP_STYLE} />
        <Legend wrapperStyle={{ fontSize: 12 }} formatter={(v) => STATUS_LABEL[v] ?? v} />
        {STATUS_ORDER.map((s) => (
          <Area
            key={s}
            type="monotone"
            dataKey={s}
            stackId="lc"
            stroke={STATUS_COLOR[s]}
            fill={STATUS_COLOR[s]}
            fillOpacity={0.55}
          />
        ))}
      </AreaChart>
    </ResponsiveContainer>
  );
}

function ZScoreLines({
  keywords,
  rows,
}: {
  keywords: string[];
  rows: { week: string; keyword: string; z_score: number | null }[];
}) {
  const data = useMemo(() => {
    const byWeek: Record<string, Record<string, number | string | null>> = {};
    for (const r of rows) {
      const w = (byWeek[r.week] ||= { week: r.week });
      w[r.keyword] = r.z_score;
    }
    return Object.values(byWeek).sort((a, b) => String(a.week).localeCompare(String(b.week)));
  }, [rows]);

  if (data.length === 0 || keywords.length === 0)
    return <Empty>이 기간에 z-score 추이 데이터가 없습니다.</Empty>;

  return (
    <ResponsiveContainer width="100%" height={320}>
      <LineChart data={data} margin={{ top: 10, right: 16, bottom: 4, left: -8 }}>
        <CartesianGrid stroke="#2c3744" />
        <XAxis dataKey="week" tick={AXIS} minTickGap={16} />
        <YAxis tick={AXIS} />
        <Tooltip contentStyle={TOOLTIP_STYLE} />
        <Legend wrapperStyle={{ fontSize: 12 }} />
        {keywords.map((kw, i) => (
          <Line
            key={kw}
            type="monotone"
            dataKey={kw}
            stroke={PALETTE[i % PALETTE.length]}
            strokeWidth={2}
            dot={false}
            connectNulls
          />
        ))}
      </LineChart>
    </ResponsiveContainer>
  );
}

// ── ZONE B ───────────────────────────────────────────────────────

/** 주차 오름차순 + 각 주차의 상품명 집합. heatmap/diff 공용 가공. */
function useProductGrid(rows: ProductRangeRow[]) {
  return useMemo(() => {
    const weeks = [...new Set(rows.map((r) => r.week))].sort((a, b) => a.localeCompare(b));
    const namesByWeek: Record<string, Set<string>> = {};
    const rankByCell: Record<string, number> = {}; // `${week} ${name}` -> rank
    for (const r of rows) {
      const name = r.product_name ?? "";
      if (!name) continue;
      (namesByWeek[r.week] ||= new Set()).add(name);
      rankByCell[`${r.week} ${name}`] = r.rank;
    }
    const allNames = [...new Set(rows.map((r) => r.product_name ?? "").filter(Boolean))];
    return { weeks, namesByWeek, rankByCell, allNames };
  }, [rows]);
}

function ProductDiffBar({ rows }: { rows: ProductRangeRow[] }) {
  const { weeks, namesByWeek } = useProductGrid(rows);
  const data = useMemo(() => {
    return weeks.map((w, i) => {
      const cur = namesByWeek[w] ?? new Set<string>();
      const prev = i > 0 ? namesByWeek[weeks[i - 1]] ?? new Set<string>() : new Set<string>();
      let added = 0;
      let kept = 0;
      for (const n of cur) (prev.has(n) ? (kept += 1) : (added += 1));
      const dropped = i > 0 ? [...prev].filter((n) => !cur.has(n)).length : 0;
      return { week: w, 신규: added, 유지: kept, 탈락: -dropped };
    });
  }, [weeks, namesByWeek]);

  if (weeks.length === 0) return <Empty>이 기간에 추천 상품이 없습니다 (7단계 미적재).</Empty>;

  return (
    <ResponsiveContainer width="100%" height={280}>
      <BarChart data={data} margin={{ top: 10, right: 16, bottom: 4, left: -8 }} stackOffset="sign">
        <CartesianGrid stroke="#2c3744" />
        <XAxis dataKey="week" tick={AXIS} minTickGap={16} />
        <YAxis tick={AXIS} allowDecimals={false} />
        <Tooltip
          contentStyle={TOOLTIP_STYLE}
          formatter={(v: number, n: string) => [Math.abs(v), n]}
        />
        <Legend wrapperStyle={{ fontSize: 12 }} />
        <Bar dataKey="신규" stackId="d" fill="var(--active)" />
        <Bar dataKey="유지" stackId="d" fill="var(--accent)" />
        <Bar dataKey="탈락" stackId="d" fill="var(--fading)" />
      </BarChart>
    </ResponsiveContainer>
  );
}

function ProductHeatmap({ rows }: { rows: ProductRangeRow[] }) {
  const { weeks, rankByCell, allNames } = useProductGrid(rows);
  if (weeks.length === 0 || allNames.length === 0)
    return <Empty>이 기간에 추천 상품이 없습니다 (7단계 미적재).</Empty>;

  const cellBg = (rank: number | undefined) => {
    if (rank === undefined) return "transparent";
    // rank 1(최상위)이 가장 진하게.
    const op = Math.max(0.2, 0.95 - (rank - 1) * 0.16);
    return `rgba(79, 156, 249, ${op})`;
  };

  return (
    <div style={{ overflowX: "auto" }}>
      <table className="heatmap" style={{ borderCollapse: "collapse", fontSize: 12, width: "100%" }}>
        <thead>
          <tr>
            <th style={{ textAlign: "left", padding: "6px 10px", color: "#8b97a6", position: "sticky", left: 0, background: "#1a2029" }}>
              상품군
            </th>
            {weeks.map((w) => (
              <th key={w} style={{ padding: "6px 8px", color: "#8b97a6", whiteSpace: "nowrap" }}>
                {w.replace(/^\d{4}-/, "")}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {allNames.map((name) => (
            <tr key={name}>
              <td
                style={{
                  padding: "6px 10px",
                  whiteSpace: "nowrap",
                  position: "sticky",
                  left: 0,
                  background: "#1a2029",
                  borderTop: "1px solid #2c3744",
                }}
              >
                {name}
              </td>
              {weeks.map((w) => {
                const rank = rankByCell[`${w} ${name}`];
                return (
                  <td
                    key={w}
                    title={rank !== undefined ? `${name} · ${w} · rank ${rank}` : undefined}
                    style={{
                      textAlign: "center",
                      padding: "6px 8px",
                      background: cellBg(rank),
                      borderTop: "1px solid #2c3744",
                      color: rank !== undefined && rank <= 2 ? "#0d1117" : "#cdd6e0",
                      fontWeight: rank !== undefined ? 600 : 400,
                    }}
                  >
                    {rank ?? ""}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ── ZONE C ───────────────────────────────────────────────────────

function KeySentenceLine({
  rows,
}: {
  rows: { week: string; keysentence_count: number; evidence_total: number }[];
}) {
  const nav = useNavigate();
  const data = useMemo(
    () => [...rows].sort((a, b) => a.week.localeCompare(b.week)),
    [rows]
  );
  if (data.length === 0) return <Empty>이 기간에 핵심문장 데이터가 없습니다 (5단계 미적재).</Empty>;

  return (
    <ResponsiveContainer width="100%" height={280}>
      <LineChart
        data={data}
        margin={{ top: 10, right: 16, bottom: 4, left: -8 }}
        onClick={(e) => {
          const w = (e?.activeLabel as string) || "";
          if (w) nav(`/trends`);
        }}
      >
        <CartesianGrid stroke="#2c3744" />
        <XAxis dataKey="week" tick={AXIS} minTickGap={16} />
        <YAxis tick={AXIS} allowDecimals={false} />
        <Tooltip contentStyle={TOOLTIP_STYLE} />
        <Legend wrapperStyle={{ fontSize: 12 }} />
        <Line
          type="monotone"
          dataKey="keysentence_count"
          name="핵심문장 수"
          stroke="#4f9cf9"
          strokeWidth={2}
          dot={{ r: 3 }}
        />
        <Line
          type="monotone"
          dataKey="evidence_total"
          name="근거 문서 합계"
          stroke="#2ecc71"
          strokeWidth={2}
          dot={false}
        />
      </LineChart>
    </ResponsiveContainer>
  );
}
