"use client";

import { LabEvent } from "@/lib/api";
import { Markdown } from "@/components/Markdown";
import type { ClaimSummary, CritiquePanelSummary, ReportSummary } from "@/lib/useSession";

type DisplayValue = string | number | boolean | null | undefined;

interface ThresholdSummary {
  op?: DisplayValue;
  threshold?: DisplayValue;
}

interface ActivityPayload {
  accepted?: boolean;
  amount?: DisplayValue;
  audit_refuted?: boolean;
  best_mae_lcso?: DisplayValue;
  best_round?: DisplayValue;
  branch?: DisplayValue;
  breaches?: unknown;
  candidates?: { experiment_type?: string; eig?: number }[];
  cap?: DisplayValue;
  capability?: DisplayValue;
  claims?: { claim_type?: string; status?: string }[];
  class?: DisplayValue;
  computed?: boolean;
  consensus_verdict?: DisplayValue;
  content?: unknown;
  continue?: boolean;
  control_silent_if?: ThresholdSummary;
  control_statistic?: DisplayValue;
  cost_usd?: DisplayValue;
  cumulative?: DisplayValue;
  decision?: DisplayValue;
  description?: DisplayValue;
  disagreement?: DisplayValue;
  error?: DisplayValue;
  experiments?: DisplayValue;
  fallback?: boolean;
  form?: DisplayValue;
  gaps?: DisplayValue;
  gate_passed?: boolean;
  holds?: boolean;
  job_id?: DisplayValue;
  kept?: DisplayValue;
  lines?: DisplayValue;
  mae?: DisplayValue;
  max_rounds?: number;
  methods?: DisplayValue;
  metric?: DisplayValue;
  metrics?: unknown;
  min_vendors?: DisplayValue;
  mode?: DisplayValue;
  n?: number;
  n_cases?: DisplayValue;
  n_hits?: number;
  n_vendors?: DisplayValue;
  note?: DisplayValue;
  number?: DisplayValue;
  of?: DisplayValue;
  op?: DisplayValue;
  original?: DisplayValue;
  preregistration_valid?: boolean;
  query?: DisplayValue;
  ran?: DisplayValue;
  rationale?: DisplayValue;
  reason?: DisplayValue;
  reasons?: string[];
  ref?: DisplayValue;
  repo?: DisplayValue;
  repro?: DisplayValue;
  reproduced?: boolean;
  requested?: DisplayValue;
  results_gate?: DisplayValue;
  round?: number;
  rounds?: number;
  score?: DisplayValue;
  scorer?: DisplayValue;
  scores?: Record<string, number>;
  source?: DisplayValue;
  stage?: DisplayValue;
  statistic?: DisplayValue;
  status?: DisplayValue;
  subtype?: DisplayValue;
  supported_if?: ThresholdSummary;
  target?: DisplayValue;
  test_statistic?: DisplayValue;
  text?: DisplayValue;
  tool?: DisplayValue;
  used?: DisplayValue;
}

const PHASE1_STAGES = [
  "survey",
  "ideate",
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
    case "surveying":
      return "surveying the literature…";
    case "ideating":
      return "forming a hypothesis…";
    case "scoring":
      return "scoring the hypothesis…";
    case "reproducing":
      return "reproducing the result…";
    case "designing":
      return "designing experiment…";
    case "coding":
      return "writing model code…";
    case "executing":
      return "running training…";
    case "analyzing":
      return "analyzing results…";
    case "optimizing":
      return "optimizing…";
    case "planning":
      return "planning the next experiment…";
    case "writing":
      return "writing report…";
    case "archived":
      return "run complete ✓";
    case "degraded":
      return `degraded — ${status.detail ?? ""}`;
    case "paused":
      return `paused — ${status.detail ?? ""}`;
    case "failed":
      return `failed — ${status.detail ?? ""}`;
    default:
      return status.state || "idle";
  }
}

