import { api } from "../api/client";
import { useAsync } from "../hooks";
import { Loading, Empty, ErrorBox } from "./ui";

/** 범용 raw 테이블 — newstrend 객체를 그대로 표로 출력(1차: raw data). */
function cell(v: unknown): string {
  if (v === null || v === undefined) return "";
  if (typeof v === "object") {
    const s = JSON.stringify(v);
    return s.length > 200 ? s.slice(0, 200) + "…" : s;
  }
  const s = String(v);
  return s.length > 300 ? s.slice(0, 300) + "…" : s;
}

export default function RawTable({
  table,
  label,
  week,
  note,
  limit = 500,
}: {
  table: string;
  label: string;
  week: string | null;
  note?: string;
  limit?: number;
}) {
  const st = useAsync(() => api.raw(table, week ?? undefined, limit), [table, week, limit]);

  return (
    <section className="raw-block">
      <div className="raw-head">
        <h3>{label}</h3>
        <code className="raw-name">{table}</code>
        {note && <span className="raw-note">{note}</span>}
        {st.data && (
          <span className="raw-meta">
            {st.data.row_count} rows
            {st.data.truncated && ` (상위 ${limit} 제한)`}
            {st.data.week_col ? ` · ${st.data.week_col}=${week ?? "latest"}` : " · 주차무관"}
          </span>
        )}
      </div>
      {st.loading && <Loading />}
      {st.error && <ErrorBox message={st.error} />}
      {st.data && (st.data.rows.length === 0 ? (
        <Empty>해당 주차 데이터가 없습니다. (단계 파이프라인 실행 필요)</Empty>
      ) : (
        <div className="raw-scroll">
          <table className="raw-table">
            <thead>
              <tr>
                {st.data.columns.map((c) => (
                  <th key={c}>{c}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {st.data.rows.map((row, i) => (
                <tr key={i}>
                  {st.data!.columns.map((c) => (
                    <td key={c} title={cell(row[c])}>
                      {cell(row[c])}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ))}
    </section>
  );
}
