"use client";

import { LabEvent } from "@/lib/api";

function statusLabel(status: { state: string; detail?: string }): string {
  switch (status.state) {
    case "thinking":
      return "thinking…";
    case "using_tool":
      return `using tool: ${status.detail ?? ""}`;
    case "awaiting_user":
      return "awaiting your reply";
    case "finalized":
      return "goal finalized ✓";
    default:
      return status.state || "idle";
  }
}

function line(e: LabEvent): string {
  const p = (e.payload ?? {}) as Record<string, any>;
  switch (e.type) {
    case "thinking":
      return p.text ?? "";
    case "tool_use":
      return `${p.tool ?? "tool"}`;
    case "tool_result":
      return typeof p.content === "string" ? p.content : JSON.stringify(p.content ?? {});
    case "memory_log":
      return `📝 ${p.note ?? ""}`;
    case "goal_finalized":
      return "experiment plan recorded";
    case "tool_denied":
      return `⛔ ${p.tool ?? ""} denied`;
    case "result":
      return `result · $${p.cost_usd ?? 0}`;
    case "error":
      return `✖ ${p.error ?? ""}`;
    case "system":
      return p.subtype ?? p.class ?? "system";
    default:
      return JSON.stringify(p);
  }
}

function PlanCard({ plan }: { plan: Record<string, string> }) {
  const order = [
    "objective",
    "domain",
    "direction",
    "hypothesis",
    "dataset",
    "method",
    "baselines",
    "metrics",
    "success_criteria",
    "risks",
    "est_compute",
  ];
  return (
    <div className="plan-card">
      <div className="plan-title">📋 Finalized experiment plan</div>
      {order
        .filter((k) => plan[k])
        .map((k) => (
          <div className="plan-row" key={k}>
            <span className="plan-k">{k.replace(/_/g, " ")}</span>
            <span className="plan-v">{plan[k]}</span>
          </div>
        ))}
    </div>
  );
}

export function Activity({
  activity,
  status,
  cost,
  finalizedPlan,
}: {
  activity: LabEvent[];
  status: { state: string; detail?: string };
  cost: number;
  finalizedPlan: Record<string, string> | null;
}) {
  return (
    <aside className="activity">
      <div className="act-head">
        <strong>Activity</strong>
        <span className="cost">${cost.toFixed(3)}</span>
      </div>

      <div className={`pill s-${status.state}`}>{statusLabel(status)}</div>

      <div className="act-feed">
        {activity.length === 0 && <div className="hint">No activity yet.</div>}
        {activity.map((e, i) => (
          <div key={e.id ?? i} className={`act t-${e.type}`}>
            <span className="k">{e.type}</span>
            <span className="v">{line(e)}</span>
          </div>
        ))}
      </div>

      {finalizedPlan && <PlanCard plan={finalizedPlan} />}
    </aside>
  );
}
