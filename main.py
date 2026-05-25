import hmac
import logging
import os
import re
from datetime import datetime
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

try:
    from google.oauth2 import service_account
    from google.auth.transport.requests import Request as GoogleAuthRequest
except ImportError:
    service_account = None
    GoogleAuthRequest = None

VERIFY_TOKEN = "25052026"
PAGE_ACCESS_TOKEN = os.getenv("PAGE_ACCESS_TOKEN", "").strip()
PAGE_ID = os.getenv("PAGE_ID", "").strip()
ADMIN_PASSWORD = os.getenv("PASSWORD", "").strip()
GRAPH_API_VERSION = os.getenv("GRAPH_API_VERSION", "v21.0")
GRAPH_BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"
SEND_API_URL = f"{GRAPH_BASE}/me/messages"
PROFILE_API_URL = GRAPH_BASE

# Google Sheets via service account.
SHEETS_SPREADSHEET_ID = os.getenv("SHEETS_SPREADSHEET_ID", "").strip()
SHEETS_RANGE = os.getenv("SHEETS_RANGE", "Messages!A:G").strip()
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
SHEETS_HEADER = ["received_at", "fb_timestamp", "sender_id", "name", "phone", "message", "message_id"]

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


_sheets_creds = None


def get_sheets_access_token() -> Optional[str]:
    global _sheets_creds
    if not (SHEETS_SPREADSHEET_ID and GOOGLE_SERVICE_ACCOUNT_JSON and service_account):
        return None
    if _sheets_creds is None:
        if not os.path.isfile(GOOGLE_SERVICE_ACCOUNT_JSON):
            logger.error("Service account JSON not found at %s", GOOGLE_SERVICE_ACCOUNT_JSON)
            return None
        _sheets_creds = service_account.Credentials.from_service_account_file(
            GOOGLE_SERVICE_ACCOUNT_JSON, scopes=SHEETS_SCOPES
        )
    if not _sheets_creds.valid:
        _sheets_creds.refresh(GoogleAuthRequest())
    return _sheets_creds.token


async def append_to_sheet(
    client: httpx.AsyncClient,
    rows: List[Dict[str, Any]],
) -> Dict[str, Any]:
    if not SHEETS_SPREADSHEET_ID:
        logger.warning("SHEETS_SPREADSHEET_ID not set; skipping sheet write")
        return {"skipped": "no_spreadsheet_id"}
    if not GOOGLE_SERVICE_ACCOUNT_JSON:
        logger.warning("GOOGLE_SERVICE_ACCOUNT_JSON not set; skipping sheet write")
        return {"skipped": "no_credentials"}
    if service_account is None:
        logger.error("google-auth not installed; cannot write to sheet")
        return {"skipped": "google_auth_missing"}

    try:
        token = get_sheets_access_token()
        if not token:
            return {"skipped": "no_token"}

        now = datetime.utcnow().isoformat() + "Z"
        values = [
            [
                now,
                r.get("timestamp", ""),
                r.get("sender_id", ""),
                r.get("name", ""),
                r.get("phone", ""),
                r.get("message", ""),
                r.get("message_id", ""),
            ]
            for r in rows
        ]
        url = (
            f"https://sheets.googleapis.com/v4/spreadsheets/{SHEETS_SPREADSHEET_ID}"
            f"/values/{SHEETS_RANGE}:append"
        )
        resp = await client.post(
            url,
            params={
                "valueInputOption": "USER_ENTERED",
                "insertDataOption": "INSERT_ROWS",
            },
            headers={"Authorization": f"Bearer {token}"},
            json={"values": values},
        )
        if resp.status_code >= 400:
            logger.error("Sheets API error %s: %s", resp.status_code, resp.text)
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


bearer_scheme = HTTPBearer(description="Admin password (from PASSWORD env)")


def require_admin(
    creds: HTTPAuthorizationCredentials = Depends(bearer_scheme),
) -> None:
    if not ADMIN_PASSWORD:
        raise HTTPException(status_code=503, detail="Admin password not configured")
    if not hmac.compare_digest(creds.credentials, ADMIN_PASSWORD):
        raise HTTPException(status_code=403, detail="Invalid password")


