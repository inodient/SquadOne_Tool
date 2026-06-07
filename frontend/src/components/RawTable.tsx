import { useMemo, useState } from "react";
import { api } from "../api/client";
import { useAsync } from "../hooks";
import { Loading, Empty, ErrorBox } from "./ui";

/** 범용 raw 테이블 — newstrend 객체를 그대로 표로 출력(1차: raw data). 헤더 클릭 정렬 지원. */
function cell(v: unknown): string {
  if (v === null || v === undefined) return "";
  if (typeof v === "object") {
    const s = JSON.stringify(v);
    return s.length > 200 ? s.slice(0, 200) + "…" : s;
  }
  const s = String(v);
  return s.length > 300 ? s.slice(0, 300) + "…" : s;
}

type Dir = "asc" | "desc";

/** 정렬 비교자 — null/undefined 는 항상 마지막, 숫자는 수치, 그 외 문자(자연 정렬). */
function compare(a: unknown, b: unknown, dir: Dir): number {
  const an = a === null || a === undefined || a === "";
  const bn = b === null || b === undefined || b === "";
  if (an && bn) return 0;
  if (an) return 1; // null 은 방향 무관 끝으로
  if (bn) return -1;
  const mul = dir === "asc" ? 1 : -1;
  if (typeof a === "number" && typeof b === "number") return (a - b) * mul;
  if (typeof a === "boolean" && typeof b === "boolean") return (Number(a) - Number(b)) * mul;
  const as = typeof a === "object" ? JSON.stringify(a) : String(a);
  const bs = typeof b === "object" ? JSON.stringify(b) : String(b);
  return as.localeCompare(bs, "ko", { numeric: true }) * mul;
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
  const [sortCol, setSortCol] = useState<string | null>(null);
  const [sortDir, setSortDir] = useState<Dir>("asc");

  const onSort = (col: string) => {
    if (sortCol === col) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortCol(col);
      setSortDir("asc");
    }
  };

  const rows = useMemo(() => {
    const data = st.data?.rows ?? [];
    if (!sortCol) return data;
    return [...data].sort((r1, r2) => compare(r1[sortCol], r2[sortCol], sortDir));
  }, [st.data, sortCol, sortDir]);

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
                  <th
                    key={c}
                    className={"sortable" + (sortCol === c ? " sorted" : "")}
                    onClick={() => onSort(c)}
                    title="클릭하여 정렬"
                  >
                    {c}
                    <span className="sort-ind">{sortCol === c ? (sortDir === "asc" ? " ▲" : " ▼") : " ⇅"}</span>
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((row, i) => (
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
