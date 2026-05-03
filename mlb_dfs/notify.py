"""WhatsApp notifications via Twilio. Free Sandbox covers any draft volume.

Setup (one time):
  1. Sign up at twilio.com (free trial credit ~$15).
  2. Console -> Messaging -> Try it out -> Send a WhatsApp message. The Sandbox
     gives you a number (e.g. +14155238886) and a join code (e.g. 'join abc-def').
  3. Each member texts that join code from their phone to the Twilio number once.
  4. Set Fly secrets:
        fly secrets set TWILIO_ACCOUNT_SID=AC... \\
                        TWILIO_AUTH_TOKEN=...    \\
                        TWILIO_FROM=whatsapp:+14155238886 \\
                        TWILIO_TO='whatsapp:+15551111111,whatsapp:+15552222222'

For production-grade (custom number, no Sandbox), apply for a Twilio
WhatsApp Sender via Meta Business Manager. Same env vars.
"""
from __future__ import annotations

import os

import requests


def is_configured() -> bool:
    return all(os.environ.get(k) for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM", "TWILIO_TO"))


def _recipients() -> list[str]:
    raw = os.environ.get("TWILIO_TO", "")
    return [t.strip() for t in raw.split(",") if t.strip()]


def notify(message: str) -> dict:
    if not is_configured():
        return {"sent": [], "failed": [], "configured": False}
    sid = os.environ["TWILIO_ACCOUNT_SID"]
    token = os.environ["TWILIO_AUTH_TOKEN"]
    from_ = os.environ["TWILIO_FROM"]
    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
    sent, failed = [], []
    for to in _recipients():
        try:
            r = requests.post(
                url,
                auth=(sid, token),
                data={"From": from_, "To": to, "Body": message},
                timeout=8,
            )
            if r.status_code in (200, 201):
                sent.append(to)
            else:
                failed.append({"to": to, "status": r.status_code, "body": r.text[:200]})
        except Exception as e:
            failed.append({"to": to, "error": str(e)})
    return {"sent": sent, "failed": failed, "configured": True}
