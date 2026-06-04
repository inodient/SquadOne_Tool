// /v1/view/* 응답 타입 (rest_mcp_server/views.py 와 1:1).

export interface WeeksResponse {
  weeks: string[];
  latest: string | null;
}

export interface Summary {
  week: string;
  keyword_count: number;
  status_counts: Record<string, number>;
  emerging: number;
  active: number;
  fading: number;
  archived: number;
  product_count: number;
}

export interface Product {
  week: string;
  rank: number;
  product_name: string | null;
  selection_reason: string | null;
}

export interface Report {
  step: string;
  week: string;
  payload: Record<string, unknown>;
  meta: Record<string, unknown> | null;
  created_at: string | null;
}

export interface RecommendationsResponse {
  week: string;
  products: Product[];
  reports: Report[];
}

export interface Trend {
  week: string;
  keyword: string;
  trend_slot_id: string | null;
  group_id: string | null;
  group_score: number | null;
  status: string | null;
  status_reason: string | null;
  z_score: number | null;
  count: number | null;
  weekly_summary: string | null;
  evidence_doc_ids: string[] | null;
  noise_classes: string[] | null;
}

export interface TrendsResponse {
  week: string;
  trends: Trend[];
}

export interface ExcludedTrend {
  week: string;
  keyword: string;
  z_score: number | null;
  noise_classes: string[] | null;
}

export interface ExcludedTrendsResponse {
  week: string;
  excluded: ExcludedTrend[];
}

export interface Group {
  week: string;
  group_id: string;
  members: unknown;
  group_score: number | null;
  cohesion: number | null;
}

export interface GroupsResponse {
  week: string;
  groups: Group[];
}

export interface Context {
  doc_id: string;
  score: number | null;
  snippet: string | null;
}

export interface KeySentence {
  query_text: string | null;
  key_sentence: string | null;
  evidence_doc_ids: string[] | null;
  evidence_count: number | null;
}

export interface TrendDetailResponse {
  week: string;
  keyword: string;
  timeseries: Trend | null;
  contexts: Context[];
  keysentence: KeySentence | null;
}

export interface ZScorePoint {
  week: string;
  z_score: number;
}

export interface ZScoreSeriesResponse {
  keyword: string;
  series: ZScorePoint[];
}

export interface SourcePoint {
  source: string;
  count: number;
}

export interface SourcesResponse {
  week: string;
  keyword: string;
  sources: SourcePoint[];
}

export type JobStatus = "idle" | "running" | "success" | "failed";

export interface GenerateStatus {
  week: string;
  status: JobStatus;
  step?: string;
  message?: string;
  error?: string | null;
  product_count?: number | null;
  elapsed?: number;
  started_at?: string;
  finished_at?: string | null;
}

export interface GenerateResponse {
  started: boolean;
  job: GenerateStatus;
}

// ── 기간(범위) 추적 (/v1/view/range/*) ──────────────────────────

export interface LifecycleRangeRow {
  week: string;
  status: string | null;
  count: number;
}

export interface LifecycleRangeResponse {
  from: string;
  to: string;
  rows: LifecycleRangeRow[];
}

export interface ZScoreMatrixRow {
  week: string;
  keyword: string;
  z_score: number | null;
}

export interface ZScoreRangeResponse {
  from: string;
  to: string;
  keywords: string[];
  rows: ZScoreMatrixRow[];
}

export interface ProductRangeRow {
  week: string;
  rank: number;
  product_name: string | null;
  selection_reason: string | null;
}

export interface ProductsRangeResponse {
  from: string;
  to: string;
  rows: ProductRangeRow[];
}

export interface KeySentenceRangeRow {
  week: string;
  keysentence_count: number;
  evidence_total: number;
}

export interface KeySentenceRangeResponse {
  from: string;
  to: string;
  rows: KeySentenceRangeRow[];
}
