import { NavLink, Navigate, Route, Routes } from "react-router-dom";
import { WeekProvider, useWeek } from "./week";
import { STAGES } from "./stages";
import StageView from "./pages/StageView";
import Home from "./pages/Home";
import TrendExplorer from "./pages/TrendExplorer";
import Detail from "./pages/Detail";
import WeekCompare from "./pages/WeekCompare";
import PeriodTracker from "./pages/PeriodTracker";

function WeekSelector() {
  const { week, weeks, setWeek } = useWeek();
  if (!weeks.length) return null;
  return (
    <div className="week-select">
      <select value={week ?? ""} onChange={(e) => setWeek(e.target.value)}>
        {[...weeks].reverse().map((w) => (
          <option key={w} value={w}>
            {w}
          </option>
        ))}
      </select>
    </div>
  );
}

function Header() {
  return (
    <header className="header">
      <div className="topbar">
        <div className="brand">
          SquadOne <small>뉴스 트렌드 → 사입 상품 추천 · 단계별 뷰</small>
        </div>
        <div className="spacer" />
        <WeekSelector />
      </div>
      {/* 단계 전환 탭 (1~8) */}
      <nav className="stage-tabs">
        {STAGES.map((s) => (
          <NavLink key={s.slug} to={`/stage/${s.slug}`} className="stage-tab">
            {s.title}
          </NavLink>
        ))}
      </nav>
      {/* 기존 대시보드(보조) */}
      <nav className="legacy-nav">
        <NavLink to="/dashboard" end>
          이번 주 추천
        </NavLink>
        <NavLink to="/trends">트렌드 익스플로러</NavLink>
        <NavLink to="/track">기간 추적</NavLink>
        <NavLink to="/compare">주차 비교</NavLink>
      </nav>
    </header>
  );
}

export default function App() {
  return (
    <WeekProvider>
      <Header />
      <main className="container">
        <Routes>
          <Route path="/" element={<Navigate to="/stage/1" replace />} />
          <Route path="/stage/:slug" element={<StageView />} />
          {/* 기존 대시보드 라우트 */}
          <Route path="/dashboard" element={<Home />} />
          <Route path="/trends" element={<TrendExplorer />} />
          <Route path="/trend/:keyword" element={<Detail />} />
          <Route path="/track" element={<PeriodTracker />} />
          <Route path="/compare" element={<WeekCompare />} />
        </Routes>
      </main>
    </WeekProvider>
  );
}
