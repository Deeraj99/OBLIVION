"""Optional WhatsApp Cloud API integration.

The teacher app stays useful even without WhatsApp configured — in that case
every call is treated as a dry-run and never touches the network.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import requests

log = logging.getLogger("facultyhub.whatsapp")


class WhatsAppService:
    def __init__(self) -> None:
        self.enabled = os.getenv("WHATSAPP_ENABLED", "false").lower() == "true"
        self.version = os.getenv("WHATSAPP_API_VERSION", "v23.0")
        self.phone_id = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "").strip()
        self.token = os.getenv("WHATSAPP_ACCESS_TOKEN", "").strip()

    @property
    def configured(self) -> bool:
        return self.enabled and bool(self.phone_id and self.token)

    def send_text(self, to: str, message: str) -> dict[str, Any]:
        if not self.configured:
            return {"sent": False, "dry_run": True, "reason": "WhatsApp is not configured."}
        if not to:
            return {"sent": False, "dry_run": False, "reason": "Recipient phone number is empty."}
        url = f"https://graph.facebook.com/{self.version}/{self.phone_id}/messages"
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"preview_url": False, "body": message},
        }
        try:
            r = requests.post(
                url,
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=20,
            )
        except requests.RequestException as exc:
            log.warning("WhatsApp send failed for %s: %s", to, exc)
            return {"sent": False, "dry_run": False, "reason": f"network error: {exc}"}
        if not r.ok:
            log.warning("WhatsApp send HTTP %s for %s: %s", r.status_code, to, r.text[:200])
            return {"sent": False, "dry_run": False, "reason": r.text[:300]}
        return {"sent": True, "dry_run": False, "data": r.json()}