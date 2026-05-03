"""WhatsApp notifications via CallMeBot — free, per-recipient API keys.

Each recipient sends `I allow callmebot to send me messages` to +34 644 51 95 23
on WhatsApp; CallMeBot replies with a personal API key. We store {phone:apikey}
pairs in the env var CALLMEBOT_RECIPIENTS as a JSON object:

    fly secrets set CALLMEBOT_RECIPIENTS='{"+15551234567":"abc12345","+15557654321":"def67890"}'

Then notify(message) blasts the message to each recipient.
"""
from __future__ import annotations

import json
import os
from urllib.parse import quote

import requests


def recipients() -> dict[str, str]:
    raw = os.environ.get("CALLMEBOT_RECIPIENTS", "").strip()
    if not raw:
        return {}
    try:
        return dict(json.loads(raw))
    except Exception:
        return {}


def is_configured() -> bool:
    return bool(recipients())


def notify(message: str) -> dict:
    """POST to CallMeBot for each recipient. Returns {sent, failed}."""
    sent = []
    failed = []
    for phone, key in recipients().items():
        url = (
            "https://api.callmebot.com/whatsapp.php"
            f"?phone={quote(phone)}&text={quote(message)}&apikey={quote(key)}"
        )
        try:
            r = requests.get(url, timeout=8)
            if r.status_code == 200 and "Message queued" in r.text:
                sent.append(phone)
            else:
                failed.append({"phone": phone, "status": r.status_code, "body": r.text[:200]})
        except Exception as e:
            failed.append({"phone": phone, "error": str(e)})
    return {"sent": sent, "failed": failed}
