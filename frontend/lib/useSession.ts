"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  LabEvent,
  createSession,
  sendMessage as apiSend,
  subscribeEvents,
} from "@/lib/api";

export interface ChatMsg {
  role: "user" | "agent";
  text: string;
  ts?: string;
}

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
]);

export function useSession() {
  const [events, setEvents] = useState<LabEvent[]>([]);
  const [runId, setRunId] = useState<string | null>(null);
  const [mode, setMode] = useState<string>("");
  const [connected, setConnected] = useState(false);
  const [dryRun, setDryRun] = useState(true);
  const [nonce, setNonce] = useState(0);
  const esRef = useRef<EventSource | null>(null);

  useEffect(() => {
    let cancelled = false;
    setEvents([]);
    setConnected(false);
    setRunId(null);
    esRef.current?.close();

    (async () => {
      try {
        const { run_id, mode } = await createSession(undefined, dryRun);
        if (cancelled) return;
        setRunId(run_id);
        setMode(mode);
        const es = subscribeEvents(
          (e) => setEvents((prev) => [...prev.slice(-999), e]),
          run_id,
        );
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
  }, [dryRun, nonce]);

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

  const activity = useMemo(
    () => events.filter((e) => ACTIVITY_TYPES.has(e.type)),
    [events],
  );

  const status = useMemo(() => {
    const last = [...events].reverse().find((e) => e.type === "status");
    return (last?.payload as any) ?? { state: "idle" };
  }, [events]);

  const cost = useMemo(() => {
    const last = [...events].reverse().find((e) => e.type === "result");
    return Number((last?.payload as any)?.cost_usd ?? 0);
  }, [events]);

  const finalizedPlan = useMemo(() => {
    const last = [...events].reverse().find((e) => e.type === "goal_finalized");
    return ((last?.payload as any)?.plan as Record<string, string>) ?? null;
  }, [events]);

  return {
    runId,
    mode,
    connected,
    chat,
    activity,
    status,
    cost,
    finalizedPlan,
    dryRun,
    setDryRun,
    send,
    newSession,
  };
}
