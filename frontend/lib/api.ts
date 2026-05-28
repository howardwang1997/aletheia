// Thin client over the FastAPI backend. Keeping all server access behind this
// module preserves the "reserved App capability": a future Expo/Tauri app can
// reuse the same contract.

export const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000";

export interface LabEvent {
  id?: number;
  run_id?: string | null;
  agent?: string;
  parent_tool_use_id?: string | null;
  type: string;
  payload?: Record<string, unknown>;
  ts?: string;
}

export async function startRun(goal: string, dryRun: boolean | null = null) {
  const res = await fetch(`${API_BASE}/runs`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ goal, dry_run: dryRun }),
  });
  if (!res.ok) throw new Error(`startRun failed: ${res.status}`);
  return (await res.json()) as { run_id: string; mode: string };
}

export async function listRuns() {
  const res = await fetch(`${API_BASE}/runs`);
  if (!res.ok) throw new Error(`listRuns failed: ${res.status}`);
  return (await res.json()) as Array<Record<string, unknown>>;
}

// --- conversational scoping sessions ---

export async function createSession(goalSeed?: string, dryRun: boolean | null = null) {
  const res = await fetch(`${API_BASE}/sessions`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ goal_seed: goalSeed || null, dry_run: dryRun }),
  });
  if (!res.ok) throw new Error(`createSession failed: ${res.status}`);
  return (await res.json()) as { run_id: string; mode: string };
}

export async function sendMessage(runId: string, text: string) {
  const res = await fetch(`${API_BASE}/sessions/${runId}/messages`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
  });
  if (!res.ok) throw new Error(`sendMessage failed: ${res.status}`);
  return await res.json();
}

export async function interruptSession(runId: string) {
  await fetch(`${API_BASE}/sessions/${runId}/interrupt`, { method: "POST" });
}

export function subscribeEvents(
  onEvent: (e: LabEvent) => void,
  runId?: string,
): EventSource {
  const url = new URL(`${API_BASE}/events`);
  if (runId) url.searchParams.set("run_id", runId);
  const es = new EventSource(url.toString());
  es.onmessage = (msg) => {
    try {
      onEvent(JSON.parse(msg.data) as LabEvent);
    } catch {
      /* ignore malformed frames */
    }
  };
  return es;
}
