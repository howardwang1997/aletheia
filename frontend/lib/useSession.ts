"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  DatasetAsset,
  ExecutionSurface,
  LabEvent,
  createSession,
  launchRun,
  listDatasets,
  registerDataset,
  resumeRun,
  sendMessage as apiSend,
  subscribeEvents,
  uploadDataset,
} from "@/lib/api";

export interface ChatMsg {
  role: "user" | "agent";
  text: string;
  ts?: string;
}

export interface CritiqueSummary {
  critic_id?: string;
  stance?: string;
  verdict?: string;
  summary?: string;
}

export interface CritiquePanelSummary {
  target?: string;
  consensus_verdict?: string;
  gate_passed?: boolean;
  rounds?: number;
  critiques?: CritiqueSummary[];
}

export interface ClaimSummary {
  claim_type?: string;
  status?: string;
  strength?: string;
  evidence_kinds?: string[];
  claim_text?: string;
}

export interface ReportSummary {
  uri?: string;
  preview?: string;
}

function payloadOf(event: LabEvent | undefined): Record<string, unknown> {
  return event?.payload ?? {};
}

// Events shown in the activity feed (scoping + the launched lifecycle loop).
const ACTIVITY_TYPES = new Set([
  "thinking",
  "tool_use",
  "tool_result",
  "memory_log",
  "memory_recall",
  "literature",
  "survey_recorded",
  "goal_finalized",
  "tool_denied",
  "result",
  "error",
  "system",
  "data_requested",
  "data_registered",
  "run_started",
  "domain_fallback",
  "research_blocked",
  "research_degraded",
  "scorecard",
  "reproduction",
  "faithfulness",
  "retriever",
  "experiment",
  "campaign",
  "campaign_plan",
  "campaign_finished",
  "stage",
  "compute_submitted",
  "compute_status",
  "code",
  "demonstration_code",
  "preregistration",
  "demonstration",
  "claims",
  "degraded_review",
  "critique_panel",
  "critique_round",
  "iam",
  "iam_repo_created",
  "iam_pr_opened",
  "optimize",
  "budget",
  "budget_breach",
  "escalation",
  "notify",
  "report",
  "run_finished",
]);