function line(e: LabEvent): string {
  const p = (e.payload ?? {}) as ActivityPayload;
  switch (e.type) {
    case "thinking":
      return String(p.text ?? "");
    case "tool_use":
      return `${p.tool ?? "tool"}`;
    case "tool_result":
      return typeof p.content === "string" ? p.content : JSON.stringify(p.content ?? {});
    case "memory_log":
      return `📝 ${p.note ?? ""}`;
    case "memory_recall":
      return `🧠 recall "${p.query ?? ""}" — ${p.n_hits ?? 0} hit${p.n_hits === 1 ? "" : "s"}`;
    case "survey_recorded":
      return `📝 survey recorded — ${p.methods ?? 0} frontier method(s), ${p.gaps ?? 0} gap(s)`;
    case "literature":
      return p.error
        ? `📚 literature error: ${p.error}`
        : `📚 literature "${p.query ?? ""}" — ${p.n ?? 0} paper${p.n === 1 ? "" : "s"}`;
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
    case "domain_fallback":
      return `⚠ domain '${p.requested}' unsupported — ran '${p.ran}'`;
    case "scorecard": {
      const sc = (p.scores ?? {}) as Record<string, number>;
      const nov = sc.novelty != null ? ` (novelty ${Number(sc.novelty).toFixed(2)}, EIG ${Number(sc.expected_information_gain ?? 0).toFixed(2)})` : "";
      return `🎯 scorecard: ${p.decision ?? ""}${nov}${p.reason ? ` — ${p.reason}` : ""}`;
    }
    case "reproduction":
      return p.error
        ? `🔁 reproduction failed: ${p.error}`
        : `🔁 reproduction ${p.reproduced ? "confirmed ✓" : "NOT confirmed ✗"} (${p.metric ?? ""}: ${p.original ?? "?"} → ${p.repro ?? "?"})`;
    case "retriever":
      return `🔎 retriever: ${p.used ?? ""}${p.fallback ? ` (requested ${p.requested}, fell back — dry-run)` : ""}`;
    case "faithfulness":
      return `🧷 faithfulness ${p.score != null ? Number(p.score).toFixed(2) : "n/a"} (${p.n_cases ?? 0} cases, ${p.scorer ?? "panel"})`;
    case "research_blocked":
      return `🛑 run blocked — ${p.reason ?? ""}`;
    case "research_degraded":
      return `⚠ degraded — ${p.reason ?? ""}`;
    case "experiment":
      return `🧪 experiment ${p.round ?? ""}${p.of ? `/${p.of}` : ""}`;
    case "campaign":
      return `🧭 campaign: ${p.decision ?? ""}${p.rationale ? ` — ${p.rationale}` : ""}`;
    case "campaign_plan": {
      const cands = (p.candidates ?? []) as { experiment_type?: string; eig?: number }[];
      const summary = cands.map((c) => `${c.experiment_type}·EIG ${Number(c.eig ?? 0).toFixed(2)}`).join(", ");
      return `🗺 plan: ${p.continue ? "continue" : "stop"}${summary ? ` — ${summary}` : ""}${p.rationale ? ` (${p.rationale})` : ""}`;
    }
    case "campaign_finished":
      return `🏁 campaign done — best LCSO ${p.best_mae_lcso ?? "?"} (round ${p.best_round ?? "?"}, ${p.experiments ?? "?"} exps)`;
    case "stage":
      return `→ ${p.stage ?? ""}: ${p.rationale ?? ""}`;
    case "compute_submitted":
      return `🧮 job ${p.job_id ?? ""} submitted`;
    case "code":
      return `⌨ solution ${p.accepted ? "accepted" : "rejected"} (${p.lines ?? 0} lines)${
        !p.accepted && p.reasons?.length ? ` — ${p.reasons.join("; ")}` : ""
      }`;
    case "compute_status":
      return `🧮 job ${p.job_id ?? ""}: ${p.status ?? ""}${p.metrics ? ` ${JSON.stringify(p.metrics)}` : ""}`;
    case "demonstration_code":
      return `🧬 AI-authored demonstration ${p.accepted ? "accepted" : "rejected"} (${p.lines ?? 0} lines, prereg ${p.preregistration_valid ? "✓" : "✗"})${
        !p.accepted && p.reasons?.length ? ` — ${p.reasons.join("; ")}` : ""
      }`;
    case "preregistration": {
      const si = p.supported_if ? `${p.supported_if.op} ${p.supported_if.threshold}` : "?";
      const cs = p.control_silent_if ? `${p.control_silent_if.op} ${p.control_silent_if.threshold}` : "?";
      return `📜 pre-registered "${p.statistic ?? "statistic"}" — supported if ${si}; control silent if ${cs}`;
    }
    case "demonstration": {
      if (p.computed === false) return `🔬 demonstration not computed${p.error ? ` — ${p.error}` : ""}`;
      const verdict = p.audit_refuted ? "REFUTED by audit ✗" : p.holds === true ? "holds ✓" : p.holds === false ? "does not hold ✗" : "not evaluated";
      const tc =
        p.test_statistic != null && p.control_statistic != null
          ? ` — test ${Number(p.test_statistic).toFixed(3)} vs control ${Number(p.control_statistic).toFixed(3)}`
          : p.statistic != null
            ? ` — statistic ${Number(p.statistic).toFixed(3)}`
            : "";
      return `🔬 demonstration (${p.form ?? "?"}): ${verdict}${tc}${p.capability ? ` [${p.capability}]` : ""}`;
    }
    case "degraded_review":
      return `⚠ degraded review — ${p.reason ?? `${p.n_vendors ?? "?"}/${p.min_vendors ?? "?"} vendors`}`;
    case "claims": {
      const cs = (p.claims ?? []) as { claim_type?: string; status?: string }[];
      return `📒 claims: ${cs.map((c) => `${c.claim_type} ${c.status}`).join(", ")}`;
    }
    case "critique_panel":
      return `⚖ ${p.target}: ${p.consensus_verdict} (gate ${p.gate_passed ? "✓" : "✗"}${p.rounds ? `, ${p.rounds} round${p.rounds > 1 ? "s" : ""}` : ""})`;
    case "critique_round":
      return `🔁 ${p.target} round ${p.round}/${p.max_rounds} — disagreement ${p.disagreement}`;
    case "optimize":
      return `🔧 kept ${p.kept} (${p.metric ?? "mae"} ${p.score ?? p.mae})`;
    case "iam_repo_created":
      return `📁 repo ${p.repo ?? ""}`;
    case "iam_pr_opened":
      return `🔀 PR #${p.number ?? ""} ${p.repo ?? ""}`;
    case "iam":
      return `🔐 ${p.op ?? ""}${p.branch ? ` ${p.branch}` : ""}${p.reason ? ` — ${p.reason}` : ""}${p.error ? ` — ${p.error}` : ""}`;
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
      return p.results_gate === "rejected" || p.status === "results_rejected"
        ? `⚠ run finished — results rejected by peer review`
        : `✓ run ${p.status ?? ""}`;
    case "result":
      return `result · $${p.cost_usd ?? 0}`;
    case "error":
      return `✖ ${p.error ?? ""}`;
    case "system":
      return String(p.subtype ?? p.class ?? "system");
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

function Verdicts({ critiques }: { critiques: CritiquePanelSummary[] }) {
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
            {c.rounds ? <span className="v-rounds"> · {c.rounds} round{c.rounds > 1 ? "s" : ""}</span> : null}
          </div>
          {(c.critiques ?? []).map((cc, j) => (
            <div key={j} className="v-sub">
              [{cc.critic_id}/{cc.stance}] {cc.verdict}: {cc.summary}
            </div>
          ))}
        </div>
      ))}
    </div>
  );
}

