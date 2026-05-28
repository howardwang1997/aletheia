"use client";

import { useEffect, useRef, useState } from "react";
import { LabEvent, startRun, subscribeEvents } from "@/lib/api";

function bodyText(e: LabEvent): string {
  const p = (e.payload ?? {}) as Record<string, any>;
  switch (e.type) {
    case "assistant_text":
    case "thinking":
      return p.text ?? "";
    case "tool_use":
      return `${p.tool ?? "tool"}(${JSON.stringify(p.input ?? {})})`;
    case "tool_result":
      return typeof p.content === "string" ? p.content : JSON.stringify(p.content ?? {});
    case "memory_log":
      return `📝 ${p.note ?? ""}  (#${p.decision_id ?? "?"})`;
    case "result":
      return `result: ${p.result ?? ""}  cost=$${p.cost_usd ?? 0}`;
    case "run_started":
      return `▶ ${p.prompt ?? ""}  [${p.mode ?? ""}]`;
    case "run_finished":
      return `■ ${p.status ?? "finished"}`;
    case "error":
      return `✖ ${p.error ?? ""}`;
    default:
      return JSON.stringify(p);
  }
}

export default function Home() {
  const [events, setEvents] = useState<LabEvent[]>([]);
  const [goal, setGoal] = useState("Introduce yourself and log a one-line plan.");
  const [connected, setConnected] = useState(false);
  const [busy, setBusy] = useState(false);
  const [dryRun, setDryRun] = useState(true);
  const streamRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const es = subscribeEvents((e) => setEvents((prev) => [...prev.slice(-499), e]));
    es.onopen = () => setConnected(true);
    es.onerror = () => setConnected(false);
    return () => es.close();
  }, []);

  useEffect(() => {
    streamRef.current?.scrollTo({ top: streamRef.current.scrollHeight });
  }, [events]);

  async function onStart() {
    setBusy(true);
    try {
      await startRun(goal, dryRun);
    } catch (err) {
      setEvents((prev) => [...prev, { type: "error", payload: { error: String(err) } }]);
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="app">
      <div className="header">
        <h1>Aletheia</h1>
        <span className="sub">lights-out lab · Live Activity</span>
        <span className="status">{connected ? "● live" : "○ disconnected"}</span>
      </div>

      <div className="controls">
        <input
          value={goal}
          onChange={(e) => setGoal(e.target.value)}
          placeholder="Goal / instruction for the lab…"
          onKeyDown={(e) => e.key === "Enter" && !busy && onStart()}
        />
        <button onClick={onStart} disabled={busy}>
          {busy ? "Starting…" : "Start run"}
        </button>
        <label style={{ display: "flex", alignItems: "center", gap: 6, whiteSpace: "nowrap" }}>
          <input
            type="checkbox"
            checked={dryRun}
            onChange={(e) => setDryRun(e.target.checked)}
            style={{ flex: "0 0 auto" }}
          />
          dry-run (no quota)
        </label>
      </div>

      <div className="stream" ref={streamRef}>
        {events.length === 0 && (
          <div className="evt">
            <span className="body" style={{ color: "var(--muted)" }}>
              Waiting for events… start a run above.
            </span>
          </div>
        )}
        {events.map((e, i) => (
          <div className={`evt t-${e.type}`} key={e.id ?? `i${i}`}>
            <span className="badge">{e.type}</span>
            <span className="agent">{e.agent ?? "—"}</span>
            <span className="body">{bodyText(e)}</span>
            <span className="ts">{e.ts?.slice(11, 19) ?? ""}</span>
          </div>
        ))}
      </div>
    </main>
  );
}
