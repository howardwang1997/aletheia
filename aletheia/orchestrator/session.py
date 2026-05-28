"""Multi-turn scoping conversation.

A ``ConversationSession`` keeps a long-lived ``ClaudeSDKClient`` alive across many
user turns so Aletheia and the user can jointly clarify the research goal. The
agent asks focused questions, proposes directions, and when the user confirms it
calls ``finalize_goal`` to record a structured experiment plan (the Phase-1
hand-off). Tools are gated to {memory_log, finalize_goal} during this phase.

Every turn streams to the event bus: ``user_message`` / ``assistant_text`` (chat)
and ``thinking`` / ``tool_use`` / ``tool_result`` / ``result`` / ``status``
(activity). A ``dry_run`` mode scripts the exchange so the UI is testable with no
quota.
"""

from __future__ import annotations

import asyncio
from typing import Any

from aletheia.config import get_settings
from aletheia.events.bus import get_bus, make_event
from aletheia.events.normalizer import normalize_message
from aletheia.memory.service import finalize_plan
from aletheia.orchestrator.auth import configure_auth, has_credentials
from aletheia.orchestrator.gate import build_tool_gate
from aletheia.orchestrator.tools import build_finalize_goal_tool, build_memory_tool

SCOPING_PROMPT = (
    "You are Aletheia, an autonomous AI scientist, in the SCOPING phase with your "
    "principal investigator (the user). Your job here is to collaboratively turn a "
    "rough research interest into a concrete, well-scoped experiment plan. Do NOT run "
    "experiments now.\n\n"
    "Guidelines:\n"
    "- Have a natural back-and-forth. Ask ONE focused clarifying question at a time "
    "(objective; target property/metric; domain & specific system; available or "
    "purchasable data; constraints; compute budget; what success looks like).\n"
    "- Be concise and concrete. Propose 1-2 candidate directions when useful, with trade-offs.\n"
    "- Use the `memory_log` tool to jot key decisions as they are made.\n"
    "- When — and ONLY when — the user has confirmed the objective and the essentials "
    "are clear, call `finalize_goal` exactly once with a complete, concrete experiment "
    "plan (objective, domain, direction, hypothesis, dataset, method, baselines, metrics, "
    "success_criteria, risks, est_compute). Then briefly summarize and stop.\n"
    "- You may ONLY converse and use the `memory_log` and `finalize_goal` tools. You "
    "cannot run code, browse, or read files in this phase."
)

ALLOWLIST = {"mcp__aletheia__memory_log", "mcp__aletheia__finalize_goal"}


