import { Link, useParams } from "react-router-dom";
import {
  Cell,
  Line,
  LineChart,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { api } from "../api/client";
import { useWeek } from "../week";
import { useAsync } from "../hooks";
import { Empty, ErrorBox, Loading, PALETTE, StatusBadge } from "../components/ui";

export default function Detail() {
  const { keyword = "" } = useParams();
  const kw = decodeURIComponent(keyword);
  const { week, ready } = useWeek();

  const detail = useAsync(() => api.trendDetail(kw, week ?? undefined), [kw, week]);
  const zseries = useAsync(() => api.zscoreSeries(kw), [kw]);
  const sources = useAsync(() => api.sources(kw, week ?? undefined), [kw, week]);

  if (!ready) return <Loading />;

  return (
    <div>
      <Link className="back-link" to="/trends">
        ← 트렌드 익스플로러로
      </Link>
      <h1 className="page-title">
        {kw} <StatusBadge status={detail.data?.timeseries?.status ?? null} />
      </h1>
      <p className="page-sub">{week} 주차 트렌드 상세 · 추천 근거 사슬</p>

      {detail.error ? (
        <ErrorBox message={detail.error} />
      ) : detail.loading ? (
        <Loading />
      ) : (
        <>
          <section className="section">
            <div className="panel">
              <h2 style={{ marginTop: 0 }}>주간 요약</h2>
              <p style={{ lineHeight: 1.7, margin: 0 }}>
                {detail.data?.timeseries?.weekly_summary || (
                  <span className="muted">이 주차 요약이 없습니다 (트렌드 미적재).</span>
                )}
              </p>
              {detail.data?.keysentence?.key_sentence && (
                <p style={{ lineHeight: 1.7, marginBottom: 0 }}>
                  <strong>핵심 문장:</strong> {detail.data.keysentence.key_sentence}
                </p>
              )}
            </div>
          </section>

          <div className="two-col">
            <section className="section">
              <h2>z-score 추이 (주차별)</h2>
              <div className="panel">
                {zseries.loading ? (
                  <Loading />
                ) : (zseries.data?.series.length ?? 0) === 0 ? (
                  <Empty>z-score 데이터가 없습니다.</Empty>
                ) : (
                  <ResponsiveContainer width="100%" height={260}>
                    <LineChart data={zseries.data!.series} margin={{ top: 10, right: 16, bottom: 4, left: -8 }}>
                      <XAxis dataKey="week" tick={{ fontSize: 11, fill: "#8b97a6" }} minTickGap={24} />
                      <YAxis tick={{ fontSize: 11, fill: "#8b97a6" }} />
                      <Tooltip contentStyle={{ background: "#1a2029", border: "1px solid #2c3744" }} />
                      <Line type="monotone" dataKey="z_score" stroke="#4f9cf9" strokeWidth={2} dot={false} />
                    </LineChart>
                  </ResponsiveContainer>
                )}
              </div>
            </section>

            <section className="section">
              <h2>언론사 분포</h2>
              <div className="panel">
                {sources.loading ? (
                  <Loading />
                ) : (sources.data?.sources.length ?? 0) === 0 ? (
                  <Empty>언론사 데이터가 없습니다.</Empty>
                ) : (
                  <ResponsiveContainer width="100%" height={260}>
                    <PieChart>
                      <Pie
                        data={sources.data!.sources.slice(0, 8)}
                        dataKey="count"
                        nameKey="source"
                        innerRadius={55}
                        outerRadius={95}
                        paddingAngle={2}
                      >
                        {sources.data!.sources.slice(0, 8).map((_, i) => (
                          <Cell key={i} fill={PALETTE[i % PALETTE.length]} />
                        ))}
                      </Pie>
                      <Tooltip contentStyle={{ background: "#1a2029", border: "1px solid #2c3744" }} />
                    </PieChart>
                  </ResponsiveContainer>
                )}
              </div>
            </section>
          </div>

          <section className="section">
            <h2>근거 뉴스 {(detail.data?.contexts.length ?? 0) > 0 ? `(${detail.data!.contexts.length})` : ""}</h2>
            <div className="panel">
              {(detail.data?.contexts.length ?? 0) === 0 ? (
                <Empty>
                  근거 뉴스 스니펫이 없습니다.
                  <div className="muted" style={{ marginTop: 8, fontSize: 13 }}>
                    6단계(trend_extractor, Qdrant 벡터검색)까지 실행되면 채워집니다.
                  </div>
                </Empty>
              ) : (
                detail.data!.contexts.map((c) => (
                  <div className="evidence-item" key={c.doc_id}>
                    <div className="meta">
                      doc {c.doc_id} · 유사도 {c.score?.toFixed(3) ?? "-"}
                    </div>
                    <div className="snippet">{c.snippet || <span className="muted">(스니펫 없음)</span>}</div>
                  </div>
                ))
              )}
            </div>
          </section>
        </>
      )}
    </div>
  );
}
