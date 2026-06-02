import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { api } from "./api/client";

interface WeekCtx {
  week: string | null; // 현재 선택 주차
  weeks: string[];
  setWeek: (w: string) => void;
  ready: boolean;
}

const Ctx = createContext<WeekCtx>({ week: null, weeks: [], setWeek: () => {}, ready: false });

export function WeekProvider({ children }: { children: ReactNode }) {
  const [weeks, setWeeks] = useState<string[]>([]);
  const [week, setWeek] = useState<string | null>(null);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    api
      .weeks()
      .then((r) => {
        setWeeks(r.weeks);
        const saved = localStorage.getItem("squadone.week");
        setWeek(saved && r.weeks.includes(saved) ? saved : r.latest);
      })
      .catch(() => {})
      .finally(() => setReady(true));
  }, []);

  const update = (w: string) => {
    setWeek(w);
    localStorage.setItem("squadone.week", w);
  };

  return <Ctx.Provider value={{ week, weeks, setWeek: update, ready }}>{children}</Ctx.Provider>;
}

export const useWeek = () => useContext(Ctx);
