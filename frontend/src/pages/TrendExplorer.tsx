import { useNavigate } from "react-router-dom";
import {
  CartesianGrid,
  Cell,
  ResponsiveContainer,
  Scatter,
  ScatterChart,
  Tooltip,
  XAxis,
  YAxis,
  ZAxis,
} from "recharts";
import { api } from "../api/client";
import { useWeek } from "../week";
import { useAsync } from "../hooks";
import { Empty, ErrorBox, Loading, PALETTE, STATUS_COLOR, STATUS_LABEL, STATUS_ORDER } from "../components/ui";
import type { Trend } from "../api/types";

function LifecycleBoard({ trends }: { trends: Trend[] }) {
  const nav = useNavigate();
  const byStatus: Record<string, Trend[]> = {};
  for (const t of trends) {
    const s = t.status ?? "Archived";
    (byStatus[s] ||= []).push(t);
  }
  return (
    <div className="lifecycle">
      {STATUS_ORDER.map((status) => {
        const items = byStatus[status] ?? [];
        const color = STATUS_COLOR[status];
        return (
          <div className="lane" key={status}>
            <h3>
              <span className="dot" style={{ background: color }} />
              {STATUS_LABEL[status]} ({status})
              <span className="count">{items.length}</span>
            </h3>
            {items.slice(0, 30).map((t) => (
              <div
                key={t.keyword}
                className="chip"
                onClick={() => nav(`/trend/${encodeURIComponent(t.keyword)}`)}
              >
                <span className="kw">{t.keyword}</span>
                <span className="z">{t.z_score?.toFixed(1) ?? "-"}</span>
              </div>
            ))}
            {items.length === 0 && <div className="muted" style={{ fontSize: 13 }}>없음</div>}
          </div>
        );
      })}
    </div>
  );
}

function GroupBubble({ data }: { data: { x: number; y: number; z: number; group_id: string }[] }) {
  return (
    <ResponsiveContainer width="100%" height={340}>
      <ScatterChart margin={{ top: 16, right: 24, bottom: 16, left: 0 }}>
        <CartesianGrid stroke="#2c3744" />
        <XAxis
          type="number"
          dataKey="x"
          name="응집도"
          tick={{ fontSize: 11, fill: "#8b97a6" }}
          label={{ value: "응집도(cohesion)", position: "insideBottom", offset: -4, fill: "#8b97a6", fontSize: 12 }}
        />
        <YAxis
          type="number"
          dataKey="y"
          name="그룹 점수"
          tick={{ fontSize: 11, fill: "#8b97a6" }}
          label={{ value: "그룹 점수", angle: -90, position: "insideLeft", fill: "#8b97a6", fontSize: 12 }}
        />
        <ZAxis type="number" dataKey="z" range={[60, 600]} name="멤버 수" />
        <Tooltip
          cursor={{ strokeDasharray: "3 3" }}
          contentStyle={{ background: "#1a2029", border: "1px solid #2c3744" }}
          formatter={(v: number, n: string) => [v, n]}
        />
        <Scatter data={data}>
          {data.map((_, i) => (
            <Cell key={i} fill={PALETTE[i % PALETTE.length]} />
          ))}
        </Scatter>
      </ScatterChart>
    </ResponsiveContainer>
  );
}

export default function TrendExplorer() {
  const { week, ready } = useWeek();
  const trends = useAsync(() => api.trends(week ?? undefined), [week]);
  const groups = useAsync(() => api.groups(week ?? undefined), [week]);

  if (!ready) return <Loading />;
  if (trends.error) return <ErrorBox message={trends.error} />;

  const allTrends = trends.data?.trends ?? [];
  const bubble = (groups.data?.groups ?? [])
    .filter((g) => g.group_score != null)
    .map((g) => ({
      x: g.cohesion ?? 0,
      y: g.group_score ?? 0,
      z: Array.isArray(g.members) ? g.members.length : 1,
      group_id: g.group_id,
    }));

  return (
    <div>
      <h1 className="page-title">트렌드 익스플로러</h1>
      <p className="page-sub">{week} 주차 · 생명주기 단계와 연관 키워드 군집</p>

      <section className="section">
        <h2>트렌드 생명주기</h2>
        {trends.loading ? (
          <Loading />
        ) : allTrends.length === 0 ? (
          <Empty>
            이 주차에는 적재된 트렌드가 없습니다.
            <div className="muted" style={{ marginTop: 8, fontSize: 13 }}>
              5~6단계(z_score_filtering → trend_extractor)까지 실행하면 채워집니다.
            </div>
          </Empty>
        ) : (
          <LifecycleBoard trends={allTrends} />
        )}
      </section>

      <section className="section">
        <h2>연관 키워드 군집</h2>
        <div className="panel">
          {groups.loading ? (
            <Loading />
          ) : bubble.length === 0 ? (
            <Empty>이 주차에는 시맨틱 그룹이 없습니다.</Empty>
          ) : (
            <GroupBubble data={bubble} />
          )}
        </div>
      </section>
    </div>
  );
}