// the verification spine made visible: each claim's status drives the colour, so a viewer can
// see at a glance what was established (supported), contradicted (refuted), tested-but-unendorsed
// (unverified), or never run (not_evaluated). Pre-registration / audit show as evidence kinds.
const CLAIM_STATUS_CLASS: Record<string, string> = {
  supported: "cl-supported",
  refuted: "cl-refuted",
  unverified: "cl-unverified",
  not_evaluated: "cl-skip",
};

function ClaimsPanel({ claims }: { claims: ClaimSummary[] }) {
  return (
    <div className="claims-card">
      <div className="plan-title">📒 Claim ledger</div>
      {claims.map((c, i) => (
        <div key={i} className="claim-row" title={c.claim_text ?? ""}>
          <span className="claim-type">{c.claim_type}</span>
          <span className={`claim-status ${CLAIM_STATUS_CLASS[c.status ?? ""] ?? "cl-skip"}`}>
            {(c.status ?? "—").replace(/_/g, " ")}
          </span>
          <span className="claim-strength">{c.strength}</span>
          {c.evidence_kinds?.length ? (
            <span className="claim-evidence">{c.evidence_kinds.join(" · ")}</span>
          ) : null}
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
  experiments = [],
  critiques = [],
  claims = null,
  report = null,
  finalMetrics = null,
}: {
  activity: LabEvent[];
  status: { state: string; detail?: string };
  cost: number;
  finalizedPlan: Record<string, string> | null;
  stageHistory?: string[];
  experiments?: { round?: number; exp_id?: string; of?: number }[];
  critiques?: CritiquePanelSummary[];
  claims?: ClaimSummary[] | null;
  report?: ReportSummary | null;
  finalMetrics?: Record<string, number> | null;
}) {
  return (
    <aside className="activity">
      <div className="act-head">
        <strong>Activity</strong>
        <span className="cost">${cost.toFixed(3)}</span>
      </div>

      <div className={`pill s-${status.state}`}>{statusLabel(status)}</div>

      {experiments.length > 1 && (
        <div className="campaign-bar">
          Campaign — experiment {experiments.length}
          {experiments[0]?.of ? ` of ${experiments[0].of}` : ""}
        </div>
      )}

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

      {claims && claims.length > 0 && <ClaimsPanel claims={claims} />}

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
          <div className="report-md">
            <Markdown>{report.preview}</Markdown>
          </div>
        </div>
      )}

      {finalizedPlan && <PlanCard plan={finalizedPlan} />}
    </aside>
  );
}
