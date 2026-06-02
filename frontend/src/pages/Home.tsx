import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api/client";
import { useWeek } from "../week";
import { useAsync } from "../hooks";
import { Empty, ErrorBox, Loading, StatusBadge } from "../components/ui";
import GenerateButton from "../components/GenerateButton";
import type { Product, Trend } from "../api/types";

function KpiCard({ label, value, accent }: { label: string; value: string | number; accent?: boolean }) {
  return (
    <div className="kpi-card">
      <div className="label">{label}</div>
      <div className={"value" + (accent ? " accent" : "")}>{value}</div>
    </div>
  );
}

function ProductCard({ product }: { product: Product }) {
  const [open, setOpen] = useState(false);
  const reason = product.selection_reason ?? "";
  const short = reason.length > 120 && !open ? reason.slice(0, 120) + "…" : reason;
  return (
    <div className="product-card" onClick={() => setOpen((v) => !v)}>
      <div className="rank">추천 #{product.rank}</div>
      <div className="name">{product.product_name ?? "(이름 없음)"}</div>
      <div className="reason">{short || <span className="muted">선정 이유 없음</span>}</div>
    </div>
  );
}

export default function Home() {
  const { week, ready } = useWeek();
  const nav = useNavigate();
  const [reloadKey, setReloadKey] = useState(0);
  const summary = useAsync(() => api.summary(week ?? undefined), [week, reloadKey]);
  const recs = useAsync(() => api.recommendations(week ?? undefined), [week, reloadKey]);
  const trends = useAsync(() => api.trends(week ?? undefined), [week, reloadKey]);

  if (!ready) return <Loading />;
  if (summary.error) return <ErrorBox message={summary.error} />;

  const s = summary.data;
  const products = recs.data?.products ?? [];
  const notable: Trend[] = (trends.data?.trends ?? []).slice(0, 8);

  return (
    <div>
      <h1 className="page-title">이번 주 상품 추천</h1>
      <p className="page-sub">{s?.week ?? week} 주차 · 뉴스 트렌드 분석 기반</p>

      <div className="kpi-grid">
        <KpiCard label="분석 주차" value={s?.week ?? "-"} />
        <KpiCard label="부상 트렌드" value={s?.emerging ?? 0} accent />
        <KpiCard label="활성 트렌드" value={s?.active ?? 0} />
        <KpiCard label="분석 키워드" value={(s?.keyword_count ?? 0).toLocaleString()} />
      </div>

      <section className="section">
        <h2>추천 상품군 {products.length > 0 ? `Top ${products.length}` : ""}</h2>
        {recs.loading ? (
          <Loading />
        ) : products.length === 0 ? (
          <Empty>
            이 주차에는 적재된 상품 추천이 없습니다.
            <div className="muted" style={{ marginTop: 8, fontSize: 13 }}>
              아래 버튼으로 이 주차의 트렌드·상품(6~7단계)을 바로 생성할 수 있습니다.
            </div>
            {week && <GenerateButton week={week} onDone={() => setReloadKey((k) => k + 1)} />}
          </Empty>
        ) : (
          <div className="product-grid">
            {products.map((p) => (
              <ProductCard key={p.rank} product={p} />
            ))}
          </div>
        )}
      </section>

      <section className="section">
        <h2>주목 트렌드</h2>
        {trends.loading ? (
          <Loading />
        ) : notable.length === 0 ? (
          <Empty>이 주차에는 적재된 트렌드가 없습니다 (5~6단계 미실행).</Empty>
        ) : (
          <div className="product-grid">
            {notable.map((t) => (
              <div
                key={t.keyword}
                className="product-card"
                onClick={() => nav(`/trend/${encodeURIComponent(t.keyword)}`)}
              >
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                  <span className="name" style={{ margin: 0, fontSize: 16 }}>
                    {t.keyword}
                  </span>
                  <StatusBadge status={t.status} />
                </div>
                <div className="reason" style={{ marginTop: 8 }}>
                  {t.weekly_summary || <span className="muted">요약 없음</span>}
                </div>
                <div className="muted" style={{ marginTop: 8, fontSize: 12 }}>
                  z-score {t.z_score?.toFixed(2) ?? "-"} · 언급 {t.count ?? 0}
                </div>
              </div>
            ))}
          </div>
        )}
      </section>
    </div>
  );
}