export function useSession() {
  const [events, setEvents] = useState<LabEvent[]>([]);
  const [runId, setRunId] = useState<string | null>(null);
  const [mode, setMode] = useState<string>("");
  const [executionSurface, setExecutionSurface] = useState<ExecutionSurface | "">("");
  const [connected, setConnected] = useState(false);
  const [dryRun, setDryRun] = useState(true);
  const [nonce, setNonce] = useState(0);
  const [datasets, setDatasets] = useState<DatasetAsset[]>([]);
  const esRef = useRef<EventSource | null>(null);
  const runIdRef = useRef<string | null>(null);

  // Stable (no deps) so it never re-triggers the session effect; reads the
  // current run id from a ref. Always safe to call with an explicit id too.
  const refreshDatasets = useCallback(async (rid?: string) => {
    const id = rid ?? runIdRef.current;
    if (!id) return;
    try {
      setDatasets(await listDatasets(id));
    } catch {
      /* ignore */
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    setEvents([]);
    setConnected(false);
    setRunId(null);
    setExecutionSurface("");
    setDatasets([]);
    esRef.current?.close();

    (async () => {
      try {
        const { run_id, mode, execution_surface } = await createSession(undefined, dryRun);
        if (cancelled) return;
        setRunId(run_id);
        runIdRef.current = run_id;
        setMode(mode);
        setExecutionSurface(execution_surface);
        const es = subscribeEvents((e) => {
          setEvents((prev) => [...prev.slice(-999), e]);
          // data + finalize events change readiness -> refresh the dataset list
          if (["data_requested", "data_registered", "goal_finalized"].includes(e.type)) {
            refreshDatasets(run_id);
          }
        }, run_id);
        es.onopen = () => setConnected(true);
        es.onerror = () => setConnected(false);
        esRef.current = es;
      } catch (err) {
        setEvents((prev) => [...prev, { type: "error", payload: { error: String(err) } }]);
      }
    })();

    return () => {
      cancelled = true;
      esRef.current?.close();
    };
  }, [dryRun, nonce, refreshDatasets]);

  const send = useCallback(
    async (text: string) => {
      if (!runId || !text.trim()) return;
      try {
        await apiSend(runId, text.trim());
      } catch (err) {
        setEvents((prev) => [...prev, { type: "error", payload: { error: String(err) } }]);
      }
    },
    [runId],
  );

  const upload = useCallback(
    async (file: File, opts: { target_column?: string; description?: string; asset_id?: string }) => {
      if (!runId) return;
      try {
        await uploadDataset(runId, file, opts);
        await refreshDatasets(runId);
      } catch (err) {
        setEvents((prev) => [...prev, { type: "error", payload: { error: String(err) } }]);
      }
    },
    [runId, refreshDatasets],
  );

  const connectData = useCallback(
    async (body: { source: string; ref?: string; target_column?: string }) => {
      if (!runId) return;
      try {
        await registerDataset(runId, body);
        await refreshDatasets(runId);
      } catch (err) {
        setEvents((prev) => [...prev, { type: "error", payload: { error: String(err) } }]);
      }
    },
    [runId, refreshDatasets],
  );

  const launch = useCallback(async () => {
    if (!runId) return;
    try {
      await launchRun(runId, dryRun);
    } catch (err) {
      setEvents((prev) => [...prev, { type: "error", payload: { error: String(err) } }]);
    }
  }, [runId, dryRun]);

  const resume = useCallback(async () => {
    if (!runId) return;
    try {
      await resumeRun(runId, dryRun);
    } catch (err) {
      setEvents((prev) => [...prev, { type: "error", payload: { error: String(err) } }]);
    }
  }, [runId, dryRun]);

  const newSession = useCallback(() => setNonce((n) => n + 1), []);

  const chat = useMemo<ChatMsg[]>(
    () =>
      events
        .filter((e) => e.type === "user_message" || e.type === "assistant_text")
        .map((e) => ({
          role: e.type === "user_message" ? "user" : "agent",
          text: String(payloadOf(e).text ?? ""),
          ts: e.ts,
        })),
    [events],
  );

  const activity = useMemo(() => events.filter((e) => ACTIVITY_TYPES.has(e.type)), [events]);

  const status = useMemo(() => {
    const last = [...events].reverse().find((e) => e.type === "status");
    const payload = payloadOf(last);
    return {
      state: typeof payload.state === "string" ? payload.state : "idle",
      ...(typeof payload.detail === "string" ? { detail: payload.detail } : {}),
    };
  }, [events]);

  const cost = useMemo(() => {
    const result = [...events].reverse().find((e) => e.type === "result");
    const budget = [...events].reverse().find((e) => e.type === "budget");
    return Number(
      payloadOf(result).cost_usd ?? payloadOf(budget).cumulative ?? 0,
    );
  }, [events]);

  const finalizedPlan = useMemo(() => {
    const last = [...events].reverse().find((e) => e.type === "goal_finalized");
    const plan = payloadOf(last).plan;
    return plan && typeof plan === "object" && !Array.isArray(plan)
      ? (plan as Record<string, string>)
      : null;
  }, [events]);

  // --- launched-run lifecycle ---
  const launched = useMemo(() => events.some((e) => e.type === "run_started"), [events]);

  const stageHistory = useMemo(
    () =>
      events
        .filter((e) => e.type === "stage")
        .map((e) => String(payloadOf(e).stage ?? "")),
    [events],
  );

  const critiques = useMemo(
    () =>
      events
        .filter((e) => e.type === "critique_panel")
        .map((e) => payloadOf(e) as CritiquePanelSummary),
    [events],
  );

  // the verification spine's outcome: the final claim ledger (type/status/strength + the
  // evidence kinds that grounded each), emitted once claims are finalized.
  const claims = useMemo(() => {
    const last = [...events].reverse().find((e) => e.type === "claims");
    const claims = payloadOf(last).claims;
    return Array.isArray(claims) ? (claims as ClaimSummary[]) : null;
  }, [events]);

  // campaign: one Run -> several linked experiments (round markers)
  const experiments = useMemo(
    () =>
      events
        .filter((e) => e.type === "experiment")
        .map((e) => e.payload as { round?: number; exp_id?: string; of?: number }),
    [events],
  );

  const report = useMemo(() => {
    const last = [...events].reverse().find((e) => e.type === "report");
    return last ? (payloadOf(last) as ReportSummary) : null;
  }, [events]);

  const finalMetrics = useMemo(() => {
    const fin = [...events].reverse().find((e) => e.type === "run_finished");
    const cs = [...events].reverse().find(
      (e) => e.type === "compute_status" && payloadOf(e).metrics,
    );
    const metrics = payloadOf(fin).metrics ?? payloadOf(cs).metrics;
    return metrics && typeof metrics === "object" && !Array.isArray(metrics)
      ? (metrics as Record<string, number>)
      : null;
  }, [events]);

  const paused = useMemo(
    () => events.some((e) => e.type === "budget_breach" || e.type === "escalation"),
    [events],
  );

  return {
    runId,
    mode,
    executionSurface,
    connected,
    chat,
    activity,
    status,
    cost,
    finalizedPlan,
    datasets,
    launched,
    stageHistory,
    experiments,
    critiques,
    claims,
    report,
    finalMetrics,
    paused,
    dryRun,
    setDryRun,
    send,
    upload,
    connectData,
    launch,
    resume,
    refreshDatasets,
    newSession,
  };
}
