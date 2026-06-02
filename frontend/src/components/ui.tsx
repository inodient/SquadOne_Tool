import type { ReactNode } from "react";

export const STATUS_ORDER = ["Emerging", "Active", "Fading", "Archived"] as const;
export type Status = (typeof STATUS_ORDER)[number];

export const STATUS_COLOR: Record<string, string> = {
  Emerging: "var(--emerging)",
  Active: "var(--active)",
  Fading: "var(--fading)",
  Archived: "var(--archived)",
};

export const STATUS_LABEL: Record<string, string> = {
  Emerging: "부상",
  Active: "활성",
  Fading: "쇠퇴",
  Archived: "종료",
};

export function StatusBadge({ status }: { status: string | null }) {
  if (!status) return null;
  const color = STATUS_COLOR[status] ?? "var(--muted)";
  return (
    <span className="badge" style={{ background: color + "22", color, border: `1px solid ${color}55` }}>
      {STATUS_LABEL[status] ?? status}
    </span>
  );
}

export function Loading() {
  return <div className="spinner">불러오는 중…</div>;
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>;
}

export function ErrorBox({ message }: { message: string }) {
  return (
    <div className="empty error">
      API 호출 실패
      <div className="muted" style={{ marginTop: 8, fontSize: 13 }}>{message}</div>
      <div className="muted" style={{ marginTop: 8, fontSize: 13 }}>
        REST 서버(<code>uvicorn rest_mcp_server.app:app --port 8000</code>)가 켜져 있는지 확인하세요.
      </div>
    </div>
  );
}

// 차트 팔레트(버블/도넛 공용).
export const PALETTE = [
  "#4f9cf9", "#2ecc71", "#f5a623", "#9b8cff", "#ff6b6b",
  "#1abc9c", "#e67e22", "#e84393", "#00cec9", "#fab1a0",
];
