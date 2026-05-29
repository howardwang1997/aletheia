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

// --- datasets (the human's connect-data step) ---

export interface DatasetAsset {
  id: string;
  source: string;
  ref?: string | null;
  status: string;
  target_column?: string | null;
  description?: string | null;
  profile?: Record<string, unknown> | null;
  requested_by?: string;
}

export async function listDatasets(runId: string) {
  const res = await fetch(`${API_BASE}/runs/${runId}/datasets`);
  if (!res.ok) throw new Error(`listDatasets failed: ${res.status}`);
  return (await res.json()) as DatasetAsset[];
}

export async function uploadDataset(
  runId: string,
  file: File,
  opts: { target_column?: string; description?: string; asset_id?: string } = {},
) {
  const form = new FormData();
  form.append("file", file);
  if (opts.target_column) form.append("target_column", opts.target_column);
  if (opts.description) form.append("description", opts.description);
  if (opts.asset_id) form.append("asset_id", opts.asset_id);
  const res = await fetch(`${API_BASE}/runs/${runId}/datasets/upload`, {
    method: "POST",
    body: form,
  });
  if (!res.ok) throw new Error(`uploadDataset failed: ${res.status}`);
  return await res.json();
}

export async function registerDataset(
  runId: string,
  body: {
    source: string; // benchmark | directory | url
    ref?: string;
    target_column?: string;
    description?: string;
  },
) {
  const res = await fetch(`${API_BASE}/runs/${runId}/datasets`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const b = await res.json().catch(() => ({}));
    throw new Error(`registerDataset failed: ${res.status} ${JSON.stringify(b.detail ?? "")}`);
  }
  return await res.json();
}

export async function satisfyDataset(runId: string, assetId: string) {
  const res = await fetch(`${API_BASE}/runs/${runId}/datasets/${assetId}/ready`, {
    method: "POST",
  });
  if (!res.ok) throw new Error(`satisfyDataset failed: ${res.status}`);
  return await res.json();
}

// --- launch / resume the autonomous loop ---

export async function launchRun(runId: string, dryRun: boolean | null = null) {
  const res = await fetch(`${API_BASE}/runs/${runId}/launch`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ dry_run: dryRun }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(
      res.status === 409
        ? `data not ready: ${JSON.stringify((body.detail || {}).pending || [])}`
        : `launchRun failed: ${res.status}`,
    );
  }
  return (await res.json()) as { run_id: string; status: string; mode: string };
}

export async function resumeRun(runId: string, dryRun: boolean | null = null) {
  const res = await fetch(`${API_BASE}/runs/${runId}/resume`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ dry_run: dryRun }),
  });
  if (!res.ok) throw new Error(`resumeRun failed: ${res.status}`);
  return await res.json();
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
