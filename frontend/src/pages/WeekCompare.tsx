import { api } from "../api/client";
import { useWeek } from "../week";
import { useAsync } from "../hooks";
import { Empty, ErrorBox, Loading } from "../components/ui";

export default function WeekCompare() {
  const { week, weeks, ready } = useWeek();
  const idx = week ? weeks.indexOf(week) : -1;
  const prevWeek = idx > 0 ? weeks[idx - 1] : null;

  const cur = useAsync(() => api.recommendations(week ?? undefined), [week]);
  const prev = useAsync(() => (prevWeek ? api.recommendations(prevWeek) : Promise.resolve(null)), [prevWeek]);

  if (!ready) return <Loading />;
  if (cur.error) return <ErrorBox message={cur.error} />;

  const curNames = new Set((cur.data?.products ?? []).map((p) => p.product_name ?? ""));
  const prevNames = new Set((prev.data?.products ?? []).map((p) => p.product_name ?? ""));
  const added = [...curNames].filter((n) => n && !prevNames.has(n));
  const kept = [...curNames].filter((n) => n && prevNames.has(n));
  const dropped = [...prevNames].filter((n) => n && !curNames.has(n));

  return (
    <div>
      <h1 className="page-title">주차 비교</h1>
      <p className="page-sub">
        {prevWeek ?? "(이전 주 없음)"} → {week} · 추천 상품군 변화
      </p>

      {cur.loading || prev.loading ? (
        <Loading />
      ) : curNames.size === 0 && prevNames.size === 0 ? (
        <Empty>비교할 추천 데이터가 없습니다 (7단계 미적재).</Empty>
      ) : (
        <div className="panel">
          <table className="cmp">
            <thead>
              <tr>
                <th>구분</th>
                <th>상품군</th>
              </tr>
            </thead>
            <tbody>
              <tr>
                <td style={{ color: "var(--active)" }}>🆕 신규</td>
                <td>{added.join(", ") || <span className="muted">없음</span>}</td>
              </tr>
              <tr>
                <td style={{ color: "var(--accent)" }}>↔ 유지</td>
                <td>{kept.join(", ") || <span className="muted">없음</span>}</td>
              </tr>
              <tr>
                <td className="muted">⏷ 탈락</td>
                <td>{dropped.join(", ") || <span className="muted">없음</span>}</td>
              </tr>
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
