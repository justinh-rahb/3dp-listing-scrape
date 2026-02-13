"""Webhook notification helpers with provider-specific payload formatting."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import requests

DEFAULT_EVENTS = {"scrape_completed", "new_deal_detected", "scrape_failed"}


def _event_enabled(event_type: str, settings: dict[str, Any]) -> bool:
    configured = settings.get("webhook_events", list(DEFAULT_EVENTS))
    if not isinstance(configured, list):
        return False
    return event_type in configured


def _canonical_event(event_type: str, data: dict[str, Any]) -> dict[str, Any]:
    return {
        "event": event_type,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "schema_version": 1,
        "data": data,
    }


def _format_discord(event: dict[str, Any]) -> dict[str, Any]:
    event_type = event["event"]
    data = event["data"]

    if event_type == "scrape_completed":
        content = (
            f"Scrape completed: found={data.get('found', 0)}, new={data.get('new', 0)}, "
            f"price_changes={data.get('price_changes', 0)}, errors={data.get('errors', 0)}"
        )
    elif event_type == "scrape_failed":
        content = f"Scrape failed: {data.get('error', 'Unknown error')}"
    elif event_type == "new_deal_detected":
        deals = data.get("deals", [])
        lines = [f"New deals detected ({len(deals)}):"]
        for deal in deals:
            lines.append(f"- {deal.get('title')} ({deal.get('currency')} ${deal.get('current_price')}) {deal.get('url')}")
        content = "\n".join(lines)
    else:
        content = f"Event: {event_type}"

    return {"content": content[:1900]}


def _format_google_chat(event: dict[str, Any]) -> dict[str, Any]:
    event_type = event["event"]
    data = event["data"]

    if event_type == "scrape_completed":
        text = (
            "Scrape completed\n"
            f"found={data.get('found', 0)} new={data.get('new', 0)} "
            f"price_changes={data.get('price_changes', 0)} errors={data.get('errors', 0)}"
        )
    elif event_type == "scrape_failed":
        text = f"Scrape failed: {data.get('error', 'Unknown error')}"
    elif event_type == "new_deal_detected":
        deals = data.get("deals", [])
        lines = [f"New deals detected ({len(deals)}):"]
        for deal in deals:
            lines.append(
                f"- {deal.get('title')} ({deal.get('currency')} ${deal.get('current_price')}) {deal.get('url')}"
            )
        text = "\n".join(lines)
    else:
        text = f"Event: {event_type}"

    return {"text": text}


def _format_payload(provider: str, event: dict[str, Any]) -> dict[str, Any]:
    if provider == "discord":
        return _format_discord(event)
    if provider == "google_chat":
        return _format_google_chat(event)
    return event


def send_webhook_event(event_type: str, data: dict[str, Any], settings: dict[str, Any]) -> bool:
    """Send an event to configured webhook endpoint. Returns True if sent."""
    enabled = bool(settings.get("webhook_enabled", False))
    webhook_url = (settings.get("webhook_url") or "").strip()
    provider = (settings.get("webhook_provider") or "generic").strip().lower()

    if not enabled or not webhook_url:
        return False
    if not _event_enabled(event_type, settings):
        return False

    event = _canonical_event(event_type, data)
    payload = _format_payload(provider, event)

    timeout_seconds = 5
    delays = [1, 3, 9]
    for idx in range(len(delays) + 1):
        try:
            resp = requests.post(webhook_url, json=payload, timeout=timeout_seconds)
            resp.raise_for_status()
            return True
        except requests.RequestException:
            if idx >= len(delays):
                return False
            delay = delays[idx]
            import time

            time.sleep(delay)
    return False
