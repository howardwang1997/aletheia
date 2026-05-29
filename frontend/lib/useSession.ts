"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  DatasetAsset,
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

// Events shown in the activity feed (scoping + the launched lifecycle loop).
const ACTIVITY_TYPES = new Set([
  "thinking",
  "tool_use",
  "tool_result",
  "memory_log",
  "goal_finalized",
  "tool_denied",
  "result",
  "error",
  "system",
  "data_requested",
  "data_registered",
  "run_started",
  "stage",
  "compute_submitted",
  "compute_status",
  "critique_panel",
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
    setDatasets([]);
    esRef.current?.close();

    (async () => {
      try {
        const { run_id, mode } = await createSession(undefined, dryRun);
        if (cancelled) return;
        setRunId(run_id);
        runIdRef.current = run_id;
        setMode(mode);
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
          text: String((e.payload as any)?.text ?? ""),
          ts: e.ts,
        })),
    [events],
  );

  const activity = useMemo(() => events.filter((e) => ACTIVITY_TYPES.has(e.type)), [events]);

  const status = useMemo(() => {
    const last = [...events].reverse().find((e) => e.type === "status");
    return (last?.payload as any) ?? { state: "idle" };
  }, [events]);

  const cost = useMemo(() => {
    const result = [...events].reverse().find((e) => e.type === "result");
    const budget = [...events].reverse().find((e) => e.type === "budget");
    return Number(
      (result?.payload as any)?.cost_usd ?? (budget?.payload as any)?.cumulative ?? 0,
    );
  }, [events]);

  const finalizedPlan = useMemo(() => {
    const last = [...events].reverse().find((e) => e.type === "goal_finalized");
    return ((last?.payload as any)?.plan as Record<string, string>) ?? null;
  }, [events]);

  // --- launched-run lifecycle ---
  const launched = useMemo(() => events.some((e) => e.type === "run_started"), [events]);

  const stageHistory = useMemo(
    () =>
      events
        .filter((e) => e.type === "stage")
        .map((e) => String((e.payload as any)?.stage ?? "")),
    [events],
  );

  const critiques = useMemo(
    () => events.filter((e) => e.type === "critique_panel").map((e) => e.payload as any),
    [events],
  );

  const report = useMemo(() => {
    const last = [...events].reverse().find((e) => e.type === "report");
    return (last?.payload as any) ?? null;
  }, [events]);

  const finalMetrics = useMemo(() => {
    const fin = [...events].reverse().find((e) => e.type === "run_finished");
    const cs = [...events].reverse().find(
      (e) => e.type === "compute_status" && (e.payload as any)?.metrics,
    );
    return ((fin?.payload as any)?.metrics ?? (cs?.payload as any)?.metrics ?? null) as
      | Record<string, number>
      | null;
  }, [events]);

  const paused = useMemo(
    () => events.some((e) => e.type === "budget_breach" || e.type === "escalation"),
    [events],
  );

  return {
    runId,
    mode,
    connected,
    chat,
    activity,
    status,
    cost,
    finalizedPlan,
    datasets,
    launched,
    stageHistory,
    critiques,
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
