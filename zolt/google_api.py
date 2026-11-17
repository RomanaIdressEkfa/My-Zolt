"""Gmail and Google Calendar through Google's official API (free).

One-time setup by the owner: create an OAuth "Desktop app" client in Google Cloud
Console, download its JSON as data/google_credentials.json. The first call opens a
Google consent page on the PC; the token is then kept in data/google_token.json.
"""
from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from typing import Any

from .memory import DATA_DIR

CREDENTIALS = DATA_DIR / "google_credentials.json"
TOKEN = DATA_DIR / "google_token.json"
SCOPES = ["https://www.googleapis.com/auth/gmail.modify", "https://www.googleapis.com/auth/calendar"]
SETUP_HELP = ("Gmail/Calendar is not connected yet. One-time setup for her: 1) console.cloud.google.com -> "
              "create a project -> enable Gmail API and Google Calendar API; 2) OAuth consent screen -> External, "
              "add her Gmail as a test user; 3) Credentials -> Create OAuth client ID -> Desktop app -> download JSON; "
              f"4) save it as {CREDENTIALS}. Then ask again and approve the Google page that opens on the PC.")

_services: dict[str, Any] = {}


def ready() -> bool:
    return TOKEN.exists()


def _creds():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES) if TOKEN.exists() else None
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    if not creds or not creds.valid:
        if not CREDENTIALS.exists():
            raise FileNotFoundError(SETUP_HELP)
        flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS), SCOPES)
        creds = flow.run_local_server(port=0, timeout_seconds=300)  # opens Google consent page on the PC
    TOKEN.write_text(creds.to_json(), encoding="utf-8")
    return creds


def _service(name: str):
    from googleapiclient.discovery import build
    if name not in _services:
        version = "v1" if name == "gmail" else "v3"
        _services[name] = build(name, version, credentials=_creds(), cache_discovery=False)
    return _services[name]


async def _call(fn):
    try:
        return await asyncio.to_thread(fn)
    except FileNotFoundError as e:
        return str(e)


def _headers(msg) -> dict[str, str]:
    return {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}


def _body(payload) -> str:
    if payload.get("mimeType", "").startswith("text/plain") and payload.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", "replace")
    for part in payload.get("parts", []) or []:
        text = _body(part)
        if text:
            return text
    return ""


async def gmail_search(query: str, max_results: int = 10) -> str:
    def go():
        svc = _service("gmail")
        res = svc.users().messages().list(userId="me", q=query, maxResults=max_results).execute()
        rows = []
        for m in res.get("messages", []):
            full = svc.users().messages().get(userId="me", id=m["id"], format="metadata",
                                              metadataHeaders=["From", "Subject", "Date"]).execute()
            h = _headers(full)
            rows.append(f"id={m['id']} | {h.get('date', '')[:25]} | {h.get('from', '')[:50]} | {h.get('subject', '')[:90]}")
        return "\n".join(rows) or "No emails found."
    return await _call(go)


async def gmail_read(message_id: str) -> str:
    def go():
        full = _service("gmail").users().messages().get(userId="me", id=message_id, format="full").execute()
        h = _headers(full)
        return (f"From: {h.get('from')}\nTo: {h.get('to')}\nDate: {h.get('date')}\nSubject: {h.get('subject')}\n\n"
                f"{_body(full['payload'])[:8000]}")
    return await _call(go)


def _mime(to: str, subject: str, body: str) -> dict[str, str]:
    msg = MIMEText(body, "plain", "utf-8")
    msg["to"], msg["subject"] = to, subject
    return {"raw": base64.urlsafe_b64encode(msg.as_bytes()).decode()}


async def gmail_draft(to: str, subject: str, body: str) -> str:
    def go():
        d = _service("gmail").users().drafts().create(userId="me", body={"message": _mime(to, subject, body)}).execute()
        return f"Draft saved in Gmail (id {d['id']})."
    return await _call(go)


async def gmail_send(to: str, subject: str, body: str) -> str:
    def go():
        m = _service("gmail").users().messages().send(userId="me", body=_mime(to, subject, body)).execute()
        return f"Sent (id {m['id']})."
    return await _call(go)


async def unread_since(seen: set[str]) -> list[dict[str, str]]:
    def go():
        svc = _service("gmail")
        res = svc.users().messages().list(userId="me", q="is:unread in:inbox newer_than:1d", maxResults=10).execute()
        out = []
        for m in res.get("messages", []):
            if m["id"] in seen:
                continue
            full = svc.users().messages().get(userId="me", id=m["id"], format="metadata",
                                              metadataHeaders=["From", "Subject"]).execute()
            h = _headers(full)
            out.append({"id": m["id"], "from": h.get("from", ""), "subject": h.get("subject", "")})
        return out
    return await asyncio.to_thread(go)


async def calendar_events(days: int = 7) -> str:
    def go():
        now = datetime.utcnow()
        res = _service("calendar").events().list(
            calendarId="primary", timeMin=now.isoformat() + "Z", timeMax=(now + timedelta(days=days)).isoformat() + "Z",
            singleEvents=True, orderBy="startTime", maxResults=30).execute()
        rows = [f"{e['start'].get('dateTime', e['start'].get('date'))} - {e.get('summary', '(no title)')}"
                for e in res.get("items", [])]
        return "\n".join(rows) or f"No events in the next {days} days."
    return await _call(go)


async def calendar_add(title: str, start: str, end: str) -> str:
    def go():
        tz = datetime.now().astimezone().tzinfo
        s = datetime.fromisoformat(start).replace(tzinfo=tz)
        e = datetime.fromisoformat(end).replace(tzinfo=tz)
        ev = _service("calendar").events().insert(calendarId="primary", body={
            "summary": title, "start": {"dateTime": s.isoformat()}, "end": {"dateTime": e.isoformat()}}).execute()
        return f"Event created: {ev.get('htmlLink')}"
    return await _call(go)
