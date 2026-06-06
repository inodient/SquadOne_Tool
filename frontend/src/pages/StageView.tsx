import { useParams } from "react-router-dom";
import { useWeek } from "../week";
import { STAGE_BY_SLUG } from "../stages";
import RawTable from "../components/RawTable";
import { Empty } from "../components/ui";

/** 단계 페이지 — 해당 단계의 newstrend 객체들을 raw 로 출력. */
export default function StageView() {
  const { slug = "1" } = useParams();
  const { week } = useWeek();
  const stage = STAGE_BY_SLUG[slug];

  if (!stage) return <Empty>알 수 없는 단계입니다.</Empty>;

  return (
    <div className="stage-view">
      <div className="stage-title">
        <h2>
          {stage.n}단계 · {stage.title.replace(/^\d+\s/, "")}
        </h2>
        <p className="stage-desc">{stage.desc}</p>
      </div>
      {stage.tables.map((t) => (
        <RawTable key={t.name} table={t.name} label={t.label} note={t.note} week={week} />
      ))}
    </div>
  );
}
