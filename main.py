import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

VERIFY_TOKEN = "25052026"
PAGE_ACCESS_TOKEN = os.getenv("PAGE_ACCESS_TOKEN", "").strip()
GRAPH_API_VERSION = os.getenv("GRAPH_API_VERSION", "v21.0")
SEND_API_URL = f"https://graph.facebook.com/{GRAPH_API_VERSION}/me/messages"
PROFILE_API_URL = f"https://graph.facebook.com/{GRAPH_API_VERSION}"
SHEET_WEBHOOK_URL = os.getenv("SHEET_WEBHOOK_URL", "").strip()

# Vietnam mobile/landline numbers in local 0-prefix form are 10 digits.
# Accept +84, 84, or 0 followed by 9 more digits. Guard with non-digit
# boundaries so we don't match inside a longer run of digits.
VN_PHONE_REGEX = re.compile(r"(?<!\d)(?:\+84|84|0)(\d{9})(?!\d)")

app = FastAPI(title="FB Messenger Webhook")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("fb-webhook")


def extract_vn_phone_numbers(text: str) -> List[str]:
    if not text:
        return []
    seen: List[str] = []
    for m in VN_PHONE_REGEX.finditer(text):
        normalized = "0" + m.group(1)
        if normalized not in seen:
            seen.append(normalized)
    return seen


def walk_messages(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    for entry in payload.get("entry", []) or []:
        for event in entry.get("messaging", []) or []:
            message = event.get("message") or {}
            text = message.get("text") or ""
            if not text:
                continue
            phones = extract_vn_phone_numbers(text)
            if not phones:
                continue
            results.append({
                "sender_id": (event.get("sender") or {}).get("id"),
                "recipient_id": (event.get("recipient") or {}).get("id"),
                "message_id": message.get("mid"),
                "timestamp": event.get("timestamp"),
                "text": text,
                "phone_numbers": phones,
            })
    return results


async def fetch_user_name(client: httpx.AsyncClient, psid: str) -> Tuple[str, str]:
    """Return (full_name, raw_profile_json_string). Falls back to PSID on error."""
    if not PAGE_ACCESS_TOKEN or not psid:
        return psid or "", ""
    try:
        resp = await client.get(
            f"{PROFILE_API_URL}/{psid}",
            params={"fields": "first_name,last_name", "access_token": PAGE_ACCESS_TOKEN},
        )
        if resp.status_code >= 400:
            logger.warning("Profile API %s: %s", resp.status_code, resp.text)
            return psid, resp.text
        data = resp.json()
        name = " ".join(p for p in [data.get("first_name"), data.get("last_name")] if p).strip()
        return name or psid, resp.text
    except Exception as e:
        logger.exception("Profile fetch failed for %s: %s", psid, e)
        return psid, ""


async def send_text_message(client: httpx.AsyncClient, recipient_id: str, text: str) -> Dict[str, Any]:
    if not PAGE_ACCESS_TOKEN:
        logger.warning("PAGE_ACCESS_TOKEN not set; skipping reply to %s", recipient_id)
        return {"skipped": "no_token"}
    body = {
        "recipient": {"id": recipient_id},
        "messaging_type": "RESPONSE",
        "message": {"text": text},
    }
    resp = await client.post(
        SEND_API_URL,
        params={"access_token": PAGE_ACCESS_TOKEN},
        json=body,
    )
    if resp.status_code >= 400:
        logger.error("Send API error %s: %s", resp.status_code, resp.text)
    else:
        logger.info("Replied to %s: %s", recipient_id, text)
    return {"status_code": resp.status_code, "body": resp.text}


async def append_to_sheet(
    client: httpx.AsyncClient,
    rows: List[Dict[str, Any]],
) -> Dict[str, Any]:
    if not SHEET_WEBHOOK_URL:
        logger.warning("SHEET_WEBHOOK_URL not set; skipping sheet write")
        return {"skipped": "no_url"}
    try:
        resp = await client.post(SHEET_WEBHOOK_URL, json={"rows": rows})
        if resp.status_code >= 400:
            logger.error("Sheet webhook error %s: %s", resp.status_code, resp.text)
        else:
            logger.info("Wrote %d row(s) to sheet", len(rows))
        return {"status_code": resp.status_code, "body": resp.text}
    except Exception as e:
        logger.exception("Sheet write failed: %s", e)
        return {"error": str(e)}


def format_reply(phones: List[str]) -> str:
    if len(phones) == 1:
        return f"Đã nhận số điện thoại: {phones[0]}"
    return "Đã nhận các số điện thoại:\n" + "\n".join(f"- {p}" for p in phones)


@app.get("/api/v1/receive-message/messenger/{verify_token}")
async def verify_webhook(
    verify_token: str,
    hub_mode: Optional[str] = Query(None, alias="hub.mode"),
    hub_challenge: Optional[str] = Query(None, alias="hub.challenge"),
    hub_verify_token: Optional[str] = Query(None, alias="hub.verify_token"),
):
    if verify_token != VERIFY_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid path token")
    if hub_mode == "subscribe" and hub_verify_token == VERIFY_TOKEN and hub_challenge:
        logger.info("Webhook verified for token %s", verify_token)
        return PlainTextResponse(content=hub_challenge)
    raise HTTPException(status_code=403, detail="Verification failed")


@app.post("/api/v1/receive-message/messenger/{verify_token}")
async def receive_message(verify_token: str, request: Request):
    if verify_token != VERIFY_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid path token")

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    logger.info("Webhook payload: %s", payload)

    extracted = walk_messages(payload)
    if not extracted:
        return {"status": "ok", "extracted": []}

    async with httpx.AsyncClient(timeout=10) as client:
        sheet_rows: List[Dict[str, Any]] = []
        for item in extracted:
            sender = item["sender_id"]
            phones = item["phone_numbers"]
            logger.info(
                "Extracted phones from sender=%s: %s | text=%r",
                sender, phones, item["text"],
            )

            name = ""
            if sender:
                name, _ = await fetch_user_name(client, sender)
            item["sender_name"] = name

            for phone in phones:
                sheet_rows.append({
                    "timestamp": item.get("timestamp"),
                    "sender_id": sender,
                    "name": name,
                    "phone": phone,
                    "message": item.get("text"),
                    "message_id": item.get("message_id"),
                })

            if sender:
                reply = format_reply(phones)
                item["reply"] = {
                    "text": reply,
                    "send_result": await send_text_message(client, sender, reply),
                }

        if sheet_rows:
            sheet_result = await append_to_sheet(client, sheet_rows)
        else:
            sheet_result = {"skipped": "no_rows"}

    return {"status": "ok", "extracted": extracted, "sheet": sheet_result}


@app.get("/health")
async def health():
    return {"status": "ok"}