class ConversationSession:
    def __init__(self, run_id: str, dry_run: bool) -> None:
        self.run_id = run_id
        self.dry_run = dry_run
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self._client: Any = None  # ClaudeSDKClient (real mode)
        self._turn = 0
        self._first_message: str | None = None

    # --- lifecycle ---
    def start(self) -> None:
        if self._task is None:
            loop = self._run_dryrun_loop if self.dry_run else self._run_real_loop
            self._task = asyncio.create_task(loop())

    async def send(self, text: str) -> None:
        await self._bus_user_message(text)
        if self._first_message is None:
            self._first_message = text
        await self._queue.put(text)

    async def interrupt(self) -> None:
        if self._client is not None:
            try:
                await self._client.interrupt()
            except Exception:
                pass
        await self._status("awaiting_user", "interrupted")

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    # --- event helpers ---
    async def _status(self, state: str, detail: str | None = None) -> None:
        await get_bus().publish(
            make_event("status", run_id=self.run_id, payload={"state": state, "detail": detail})
        )

    async def _bus_user_message(self, text: str) -> None:
        await get_bus().publish(
            make_event("user_message", run_id=self.run_id, agent="user", payload={"text": text})
        )

    async def _emit(self, evt: dict) -> None:
        await get_bus().publish(evt)

    # --- real loop ---
    async def _run_real_loop(self) -> None:
        from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, create_sdk_mcp_server

        settings = get_settings()
        configure_auth(settings)

        server = create_sdk_mcp_server(
            name="aletheia",
            version="0.0.1",
            tools=[build_memory_tool(self.run_id), build_finalize_goal_tool(self.run_id)],
        )
        options = ClaudeAgentOptions(
            model=settings.claude_model,
            system_prompt=SCOPING_PROMPT,
            mcp_servers={"aletheia": server},
            allowed_tools=list(ALLOWLIST),
            permission_mode="default",  # so can_use_tool is consulted (gated, no prompt)
            can_use_tool=build_tool_gate(ALLOWLIST, self.run_id),
            setting_sources=[],
            max_budget_usd=settings.budget_usd,
        )

        try:
            async with ClaudeSDKClient(options=options) as client:
                self._client = client
                await self._status("awaiting_user", "ready")
                while True:
                    text = await self._queue.get()
                    await self._status("thinking")
                    try:
                        await client.query(text)
                        async for msg in client.receive_response():
                            for evt in normalize_message(msg, self.run_id):
                                await self._emit(evt)
                                if evt["type"] == "tool_use":
                                    await self._status(
                                        "using_tool", (evt.get("payload") or {}).get("tool")
                                    )
                    except Exception as exc:  # keep the session alive across turn errors
                        await self._emit(
                            make_event("error", run_id=self.run_id, payload={"error": str(exc)})
                        )
                    await self._status("awaiting_user")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            await self._emit(make_event("error", run_id=self.run_id, payload={"error": str(exc)}))

    # --- dry-run loop (scripted) ---
    async def _run_dryrun_loop(self) -> None:
        await self._status("awaiting_user", "ready")
        while True:
            text = await self._queue.get()
            self._turn += 1
            await self._status("thinking")
            await asyncio.sleep(0.3)
            if self._turn == 1:
                await self._emit(
                    make_event(
                        "assistant_text",
                        run_id=self.run_id,
                        payload={
                            "text": (
                                "[dry-run] Good start. To scope this well: what's the "
                                "target property/metric, and do you already have a dataset "
                                "in mind (or should I find one)?"
                            )
                        },
                    )
                )
                await self._status("awaiting_user")
            else:
                await self._emit(
                    make_event(
                        "assistant_text",
                        run_id=self.run_id,
                        payload={"text": "[dry-run] Great — here's the experiment plan I'll record."},
                    )
                )
                await self._emit(
                    make_event(
                        "tool_use",
                        run_id=self.run_id,
                        payload={"tool": "mcp__aletheia__finalize_goal", "input": {}},
                    )
                )
                await self._status("using_tool", "mcp__aletheia__finalize_goal")
                plan = self._canned_plan()
                experiment_id = await asyncio.to_thread(finalize_plan, self.run_id, plan)
                await self._emit(
                    make_event(
                        "goal_finalized",
                        run_id=self.run_id,
                        payload={"plan": plan, "experiment_id": experiment_id},
                    )
                )
                await self._emit(
                    make_event("result", run_id=self.run_id, payload={"result": "scoping complete", "cost_usd": 0.0})
                )
                await self._status("finalized")

    def _canned_plan(self) -> dict[str, str]:
        seed = (self._first_message or "a research goal").strip()
        return {
            "objective": f"[dry-run] {seed}",
            "domain": "materials",
            "direction": "composition-based property prediction",
            "hypothesis": "Magpie composition features + a gradient-boosted model predict the target property.",
            "dataset": "Matbench (e.g. matbench_expt_gap), official train/test folds",
            "method": "matminer Magpie features -> sklearn GBM",
            "baselines": "mean predictor; RandomForest",
            "metrics": "MAE, R^2 on the held-out fold",
            "success_criteria": "beat the mean baseline; MAE competitive with the Matbench leaderboard",
            "risks": "data leakage across folds; composition-only ceiling",
            "est_compute": "CPU-only, minutes",
        }


class SessionManager:
    def __init__(self) -> None:
        self._sessions: dict[str, ConversationSession] = {}

    def create(self, run_id: str, dry_run: bool) -> ConversationSession:
        session = ConversationSession(run_id, dry_run)
        self._sessions[run_id] = session
        session.start()
        return session

    def get(self, run_id: str) -> ConversationSession | None:
        return self._sessions.get(run_id)

    async def send(self, run_id: str, text: str) -> bool:
        session = self._sessions.get(run_id)
        if session is None:
            return False
        await session.send(text)
        return True

    async def interrupt(self, run_id: str) -> bool:
        session = self._sessions.get(run_id)
        if session is None:
            return False
        await session.interrupt()
        return True

    async def close_all(self) -> None:
        for session in list(self._sessions.values()):
            await session.close()
        self._sessions.clear()


_MANAGER: SessionManager | None = None


def get_session_manager() -> SessionManager:
    global _MANAGER
    if _MANAGER is None:
        _MANAGER = SessionManager()
    return _MANAGER


def auto_dry_run() -> bool:
    """Default dry-run decision when the client doesn't specify."""
    return not has_credentials(get_settings())
