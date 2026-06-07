import type {
  ExcludedTrendsResponse,
  GenerateResponse,
  GenerateStatus,
  GroupsResponse,
  KeySentenceRangeResponse,
  LifecycleRangeResponse,
  ProductsRangeResponse,
  RecommendationsResponse,
  SourcesResponse,
  Summary,
  TrendDetailResponse,
  TrendsResponse,
  WeeksResponse,
  ZScoreRangeResponse,
  ZScoreSeriesResponse,
} from "./types";

// 기본값 8010 = SquadOne_Tool REST. (8000/8001 은 SquadOne_AI 점유 → 기본값으로 쓰지 않음.)
const BASE = (import.meta.env.VITE_API_BASE ?? "http://localhost:8010").replace(/\/$/, "");
const API_KEY = import.meta.env.VITE_API_KEY ?? "";

/** 단계별 raw 조회 응답(범용 테이블). */
export interface RawTable {
  table: string;
  week_col: string | null;
  columns: string[];
  rows: Record<string, unknown>[];
  row_count: number;
  truncated: boolean;
}

async function get<T>(path: string, params?: Record<string, string | number | undefined>): Promise<T> {
  const url = new URL(BASE + path);
  if (params) {
    for (const [k, v] of Object.entries(params)) {
      if (v !== undefined && v !== "") url.searchParams.set(k, String(v));
    }
  }
  const headers: Record<string, string> = {};
  if (API_KEY) headers["X-API-Key"] = API_KEY;
  const res = await fetch(url.toString(), { headers });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`${res.status} ${res.statusText} — ${text.slice(0, 200)}`);
  }
  return res.json() as Promise<T>;
}

async function post<T>(path: string, body: unknown): Promise<T> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (API_KEY) headers["X-API-Key"] = API_KEY;
  const res = await fetch(BASE + path, { method: "POST", headers, body: JSON.stringify(body) });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`${res.status} ${res.statusText} — ${text.slice(0, 200)}`);
  }
  return res.json() as Promise<T>;
}

export const api = {
  weeks: () => get<WeeksResponse>("/v1/view/weeks"),
  summary: (week?: string) => get<Summary>("/v1/view/summary", { week }),
  recommendations: (week?: string) => get<RecommendationsResponse>("/v1/view/recommendations", { week }),
  trends: (week?: string, status?: string) => get<TrendsResponse>("/v1/view/trends", { week, status }),
  trendsExcluded: (week?: string) => get<ExcludedTrendsResponse>("/v1/view/trends/excluded", { week }),
  groups: (week?: string) => get<GroupsResponse>("/v1/view/groups", { week }),
  trendDetail: (keyword: string, week?: string) =>
    get<TrendDetailResponse>("/v1/view/trend", { keyword, week }),
  zscoreSeries: (keyword: string, limit = 200) =>
    get<ZScoreSeriesResponse>("/v1/view/zscore-series", { keyword, limit }),
  sources: (keyword: string, week?: string) => get<SourcesResponse>("/v1/view/sources", { keyword, week }),
  generate: (week: string) => post<GenerateResponse>("/v1/view/generate", { week }),
  generateStatus: (week: string) => get<GenerateStatus>("/v1/view/generate-status", { week }),
  rangeLifecycle: (from: string, to: string) =>
    get<LifecycleRangeResponse>("/v1/view/range/lifecycle", { from, to }),
  rangeZScore: (from: string, to: string, top = 8) =>
    get<ZScoreRangeResponse>("/v1/view/range/zscore", { from, to, top }),
  rangeProducts: (from: string, to: string) =>
    get<ProductsRangeResponse>("/v1/view/range/products", { from, to }),
  rangeKeySentence: (from: string, to: string) =>
    get<KeySentenceRangeResponse>("/v1/view/range/keysentence", { from, to }),
  // 단계별 raw 조회 (단일 주차 또는 기간 from~to)
  raw: (table: string, week?: string, limit = 500, weekFrom?: string, weekTo?: string) =>
    get<RawTable>("/v1/view/raw", { table, week, week_from: weekFrom, week_to: weekTo, limit }),
  // 키워드 제외(분석 영구 제외)
  exclusions: () => get<ExclusionsResponse>("/v1/view/exclusions"),
  addExclusions: (keywords: string[]) =>
    post<ExclusionsResponse & { added: number }>("/v1/view/exclusions", { keywords }),
  removeExclusions: (keywords: string[]) =>
    post<ExclusionsResponse & { removed: number }>("/v1/view/exclusions/remove", { keywords }),
  // 단계 재실행(제외/기간 반영)
  rerun: (stage: number, week: string, weekFrom?: string, weekTo?: string, skipExternal = false) =>
    post<{ started: boolean; job: RerunJob }>("/v1/view/rerun", {
      stage, week, week_from: weekFrom, week_to: weekTo, skip_external: skipExternal,
    }),
  rerunStatus: (week: string) => get<RerunJob>("/v1/view/rerun-status", { week }),
};

export interface Exclusion {
  keyword: string;
  reason: string | null;
  created_at: string | null;
}
export interface ExclusionsResponse {
  exclusions: Exclusion[];
}
export interface RerunJob {
  week: string;
  status: "idle" | "running" | "success" | "failed";
  stage?: number;
  step?: string;
  message?: string;
  error?: string;
  elapsed?: number;
}
