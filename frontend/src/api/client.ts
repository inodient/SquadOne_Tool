import type {
  GenerateResponse,
  GenerateStatus,
  GroupsResponse,
  RecommendationsResponse,
  SourcesResponse,
  Summary,
  TrendDetailResponse,
  TrendsResponse,
  WeeksResponse,
  ZScoreSeriesResponse,
} from "./types";

const BASE = (import.meta.env.VITE_API_BASE ?? "http://localhost:8000").replace(/\/$/, "");
const API_KEY = import.meta.env.VITE_API_KEY ?? "";

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
  groups: (week?: string) => get<GroupsResponse>("/v1/view/groups", { week }),
  trendDetail: (keyword: string, week?: string) =>
    get<TrendDetailResponse>("/v1/view/trend", { keyword, week }),
  zscoreSeries: (keyword: string, limit = 200) =>
    get<ZScoreSeriesResponse>("/v1/view/zscore-series", { keyword, limit }),
  sources: (keyword: string, week?: string) => get<SourcesResponse>("/v1/view/sources", { keyword, week }),
  generate: (week: string) => post<GenerateResponse>("/v1/view/generate", { week }),
  generateStatus: (week: string) => get<GenerateStatus>("/v1/view/generate-status", { week }),
};