async def graph_paginate(
    client: httpx.AsyncClient, url: str, params: Dict[str, Any]
) -> AsyncIterator[Dict[str, Any]]:
    """Yield each page's JSON, following paging.next until exhausted."""
    next_url: Optional[str] = url
    next_params: Optional[Dict[str, Any]] = params
    while next_url:
        resp = await client.get(next_url, params=next_params)
        if resp.status_code >= 400:
            raise HTTPException(
                status_code=502,
                detail=f"Graph API {resp.status_code}: {resp.text[:500]}",
            )
        data = resp.json()
        yield data
        # paging.next is a full URL with cursor + access_token baked in.
        next_url = (data.get("paging") or {}).get("next")
        next_params = None


class BackfillRequest(BaseModel):
    page_id: Optional[str] = None
    max_conversations: Optional[int] = None
    max_messages_per_conversation: Optional[int] = None
    dry_run: bool = False


@app.post("/admin/backfill", dependencies=[Depends(require_admin)])
async def admin_backfill(req: BackfillRequest):
    if not PAGE_ACCESS_TOKEN:
        raise HTTPException(status_code=503, detail="PAGE_ACCESS_TOKEN not set")

    page_id = (req.page_id or PAGE_ID or "").strip()
    if not page_id:
        raise HTTPException(
            status_code=400,
            detail="page_id required (in request body or PAGE_ID env)",
        )

    conv_count = 0
    msg_count = 0
    rows: List[Dict[str, Any]] = []

    async with httpx.AsyncClient(timeout=30) as client:
        conv_url = f"{GRAPH_BASE}/{page_id}/conversations"
        conv_params = {
            "fields": "id,participants,updated_time",
            "access_token": PAGE_ACCESS_TOKEN,
            "limit": 25,
        }

        stop = False
        async for conv_page in graph_paginate(client, conv_url, conv_params):
            for conv in conv_page.get("data", []) or []:
                conv_count += 1
                if req.max_conversations and conv_count > req.max_conversations:
                    stop = True
                    break

                conv_id = conv.get("id")
                if not conv_id:
                    continue

                # Identify the non-Page participant (the user) for the name fallback.
                participant_name = ""
                participant_id = ""
                for p in ((conv.get("participants") or {}).get("data") or []):
                    if str(p.get("id")) != str(page_id):
                        participant_id = p.get("id", "")
                        participant_name = p.get("name", "")
                        break

                msg_url = f"{GRAPH_BASE}/{conv_id}/messages"
                msg_params = {
                    "fields": "id,created_time,from,message",
                    "access_token": PAGE_ACCESS_TOKEN,
                    "limit": 100,
                }
                msgs_in_conv = 0
                conv_done = False
                async for msg_page in graph_paginate(client, msg_url, msg_params):
                    for msg in msg_page.get("data", []) or []:
                        msg_count += 1
                        msgs_in_conv += 1
                        if (
                            req.max_messages_per_conversation
                            and msgs_in_conv > req.max_messages_per_conversation
                        ):
                            conv_done = True
                            break

                        from_obj = msg.get("from") or {}
                        sender_id = str(from_obj.get("id") or "")
                        if sender_id == str(page_id):
                            # Skip Page-side messages (our own bot echoes).
                            continue

                        text = msg.get("message") or ""
                        phones = extract_vn_phone_numbers(text)
                        if not phones:
                            continue

                        sender_name = from_obj.get("name") or participant_name
                        for phone in phones:
                            rows.append({
                                "timestamp": msg.get("created_time"),
                                "sender_id": sender_id or participant_id,
                                "name": sender_name,
                                "phone": phone,
                                "message": text,
                                "message_id": msg.get("id"),
                            })
                    if conv_done:
                        break
            if stop:
                break

        result: Dict[str, Any] = {
            "conversations_scanned": conv_count,
            "messages_scanned": msg_count,
            "rows_collected": len(rows),
            "dry_run": req.dry_run,
        }

        if not req.dry_run and rows:
            BATCH = 500
            sheet_writes = []
            for i in range(0, len(rows), BATCH):
                wr = await append_to_sheet(client, rows[i : i + BATCH])
                sheet_writes.append(wr)
            result["sheet_writes"] = sheet_writes

        return result


@app.get("/health")
async def health():
    return {"status": "ok"}
