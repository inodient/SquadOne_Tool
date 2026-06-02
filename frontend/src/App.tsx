import { NavLink, Route, Routes } from "react-router-dom";
import { WeekProvider, useWeek } from "./week";
import Home from "./pages/Home";
import TrendExplorer from "./pages/TrendExplorer";
import Detail from "./pages/Detail";
import WeekCompare from "./pages/WeekCompare";

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
      <div className="brand">
        SquadOne <small>뉴스 트렌드 → 상품 추천</small>
      </div>
      <nav>
        <NavLink to="/" end>
          이번 주 추천
        </NavLink>
        <NavLink to="/trends">트렌드 익스플로러</NavLink>
        <NavLink to="/compare">주차 비교</NavLink>
      </nav>
      <div className="spacer" />
      <WeekSelector />
    </header>
  );
}

export default function App() {
  return (
    <WeekProvider>
      <Header />
      <main className="container">
        <Routes>
          <Route path="/" element={<Home />} />
          <Route path="/trends" element={<TrendExplorer />} />
          <Route path="/trend/:keyword" element={<Detail />} />
          <Route path="/compare" element={<WeekCompare />} />
        </Routes>
      </main>
    </WeekProvider>
  );
}
