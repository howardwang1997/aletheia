"use client";

import { LabEvent } from "@/lib/api";

const PHASE1_STAGES = [
  "experiment_design",
  "execution",
  "analysis",
  "optimize",
  "write_up",
  "archive",
];

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
    case "designing":
      return "designing experiment…";
    case "executing":
      return "running training…";
    case "analyzing":
      return "analyzing results…";
    case "optimizing":
      return "optimizing…";
    case "writing":
      return "writing report…";
    case "archived":
      return "run complete ✓";
    case "paused":
      return `paused — ${status.detail ?? ""}`;
    case "failed":
      return `failed — ${status.detail ?? ""}`;
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
    case "data_requested":
      return `📥 needs data: ${p.source ?? ""} ${p.ref ?? ""} — ${p.description ?? ""}`;
    case "data_registered":
      return `📦 data ${p.status ?? ""}: ${p.source ?? ""} ${p.ref ?? ""}`;
    case "run_started":
      return `▶ run started (${p.mode ?? ""})`;
    case "stage":
      return `→ ${p.stage ?? ""}: ${p.rationale ?? ""}`;
    case "compute_submitted":
      return `🧮 job ${p.job_id ?? ""} submitted`;
    case "compute_status":
      return `🧮 job ${p.job_id ?? ""}: ${p.status ?? ""}${p.metrics ? ` ${JSON.stringify(p.metrics)}` : ""}`;
    case "critique_panel":
      return `⚖ ${p.target}: ${p.consensus_verdict} (gate ${p.gate_passed ? "✓" : "✗"})`;
    case "optimize":
      return `🔧 kept ${p.kept} (MAE ${p.mae})`;
    case "budget":
      return `💰 +$${p.amount} → $${Number(p.cumulative ?? 0).toFixed(2)} / $${p.cap}`;
    case "budget_breach":
      return `🛑 budget breach: ${JSON.stringify(p.breaches ?? [])}`;
    case "escalation":
      return `🚨 ${p.reason ?? ""}`;
    case "notify":
      return `🔔 ${p.text ?? ""}`;
    case "report":
      return `📄 report written`;
    case "run_finished":
      return `✓ run ${p.status ?? ""}`;
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
    "objective", "domain", "direction", "hypothesis", "dataset",
    "method", "baselines", "metrics", "success_criteria", "risks", "est_compute",
  ];
  return (
    <div className="plan-card">
      <div className="plan-title">📋 Finalized experiment plan</div>
      {order.filter((k) => plan[k]).map((k) => (
        <div className="plan-row" key={k}>
          <span className="plan-k">{k.replace(/_/g, " ")}</span>
          <span className="plan-v">{plan[k]}</span>
        </div>
      ))}
    </div>
  );
}

function StageTimeline({ reached }: { reached: string[] }) {
  const current = reached[reached.length - 1];
  return (
    <div className="timeline">
      {PHASE1_STAGES.map((s) => {
        const done = reached.includes(s);
        const isCurrent = s === current;
        return (
          <span key={s} className={`tl ${done ? "done" : ""} ${isCurrent ? "cur" : ""}`}>
            {s.replace(/_/g, " ")}
          </span>
        );
      })}
    </div>
  );
}

function Verdicts({ critiques }: { critiques: any[] }) {
  return (
    <div className="verdicts">
      <div className="v-title">⚖ Critic verdicts</div>
      {critiques.map((c, i) => (
        <div key={i} className="v-panel">
          <div className="v-row">
            <b>{c.target}</b> → {c.consensus_verdict}{" "}
            <span className={c.gate_passed ? "gate-ok" : "gate-no"}>
              gate {c.gate_passed ? "passed" : "failed"}
            </span>
          </div>
          {(c.critiques ?? []).map((cc: any, j: number) => (
            <div key={j} className="v-sub">
              [{cc.critic_id}/{cc.stance}] {cc.verdict}: {cc.summary}
            </div>
          ))}
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
  stageHistory = [],
  critiques = [],
  report = null,
  finalMetrics = null,
}: {
  activity: LabEvent[];
  status: { state: string; detail?: string };
  cost: number;
  finalizedPlan: Record<string, string> | null;
  stageHistory?: string[];
  critiques?: any[];
  report?: { uri?: string; preview?: string } | null;
  finalMetrics?: Record<string, number> | null;
}) {
  return (
    <aside className="activity">
      <div className="act-head">
        <strong>Activity</strong>
        <span className="cost">${cost.toFixed(3)}</span>
      </div>

      <div className={`pill s-${status.state}`}>{statusLabel(status)}</div>

      {stageHistory.length > 0 && <StageTimeline reached={stageHistory} />}

      {finalMetrics && (
        <div className="metrics-card">
          <div className="plan-title">📊 Metrics</div>
          {Object.entries(finalMetrics).map(([k, v]) => (
            <div className="plan-row" key={k}>
              <span className="plan-k">{k}</span>
              <span className="plan-v">{Number(v).toFixed(4)}</span>
            </div>
          ))}
        </div>
      )}

      {critiques.length > 0 && <Verdicts critiques={critiques} />}

      <div className="act-feed">
        {activity.length === 0 && <div className="hint">No activity yet.</div>}
        {activity.map((e, i) => (
          <div key={e.id ?? i} className={`act t-${e.type}`}>
            <span className="k">{e.type}</span>
            <span className="v">{line(e)}</span>
          </div>
        ))}
      </div>

      {report?.preview && (
        <div className="report-card">
          <div className="plan-title">📄 Report</div>
          <pre className="report-pre">{report.preview}</pre>
        </div>
      )}

      {finalizedPlan && <PlanCard plan={finalizedPlan} />}
    </aside>
  );
}
