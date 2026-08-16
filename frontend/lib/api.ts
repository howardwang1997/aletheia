// Thin client over the FastAPI backend. Keeping all server access behind this
// module preserves the "reserved App capability": a future Expo/Tauri app can
// reuse the same contract. Every call sends the session cookie (credentials:
// "include"); the backend gates all non-/auth endpoints behind it.

export const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000";

// merge caller init with credentials so the session cookie always rides along
function withCreds(init: RequestInit = {}): RequestInit {
  return { credentials: "include", ...init };
}

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
  const res = await fetch(`${API_BASE}/runs`, withCreds({
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ goal, dry_run: dryRun }),
  }));
  if (!res.ok) throw new Error(`startRun failed: ${res.status}`);
  return (await res.json()) as { run_id: string; mode: string };
}

export async function listRuns() {
  const res = await fetch(`${API_BASE}/runs`, withCreds());
  if (!res.ok) throw new Error(`listRuns failed: ${res.status}`);
  return (await res.json()) as Array<Record<string, unknown>>;
}

// --- durable Quest / Program / Campaign graph ---

export interface ResearchGraphNode {
  node_id: string;
  quest_id: string;
  parent_node_id?: string | null;
  node_type: "quest" | "program" | "campaign";
  state: string;
  state_version: number;
  spec: Record<string, unknown>;
}

export interface QuestGraphSnapshot {
  quest_id: string;
  nodes: ResearchGraphNode[];
  transitions: Array<Record<string, unknown>>;
  dependencies: Array<Record<string, unknown>>;
  scientific_families: Array<Record<string, unknown>>;
  external_bindings: Array<Record<string, unknown>>;
  data_allocations: Array<Record<string, unknown>>;
  budget_allocations: Array<Record<string, unknown>>;
  graph_sha256: string;
}

export async function listQuestGraphs(): Promise<QuestGraphSnapshot[]> {
  const res = await fetch(`${API_BASE}/research-graph/quests`, withCreds());
  if (!res.ok) throw new Error(`listQuestGraphs failed: ${res.status}`);
  return (await res.json()) as QuestGraphSnapshot[];
}

// --- auth ---

export interface AuthUser {
  id: string;
  email?: string | null;
  display_name?: string | null;
  role: string;
}

export type AuthProviders = Record<string, boolean>;

export async function getProviders() {
  const res = await fetch(`${API_BASE}/auth/providers`, withCreds());
  if (!res.ok) throw new Error(`getProviders failed: ${res.status}`);
  return (await res.json()) as AuthProviders;
}

// current user, or null if not authenticated (401)
export async function getMe(): Promise<AuthUser | null> {
  const res = await fetch(`${API_BASE}/auth/me`, withCreds());
  if (res.status === 401) return null;
  if (!res.ok) throw new Error(`getMe failed: ${res.status}`);
  return (await res.json()) as AuthUser;
}

export async function login(email: string, password: string) {
  const res = await fetch(`${API_BASE}/auth/login`, withCreds({
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  }));
  if (!res.ok) throw new Error("invalid credentials");
  return await res.json();
}

export async function logout() {
  await fetch(`${API_BASE}/auth/logout`, withCreds({ method: "POST" }));
}

export async function phoneRequest(phone: string) {
  const res = await fetch(`${API_BASE}/auth/phone/request`, withCreds({
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ phone }),
  }));
  if (!res.ok) throw new Error(`phoneRequest failed: ${res.status}`);
  return await res.json();
}

export async function phoneVerify(phone: string, code: string) {
  const res = await fetch(`${API_BASE}/auth/phone/verify`, withCreds({
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ phone, code }),
  }));
  if (!res.ok) throw new Error("invalid or expired code");
  return await res.json();
}

// OAuth is a full-page redirect (the backend sets the cookie and bounces back)
export function oauthStartUrl(provider: string): string {
  return `${API_BASE}/auth/${provider}/start`;
}

// --- conversational scoping sessions ---

export async function createSession(goalSeed?: string, dryRun: boolean | null = null) {
  const res = await fetch(`${API_BASE}/sessions`, withCreds({
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ goal_seed: goalSeed || null, dry_run: dryRun }),
  }));
  if (!res.ok) throw new Error(`createSession failed: ${res.status}`);
  return (await res.json()) as { run_id: string; mode: string };
}

export async function sendMessage(runId: string, text: string) {
  const res = await fetch(`${API_BASE}/sessions/${runId}/messages`, withCreds({
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
  }));
  if (!res.ok) throw new Error(`sendMessage failed: ${res.status}`);
  return await res.json();
}

export async function interruptSession(runId: string) {
  await fetch(`${API_BASE}/sessions/${runId}/interrupt`, withCreds({ method: "POST" }));
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
  const res = await fetch(`${API_BASE}/runs/${runId}/datasets`, withCreds());
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
  const res = await fetch(`${API_BASE}/runs/${runId}/datasets/upload`, withCreds({
    method: "POST",
    body: form,
  }));
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
  const res = await fetch(`${API_BASE}/runs/${runId}/datasets`, withCreds({
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }));
  if (!res.ok) {
    const b = await res.json().catch(() => ({}));
    throw new Error(`registerDataset failed: ${res.status} ${JSON.stringify(b.detail ?? "")}`);
  }
  return await res.json();
}

export async function satisfyDataset(runId: string, assetId: string) {
  const res = await fetch(`${API_BASE}/runs/${runId}/datasets/${assetId}/ready`, withCreds({
    method: "POST",
  }));
  if (!res.ok) throw new Error(`satisfyDataset failed: ${res.status}`);
  return await res.json();
}

// --- launch / resume the autonomous loop ---

export async function launchRun(runId: string, dryRun: boolean | null = null) {
  const res = await fetch(`${API_BASE}/runs/${runId}/launch`, withCreds({
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ dry_run: dryRun }),
  }));
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
  const res = await fetch(`${API_BASE}/runs/${runId}/resume`, withCreds({
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ dry_run: dryRun }),
  }));
  if (!res.ok) throw new Error(`resumeRun failed: ${res.status}`);
  return await res.json();
}

export function subscribeEvents(
  onEvent: (e: LabEvent) => void,
  runId?: string,
): EventSource {
  const url = new URL(`${API_BASE}/events`);
  if (runId) url.searchParams.set("run_id", runId);
  // withCredentials so the session cookie is sent with the SSE stream too
  const es = new EventSource(url.toString(), { withCredentials: true });
  es.onmessage = (msg) => {
    try {
      onEvent(JSON.parse(msg.data) as LabEvent);
    } catch {
      /* ignore malformed frames */
    }
  };
  return es;
}
