"""Feishu (飞书) notifications for auto-pause / escalations / errors.

Phase-1 stub: POSTs a text card to the configured custom-bot webhook if set;
otherwise it just emits a ``notify`` event (so the dashboard still shows it) and
returns. Never raises — notification failure must not crash the lab.
"""

from __future__ import annotations

from aletheia.config import get_settings
from aletheia.events.bus import get_bus, make_event


async def notify_feishu(text: str, run_id: str | None = None) -> None:
    """Best-effort push to Feishu + a dashboard ``notify`` event."""
    await get_bus().publish(make_event("notify", run_id=run_id, payload={"text": text}))
    url = get_settings().feishu_webhook_url
    if not url:
        return
    try:
        import httpx

        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(url, json={"msg_type": "text", "content": {"text": text}})
    except Exception:  # notification is best-effort
        pass
