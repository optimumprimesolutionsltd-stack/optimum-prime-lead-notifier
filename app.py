#!/usr/bin/env python3
"""
Optimum Prime Solutions — Lead Auto-Reply & Webinar Notification System
"""

import os
import re
import json
import html
import csv
import collections
import io
import uuid
import hashlib
import hmac
import urllib.parse
import requests
from datetime import datetime, timedelta, timezone
from flask import Flask, request, jsonify, Response
from flask_cors import CORS
from openai import OpenAI
from google.oauth2 import service_account
from google.auth.transport.requests import Request as GoogleAuthRequest

# ── Meta WhatsApp Cloud API config ───────────────────────────────────────────
# Set these in Render environment variables:
#   META_WA_TOKEN        — permanent system user access token from Meta Business Manager
#   META_WA_PHONE_ID     — Phone Number ID from WhatsApp Manager (not the phone number itself)
META_WA_TOKEN    = os.environ.get("META_WA_TOKEN", "").strip()
META_WA_PHONE_ID = os.environ.get("META_WA_PHONE_ID", "").strip()
META_WA_API_URL  = f"https://graph.facebook.com/v20.0/{META_WA_PHONE_ID}/messages"
# The WhatsApp Business Account the templates live in, confirmed against
# WhatsApp Manager: the sending number +254 727 209720 sits in this same
# WABA. Two other WABAs on this business are empty — a template created in
# one of those would look fine and never be found at send time.
META_WABA_ID     = os.environ.get("META_WABA_ID", "1374387564578294").strip()
# Gate for the template admin endpoint. Unset means the endpoint is off,
# which is how it should sit when nobody is actively using it.
TEMPLATE_ADMIN_KEY = os.environ.get("TEMPLATE_ADMIN_KEY", "").strip()

# ── Resend email config ───────────────────────────────────────────────────────
# Set in Render environment variables:
#   RESEND_API_KEY       — API key from resend.com/api-keys
#   RESEND_FROM          — verified sender, e.g. "Optimum Prime Solutions <zawadi@mail.optimumprimesolutions.co.ke>"
#                          (falls back to Resend's test sender if you haven't verified a domain yet)
#   RESEND_WEBHOOK_SECRET — signing secret shown when you create the inbound webhook in the
#                          Resend dashboard (Webhooks → Add Webhook → select "email.received").
#                          Used to verify inbound requests are really from Resend.
# .strip() guards against a trailing newline/whitespace sneaking in via copy-paste
# into Render's env var UI, which breaks the Authorization header otherwise.
RESEND_API_KEY        = os.environ.get("RESEND_API_KEY", "").strip()
RESEND_FROM           = os.environ.get("RESEND_FROM", "Optimum Prime Solutions <onboarding@resend.dev>").strip()
RESEND_WEBHOOK_SECRET = os.environ.get("RESEND_WEBHOOK_SECRET", "").strip()
RESEND_API_URL        = "https://api.resend.com/emails"

# Dark theme for subscriber-facing emails (welcome, blog notify, broadcast) —
# blue accent instead of the site's red, which reads as alarming in an inbox.
EMAIL_BG       = "#0f172a"  # slate-900
EMAIL_TEXT     = "#e2e8f0"  # slate-200
EMAIL_TEXT_DIM = "#94a3b8"  # slate-400
EMAIL_ACCENT   = "#3b82f6"  # blue-500
EMAIL_BORDER   = "rgba(255,255,255,0.1)"

# Team inbox that gets an email alert for every new lead/webinar registration,
# alongside the existing WhatsApp alerts to TEAM_NUMBERS.
ADMIN_NOTIFY_EMAIL    = os.environ.get("ADMIN_NOTIFY_EMAIL", "").strip()

# Signs one-click unsubscribe links so anyone can unsubscribe without logging
# in, but only for their own address (can't be used to unsubscribe someone
# else without also knowing their exact email — the token is a keyed hash of it).
UNSUB_SECRET          = os.environ.get("UNSUB_SECRET", "").strip()

FIREBASE_BASE            = "https://optimum-prime-website-default-rtdb.europe-west1.firebasedatabase.app"
FIREBASE_WEBINAR_URL     = f"{FIREBASE_BASE}/webinar_registrants.json"
FIREBASE_LEADS_URL       = f"{FIREBASE_BASE}/leads.json"
FIREBASE_NEWSLETTER_BASE = f"{FIREBASE_BASE}/newsletter_subscribers"
FIREBASE_NEWSLETTER_URL  = f"{FIREBASE_NEWSLETTER_BASE}.json"
FIREBASE_WA_CONVOS_BASE  = f"{FIREBASE_BASE}/whatsapp_conversations"
FIREBASE_BLOGS_BASE      = f"{FIREBASE_BASE}/siteData/blogs"
FIREBASE_BLOGS_URL       = f"{FIREBASE_BLOGS_BASE}.json"

# database.rules.json locked the RTDB down on 2026-08-06 (commit c630f3dd in the
# website repo, "Lock down the Realtime Database and move leads out of
# siteData") — reads/writes to /leads, /whatsapp_conversations, /crm etc. now
# require auth != null. Before that, this file's plain unauthenticated
# requests.post/get/patch calls worked because the database had no rules at
# all. Nobody updated this separate backend at the time, so every server-side
# Firebase write here (Zawadi bookings, Zawadi handoffs, the WhatsApp
# conversation log) has been silently rejected ever since — caught by a bare
# except and printed to the Render logs, never surfaced anywhere a human would
# see it. A service account restores write access the same way Cloud
# Functions / the Admin SDK would: it isn't bound by database.rules.json at
# all, so it works regardless of which uids that file happens to allow.
FIREBASE_SERVICE_ACCOUNT_JSON = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON", "").strip()
_firebase_credentials = None

def _firebase_auth_headers() -> dict:
    """
    Bearer token for a Firebase service account, to attach to every direct
    Firebase REST call in this file. Returns {} (no Authorization header) if
    FIREBASE_SERVICE_ACCOUNT_JSON isn't set, so a misconfigured env doesn't
    crash the request outright — it just gets denied by the rules, same as
    before this fix, with the error still visible in the response.
    """
    global _firebase_credentials
    if not FIREBASE_SERVICE_ACCOUNT_JSON:
        return {}
    try:
        if _firebase_credentials is None:
            info = json.loads(FIREBASE_SERVICE_ACCOUNT_JSON)
            _firebase_credentials = service_account.Credentials.from_service_account_info(
                info,
                scopes=[
                    "https://www.googleapis.com/auth/firebase.database",
                    "https://www.googleapis.com/auth/userinfo.email",
                ],
            )
        if not _firebase_credentials.valid:
            _firebase_credentials.refresh(GoogleAuthRequest())
        return {"Authorization": f"Bearer {_firebase_credentials.token}"}
    except Exception as e:
        print(f"[Firebase auth] Could not get service account token: {e}")
        return {}

# Office/admin numbers (E.164 format, no 'whatsapp:' prefix needed for Meta API).
# Every "new lead", "demo booked", "review submitted", etc. alert in this file
# goes to both of these. Messages are SENT FROM +254727209720 (the registered
# Meta API number) — a number can't receive Cloud API messages sent from
# itself (confirmed live: every send to it failed with Meta error #100,
# "Invalid parameter"), so it can never be one of the two recipients here.
TEAM_NUMBERS = [
    "+254758449475",
    "+254116246074",
]

SERVICE_URL = os.environ.get("SERVICE_URL", "https://optimum-prime-lead-notifier.onrender.com")

app = Flask(__name__)
CORS(app)


# Recent delivery verdicts from the status webhook, newest last. Meta accepts
# a send with an HTTP 200 and only reports the real outcome here, so without
# keeping these there is no way to tell a delivered message from one that was
# accepted and silently dropped. Bounded, in memory, and deliberately not
# persisted: this is for answering 'did that last send actually land', not a
# record of anything.
WA_DELIVERY_LOG = collections.deque(maxlen=100)

def _wa_send(to: str, body: str, name: str = "", force_log: bool = False) -> dict:
    """
    Send a WhatsApp text message via Meta Cloud API.
    `to` should be E.164 format, e.g. '+254712345678'.
    `force_log` logs this message even if `to` is a team number — use this for
    genuine bot/human replies within a real conversation (e.g. a team member
    testing Zawadi from their own phone), not for internal alert broadcasts.
    Returns a dict with keys: success (bool), message_id (str), error (str).
    """
    # Strip leading '+' — Meta API expects digits only (no + prefix)
    to_clean = to.lstrip("+")
    payload = {
        "messaging_product": "whatsapp",
        "to": to_clean,
        "type": "text",
        "text": {"body": body, "preview_url": False},
    }
    headers = {
        "Authorization": f"Bearer {META_WA_TOKEN}",
        "Content-Type": "application/json",
    }
    try:
        resp = requests.post(META_WA_API_URL, json=payload, headers=headers, timeout=10)
        data = resp.json()
        if resp.status_code == 200 and "messages" in data:
            msg_id = data["messages"][0].get("id", "")
            _log_wa_message(to_clean, "out", body, name=name, message_id=msg_id, force=force_log)
            return {"success": True, "message_id": msg_id, "error": ""}
        else:
            err = data.get("error", {}).get("message", str(data))
            print(f"[Meta WA] Send failed to {to}: {err}")
            return {"success": False, "message_id": "", "error": err}
    except Exception as e:
        print(f"[Meta WA] Exception sending to {to}: {e}")
        return {"success": False, "message_id": "", "error": str(e)}


def _wa_send_template(to: str, template_name: str, params: list, language: str = "en", name: str = "") -> dict:
    """
    Send an approved WhatsApp message template via Meta Cloud API.
    `params` is an ordered list of strings filling {{1}}, {{2}}, ... in the template body.
    Required for every business-initiated message: outside the 24h window that a
    customer's own message opens, Meta accepts free text and then drops it.
    """
    to_clean = to.lstrip("+")
    payload = {
        "messaging_product": "whatsapp",
        "to": to_clean,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": language},
            "components": [{
                "type": "body",
                "parameters": [{"type": "text", "text": str(p)} for p in params],
            }],
        },
    }
    headers = {
        "Authorization": f"Bearer {META_WA_TOKEN}",
        "Content-Type": "application/json",
    }
    try:
        resp = requests.post(META_WA_API_URL, json=payload, headers=headers, timeout=10)
        data = resp.json()
        if resp.status_code == 200 and "messages" in data:
            msg_id = data["messages"][0].get("id", "")
            body_preview = f"[{template_name}] " + " | ".join(str(p) for p in params)
            _log_wa_message(to_clean, "out", body_preview, name=name, message_id=msg_id)
            return {"success": True, "message_id": msg_id, "error": ""}
        else:
            error = data.get("error", {}) or {}
            err = error.get("message", str(data))
            print(f"[Meta WA] Template send failed to {to}: {err}")
            return {"success": False, "message_id": "", "error": err, "code": error.get("code")}
    except Exception as e:
        print(f"[Meta WA] Exception sending template to {to}: {e}")
        return {"success": False, "message_id": "", "error": str(e), "code": None}


# Meta error codes meaning "this template can't be used" — it doesn't exist, isn't
# approved yet, was paused or deleted, or the parameters don't match its body.
# Anything else (a bad token, a blocked number) is a real failure worth reporting.
TEMPLATE_UNUSABLE_CODES = {132000, 132001, 132005, 132007, 132012, 132015, 132016, 132068, 132069}


def _wa_notify(to: str, template_name: str, params: list, fallback_body: str, name: str = "") -> dict:
    """
    Send a business-initiated WhatsApp message: one the customer or team member
    didn't ask for in the last 24 hours.

    Meta only accepts free text inside the 24-hour window that opens when someone
    messages us. Outside it, free text is accepted by the API (HTTP 200, message
    id and all) and then silently dropped, reported later on the status webhook as
    error 131047. So every one of these has to go out as an approved template.

    While a template is still awaiting approval, this falls back to the free-text
    body — that at least reaches anyone inside an open window, which is what the
    old behaviour managed, and the log line says why it happened.
    """
    result = _wa_send_template(to, template_name, _template_params(params), name=name)
    if result["success"] or result.get("code") not in TEMPLATE_UNUSABLE_CODES:
        return result
    print(f"[Meta WA] Template '{template_name}' unusable ({result['error']}) — "
          f"falling back to free text for {to}, which only delivers inside an open 24h window")
    fallback = _wa_send(to, fallback_body, name=name)
    # The free-text send reports success off an HTTP 200, but Meta drops it
    # unread outside the 24h window and only says so later on the status
    # webhook. Callers that tell a human "the client was notified" have to be
    # able to tell this apart from a real delivery, so say it here rather than
    # letting a 200 stand in for "they got it".
    fallback["delivery_uncertain"] = True
    fallback["template_unusable"] = template_name
    fallback["template_error"] = result.get("error", "")
    return fallback


def _template_params(params: list) -> list:
    """
    Meta rejects template parameters containing newlines or tabs, and caps their
    length. Review text and error messages routinely carry both.
    """
    cleaned = []
    for p in params:
        text = " ".join(str(p if p not in (None, "") else "not provided").split())
        cleaned.append(text[:900])
    return cleaned


def _log_wa_message(phone_digits: str, direction: str, text: str, name: str = "", message_id: str = "", force: bool = False) -> None:
    """
    Append a message to a customer's WhatsApp conversation thread in Firebase,
    so the admin panel can show chat history and Zawadi has memory. Team
    numbers are excluded by default so internal alert broadcasts don't clutter
    the conversation list — pass force=True for a genuine conversation
    (inbound message or bot/human reply) even if that number is also a team
    number, e.g. someone testing Zawadi from their own phone.
    """
    if not force and phone_digits in {n.lstrip("+") for n in TEAM_NUMBERS}:
        return
    now = datetime.now(timezone.utc).isoformat()
    try:
        requests.post(f"{FIREBASE_WA_CONVOS_BASE}/{phone_digits}/messages.json", json={
            "direction": direction,
            "text": text,
            "timestamp": now,
            "messageId": message_id,
        }, headers=_firebase_auth_headers(), timeout=5)
        meta = {
            "phone": f"+{phone_digits}",
            "lastMessage": text,
            "lastMessageAt": now,
            "lastDirection": direction,
            "unread": direction == "in",
        }
        if name:
            meta["name"] = name
        requests.patch(f"{FIREBASE_WA_CONVOS_BASE}/{phone_digits}/meta.json", json=meta, headers=_firebase_auth_headers(), timeout=5)
    except Exception as e:
        print(f"[WA conversation log] error: {e}")


def _send_email(to: str, subject: str, html: str) -> dict:
    """
    Send a transactional email via Resend.
    Returns a dict with keys: success (bool), id (str), error (str).
    """
    if not RESEND_API_KEY:
        print("[Resend] RESEND_API_KEY not set — skipping email send")
        return {"success": False, "id": "", "error": "RESEND_API_KEY not configured"}

    payload = {"from": RESEND_FROM, "to": [to], "subject": subject, "html": html}
    headers = {
        "Authorization": f"Bearer {RESEND_API_KEY}",
        "Content-Type": "application/json",
    }
    try:
        resp = requests.post(RESEND_API_URL, json=payload, headers=headers, timeout=10)
        data = resp.json()
        if resp.status_code in (200, 201) and data.get("id"):
            return {"success": True, "id": data["id"], "error": ""}
        else:
            err = data.get("message", str(data))
            print(f"[Resend] Send failed to {to}: {err}")
            return {"success": False, "id": "", "error": err}
    except Exception as e:
        print(f"[Resend] Exception sending to {to}: {e}")
        return {"success": False, "id": "", "error": str(e)}


RESEND_BATCH_URL = "https://api.resend.com/emails/batch"


def _send_email_batch(emails: list) -> dict:
    """
    Send up to 100 emails in one Resend API call (POST /emails/batch).
    `emails` is a list of {from, to, subject, html} dicts — each can have a
    different recipient/body, unlike a single broadcast to one audience.
    Returns a dict with keys: success (bool), error (str).
    """
    if not RESEND_API_KEY:
        return {"success": False, "error": "RESEND_API_KEY not configured"}
    headers = {
        "Authorization": f"Bearer {RESEND_API_KEY}",
        "Content-Type": "application/json",
    }
    try:
        resp = requests.post(RESEND_BATCH_URL, json=emails, headers=headers, timeout=20)
        data = resp.json()
        if resp.status_code in (200, 201):
            return {"success": True, "error": ""}
        else:
            err = data.get("message", str(data))
            print(f"[Resend batch] Send failed: {err}")
            return {"success": False, "error": err}
    except Exception as e:
        print(f"[Resend batch] Exception: {e}")
        return {"success": False, "error": str(e)}


def _unsub_token(email: str) -> str:
    """Keyed hash of a lowercased email — lets someone unsubscribe their own
    address via a plain link with no login, but can't be forged for another
    address without also knowing UNSUB_SECRET."""
    return hmac.new(UNSUB_SECRET.encode(), email.strip().lower().encode(), hashlib.sha256).hexdigest()[:24]


def _unsub_link(email: str) -> str:
    return f"{SERVICE_URL}/unsubscribe?email={urllib.parse.quote(email)}&token={_unsub_token(email)}"


def _email_footer(to_email: str) -> str:
    """Standard footer appended to every subscriber-facing email — required
    so newsletter/broadcast sends have a working one-click unsubscribe."""
    return f"""
    <p style="font-size:11px;color:{EMAIL_TEXT_DIM};margin-top:28px;border-top:1px solid {EMAIL_BORDER};padding-top:12px;">
      You're receiving this because you subscribed at optimumprimesolutions.co.ke.
      <a href="{_unsub_link(to_email)}" style="color:{EMAIL_TEXT_DIM};">Unsubscribe</a>
    </p>
    """


def _first_name(subscriber: dict) -> str:
    """
    First name to use in an email greeting, or '' if none is stored.
    Deliberately does NOT guess from the email address (e.g. 'info@...' or
    'optimumprimesolutionsltd@...' produce awkward, wrong-looking
    greetings) — callers should omit the greeting entirely when this is
    empty (shared/team inboxes), rather than falling back to something
    generic like 'Hi there,'.
    """
    name = (subscriber.get("name") or "").strip()
    return name.split()[0] if name else ""


def _verify_resend_webhook(payload: bytes, headers: dict) -> bool:
    """
    Verify an inbound Resend webhook request using its Svix signing secret.
    Returns False (reject) if verification fails or the secret isn't configured.
    """
    if not RESEND_WEBHOOK_SECRET:
        print("[Resend webhook] RESEND_WEBHOOK_SECRET not set — rejecting inbound webhook")
        return False
    try:
        from svix.webhooks import Webhook, WebhookVerificationError
        wh = Webhook(RESEND_WEBHOOK_SECRET)
        wh.verify(payload, headers)
        return True
    except WebhookVerificationError as e:
        print(f"[Resend webhook] Signature verification failed: {e}")
        return False
    except Exception as e:
        print(f"[Resend webhook] Verification error: {e}")
        return False


def _resend_get_received_email(email_id: str) -> dict:
    """Fetch the full body of a received email (webhooks only carry metadata)."""
    headers = {"Authorization": f"Bearer {RESEND_API_KEY}"}
    resp = requests.get(f"https://api.resend.com/emails/receiving/{email_id}", headers=headers, timeout=10)
    resp.raise_for_status()
    return resp.json()


# ── Google Meet Link Generator ───────────────────────────────────────────────

def generate_meet_link(name: str, company: str, date_str: str, time_slot: str) -> str:
    """
    Generate a deterministic Google Meet link tied to the booking.
    Format: meet.google.com/xxx-xxxx-xxx (10 chars from booking hash)
    """
    seed = f"{name}-{company}-{date_str}-{time_slot}".lower().replace(" ", "")
    h = hashlib.md5(seed.encode()).hexdigest()
    # Build a meet-style code: 3-4-3 letter groups (a-z only)
    letters = ''.join(c for c in h if c.isalpha())[:10].ljust(10, 'a')
    code = f"{letters[0:3]}-{letters[3:7]}-{letters[7:10]}"
    return f"https://meet.google.com/{code}"


# ── Google Calendar Link Builder ──────────────────────────────────────────────

def build_google_calendar_link(name: str, company: str, date_str: str, time_slot: str) -> str:
    """
    Build a Google Calendar 'Add to Calendar' link for a 1-hour TallyPrime demo.
    time_slot format: "10:00 AM – 11:00 AM"
    date_str format:  "2026-07-15"
    """
    if not date_str or not time_slot:
        return ""
    try:
        # Extract start time from slot e.g. "10:00 AM – 11:00 AM" → "10:00 AM"
        start_str = time_slot.split("–")[0].strip()
        # Two shapes reach here: the public form's 12h range and the admin
        # pop-up's 24h slot ("14:00"). Only the first used to parse, so every
        # demo booked by the team came out with no calendar link at all.
        try:
            dt_naive = datetime.strptime(f"{date_str} {start_str}", "%Y-%m-%d %I:%M %p")
        except ValueError:
            dt_naive = datetime.strptime(f"{date_str} {start_str}", "%Y-%m-%d %H:%M")
        eat = timezone(timedelta(hours=3))
        dt_eat  = dt_naive.replace(tzinfo=eat)
        dt_utc  = dt_eat.astimezone(timezone.utc)
        dt_end  = dt_utc + timedelta(hours=1)

        def fmt(d: datetime) -> str:
            return d.strftime("%Y%m%dT%H%M%SZ")

        client_label = company if company else name
        title    = urllib.parse.quote(f"TallyPrime Demo — {client_label}")
        details  = urllib.parse.quote(f"TallyPrime demo | Optimum Prime Solutions | +254116246074")
        location = urllib.parse.quote("Google Meet")

        return (
            f"https://calendar.google.com/calendar/render?action=TEMPLATE"
            f"&text={title}"
            f"&dates={fmt(dt_utc)}/{fmt(dt_end)}"
            f"&details={details}"
            f"&location={location}"
        )
    except Exception:
        return ""


def format_time_display(time_str: str) -> str:
    """
    A time a client can read, from either shape the app stores.

    The admin booking pop-up saves 24h slots ("14:00"); the public request form
    saves hour ranges ("2:00 PM – 3:00 PM"). Confirmations went out with the
    raw stored value, so a client approved for a 2pm demo was told "14:00".
    Anything unrecognised is passed through untouched.
    """
    t = (time_str or "").strip()
    if not t:
        return t
    try:
        return datetime.strptime(t, "%H:%M").strftime("%I:%M %p").lstrip("0")
    except ValueError:
        return t


# ── Bookable days and hours ──────────────────────────────────────────────────
# The same rules the website's own demo form applies, in Python so the chat
# widget can offer real slots as tappable chips rather than asking a visitor to
# type a date and then telling them it was a Sunday.

KE_HOLIDAYS_RECURRING = {"01-01", "05-01", "06-01", "10-10", "10-20", "12-12", "12-25", "12-26"}
# Easter moves each year, so those are listed out rather than computed.
KE_HOLIDAYS_ONEOFF = {"2026-04-03", "2026-04-06", "2027-03-26", "2027-03-29"}


def is_date_blocked(date_str: str) -> bool:
    """Sunday or Kenyan public holiday — we take no bookings at all."""
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return True
    if d.weekday() == 6:  # Sunday
        return True
    return date_str[5:] in KE_HOLIDAYS_RECURRING or date_str in KE_HOLIDAYS_ONEOFF


def bookable_days(count: int = 5) -> list:
    """The next `count` days we are open, starting tomorrow, as (iso, label)."""
    out = []
    day = datetime.now(timezone(timedelta(hours=3))).date() + timedelta(days=1)
    # 30 days is far more than enough to find 5 open ones; it also stops a bad
    # holiday list turning this into an infinite loop.
    for _ in range(30):
        iso = day.isoformat()
        if not is_date_blocked(iso):
            # Built by hand rather than with %-d, which is not portable —
            # this runs on Linux but is edited on Windows.
            out.append((iso, f"{day.strftime('%a')} {day.day} {day.strftime('%b')}"))
        if len(out) >= count:
            break
        day += timedelta(days=1)
    return out


def bookable_hours(date_str: str) -> list:
    """Hour slots for a date, as 12-hour labels. Empty when we are closed."""
    if is_date_blocked(date_str):
        return []
    try:
        saturday = datetime.strptime(date_str, "%Y-%m-%d").weekday() == 5
    except ValueError:
        return []
    blocks = [(8, 13)] if saturday else [(8, 13), (14, 17)]
    labels = []
    for start, end in blocks:
        for h in range(start, end):
            ampm = "AM" if h < 12 else "PM"
            h12 = 12 if h % 12 == 0 else h % 12
            labels.append(f"{h12}:00 {ampm}")
    return labels


def format_date_display(date_str: str) -> str:
    """Convert '2026-07-15' to 'Wednesday, 15 July 2026'."""
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        return dt.strftime("%A, %d %B %Y")
    except Exception:
        return date_str


# ── Firebase Helpers ──────────────────────────────────────────────────────────

def get_registration_count() -> int:
    try:
        resp = requests.get(FIREBASE_WEBINAR_URL, headers=_firebase_auth_headers(), timeout=5)
        data = resp.json()
        return len(data) if data and isinstance(data, dict) else 0
    except Exception:
        return -1

def fetch_firebase(url: str) -> dict:
    try:
        resp = requests.get(url, headers=_firebase_auth_headers(), timeout=8)
        data = resp.json()
        return data if isinstance(data, dict) and "error" not in data else {}
    except Exception:
        return {}


# ── WhatsApp Messaging ────────────────────────────────────────────────────────

def notify_team_email(lead: dict) -> dict:
    """
    Email ADMIN_NOTIFY_EMAIL with the same lead details sent to the team over
    WhatsApp. Best-effort — a missing ADMIN_NOTIFY_EMAIL/RESEND_API_KEY or a
    Resend failure never blocks the WhatsApp alert or the lead save.
    """
    if not ADMIN_NOTIFY_EMAIL:
        return {"success": False, "error": "ADMIN_NOTIFY_EMAIL not configured"}

    name      = lead.get("name", "Unknown")
    phone     = lead.get("phone", "Not provided")
    email     = lead.get("email", "Not provided")
    company   = lead.get("company", "Not provided")
    interest  = lead.get("interest", "General enquiry")
    source    = lead.get("source", "Website")
    message   = lead.get("message", "")
    demo_date = lead.get("demoDate", "")
    demo_time = lead.get("demoTime", "")

    rows = [
        ("Name", name), ("Company", company), ("Phone", phone), ("Email", email),
        ("Interest", interest), ("Source", source),
    ]
    if demo_date:
        rows.append(("Preferred date", demo_date))
    if demo_time:
        rows.append(("Preferred time", demo_time))
    if message:
        rows.append(("Message", message))

    rows_html = "".join(
        f'<tr><td style="padding:6px 12px;color:#888;white-space:nowrap;">{html.escape(k)}</td>'
        f'<td style="padding:6px 12px;">{html.escape(str(v))}</td></tr>'
        for k, v in rows
    )
    email_html = f"""
    <div style="font-family: -apple-system, Segoe UI, Roboto, sans-serif; max-width: 520px; margin: 0 auto; color: #1A1A2E;">
      <h1 style="color: #C0392B; font-size: 20px;">🔔 New Lead — Optimum Prime Solutions</h1>
      <table style="border-collapse: collapse; font-size: 14px;">{rows_html}</table>
      <p style="font-size: 13px; color: #888; margin-top: 24px;">
        Reply quickly — leads convert best within 5 minutes.<br/>
        <a href="https://www.optimumprimesolutions.co.ke/admin" style="color: #C0392B;">Manage in admin panel</a>
      </p>
    </div>
    """
    return _send_email(ADMIN_NOTIFY_EMAIL, f"New lead: {name} — {interest}", email_html)


def notify_team(lead: dict) -> list:
    name      = lead.get("name", "Unknown")
    phone     = lead.get("phone", "Not provided")
    email     = lead.get("email", "Not provided")
    company   = lead.get("company", "Not provided")
    interest  = lead.get("interest", "General enquiry")
    source    = lead.get("source", "Website")
    message   = lead.get("message", "")
    demo_date = lead.get("demoDate", "")
    demo_time = lead.get("demoTime", "")

    is_webinar = "Webinar" in interest
    results = []

    # Best-effort email alert alongside WhatsApp — never let an email failure
    # block or delay the WhatsApp notifications below.
    try:
        notify_team_email(lead)
    except Exception as e:
        print(f"[Team email] error: {e}")

    if not is_webinar:
        # The preferred slot was read above and then dropped: the team's email
        # alert carries it, the WhatsApp one — the one actually read first —
        # did not, so whoever picked up a lead had to open the panel to find
        # out when the person wanted to be seen. `new_lead_alert` has four
        # fixed parameters, so it rides in alongside the interest.
        slot_bits = [format_date_display(demo_date) if demo_date else "", format_time_display(demo_time)]
        slot = " at ".join(b for b in slot_bits if b)
        interest_line = f"{interest} — wants {slot}" if slot else interest
        # Uses the approved `new_lead_alert` template — free text to these numbers is
        # dropped unless someone on the team messaged us in the last 24 hours.
        for to in TEAM_NUMBERS:
            r = _wa_send_template(to, "new_lead_alert", [name, company, phone, interest_line])
            results.append({"to": to, "message_id": r.get("message_id", ""), "success": r["success"], "error": r.get("error", "")})
        return results

    # Webinar registrations use the `webinar_registration_alert` template, falling back
    # to this richer free-text body while that template is still awaiting approval.
    count = get_registration_count()
    count_line = f"📊 *Total registrations so far:* {count}\n" if count != -1 else "📊 *Total registrations:* (unavailable)\n"
    header = "🔔 *New Webinar Registration — Optimum Prime Solutions*"

    body = (
        f"{header}\n\n"
        f"👤 *Name:* {name}\n"
        f"🏢 *Company:* {company}\n"
        f"📞 *Phone:* {phone}\n"
        f"📧 *Email:* {email}\n"
        f"💼 *Interest:* {interest}\n"
        f"📍 *Source:* {source}\n"
    )
    if message:
        body += f"💬 *Message:* {message}\n"
    if count_line:
        body += f"\n{count_line}"

    body += (
        f"\n👉 *Manage in admin panel:*\n"
        f"https://www.optimumprimesolutions.co.ke/admin\n"
        f"\n_Reply quickly — leads convert best within 5 minutes!_ ⚡"
    )

    for to in TEAM_NUMBERS:
        r = _wa_notify(to, "webinar_registration_alert",
                       [name, company, phone, str(count) if count != -1 else "unavailable"],
                       body)
        results.append({"to": to, "message_id": r.get("message_id", ""), "success": r["success"], "error": r.get("error", "")})
    return results


def reply_to_lead(lead: dict) -> dict:
    phone = lead.get("phone", "").strip().replace(" ", "")
    if not phone:
        return {"success": False, "reason": "No phone number provided"}
    # Normalise Kenyan phone numbers to E.164 format
    if phone.startswith("0") and len(phone) == 10:
        phone = "+254" + phone[1:]          # 0712345678 → +254712345678
    elif phone.startswith("254") and not phone.startswith("+"):
        phone = "+" + phone                 # 254712345678 → +254712345678
    elif not phone.startswith("+"):
        phone = "+254" + phone              # 712345678 → +254712345678

    name     = lead.get("name", "there")
    interest = lead.get("interest", "TallyPrime")

    # Custom message (e.g. a webinar confirmation from send_webinar_invite.py) takes
    # priority. It stays free text because the content is arbitrary per call, so no fixed
    # template covers it — which means it only reaches recipients inside an open 24h
    # window. Bulk outreach through these scripts needs its own approved template.
    custom_msg = lead.get("confirmation_message", "")
    if custom_msg:
        r = _wa_send(phone, custom_msg)
    else:
        # Uses the approved `lead_confirmation` template — free text to a lead who has
        # not messaged us in the last 24 hours is dropped.
        r = _wa_send_template(phone, "lead_confirmation", [name, interest])

    if r["success"]:
        return {"success": True, "message_id": r["message_id"], "to": phone}
    else:
        return {"success": False, "reason": r["error"], "to": phone}


# ── CSV Export Helpers ────────────────────────────────────────────────────────

def to_local_phone(phone: str) -> str:
    """Convert any Kenyan phone format to local 07XX XXX XXX (10 digits, leading 0)."""
    p = str(phone).strip().replace(" ", "").replace("-", "")
    if not p:
        return p
    if p.startswith("+254") and len(p) == 13:
        return "0" + p[4:]          # +254712345678 → 0712345678
    if p.startswith("254") and len(p) == 12:
        return "0" + p[3:]          # 254712345678  → 0712345678
    if p.startswith("0") and len(p) == 10:
        return p                     # already correct
    if len(p) == 9:                  # 712345678 (missing leading 0)
        return "0" + p
    return p                         # return as-is if unrecognised


def build_leads_csv():
    data = fetch_firebase(FIREBASE_LEADS_URL)
    output = io.StringIO()
    fields = ["Name", "Company", "Phone", "Email", "Business Type",
              "Current Software", "Preferred Demo Date", "Preferred Time", "Message", "Status", "Submitted At"]
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    rows = []
    for key, r in data.items():
        if not isinstance(r, dict):
            continue
        rows.append({
            "Name":                r.get("name", ""),
            "Company":             r.get("company", ""),
            "Phone":               to_local_phone(r.get("phone", "")),
            "Email":               r.get("email", ""),
            "Business Type":       r.get("businessType", ""),
            "Current Software":    r.get("currentSoftware", ""),
            "Preferred Demo Date": r.get("demoDate", ""),
            "Preferred Time":      r.get("demoTime", ""),
            "Message":             r.get("message", ""),
            "Status":              r.get("status", "New"),
            "Submitted At":        r.get("createdAt", ""),
        })
    rows.sort(key=lambda x: x["Submitted At"])
    writer.writerows(rows)
    return output.getvalue(), len(rows)

def build_webinar_csv():
    data = fetch_firebase(FIREBASE_WEBINAR_URL)
    output = io.StringIO()
    fields = ["Name", "Company", "Phone", "Email", "Webinar", "Registered At"]
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    rows = []
    for key, r in data.items():
        if not isinstance(r, dict):
            continue
        rows.append({
            "Name":          r.get("name", ""),
            "Company":       r.get("company", ""),
            "Phone":         to_local_phone(r.get("phone", "")),
            "Email":         r.get("email", ""),
            "Webinar":       r.get("webinar", "TallyPrime 7.1"),
            "Registered At": r.get("timestamp", r.get("registeredAt", "")),
        })
    rows.sort(key=lambda x: x["Registered At"])
    writer.writerows(rows)
    return output.getvalue(), len(rows)


# ── Routes ────────────────────────────────────────────────────────────────────

# ── Zawadi AI System Prompt ───────────────────────────────────────────────────
ZAWADI_SYSTEM_PROMPT = """
You are Zawadi, the friendly and knowledgeable AI assistant for Optimum Prime Solutions — Kenya's certified TallyPrime partner based in Nairobi.

Your role is to help business owners and managers in Kenya discover the right solutions for their business, answer questions, and guide them toward booking a demo or speaking with an expert.

ABOUT OPTIMUM PRIME SOLUTIONS:
- Kenya's certified TallyPrime partner
- Services: TallyPrime accounting software, Cloud Hosting, EOS® Business Consulting, Biz Analyst
- Phone: +254 116 246 074
- Website: www.optimumprimesolutions.co.ke
- Location: Nairobi, Kenya

TALLYPRIME EDITIONS (Kenya pricing in KES):
- Silver: Single user, ideal for small businesses. Handles invoicing, VAT, KRA eTIMS compliance.
- Gold: Multi-user, ideal for growing businesses with multiple staff or branches. Includes all Silver features plus multi-user access and advanced reporting.
- Prime: Enterprise-level, for large organisations with complex needs.
- All editions support KRA eTIMS (electronic tax invoice management system) compliance.
- TallyPrime 7.1 new features: Auto Wrap Text, 8 professional invoice print templates, Scheduled Auto Backup, Reuse Deleted Voucher Numbers.

CLOUD HOSTING:
- Host TallyPrime on a secure cloud server — access from anywhere in Kenya or globally.
- Automatic daily backups, 99.9% uptime guarantee.
- Ideal for businesses with remote teams, multiple branches, or staff working from home.
- Eliminates the risk of data loss from hardware failure.

EOS® (ENTREPRENEURIAL OPERATING SYSTEM):
- A proven business management framework used by thousands of companies globally.
- Helps leadership teams get clarity on Vision, Traction, and Team Health.
- Tools include: Level 10 Meetings, Scorecards, Rocks (quarterly priorities), People Analyser.
- Ideal for SMEs with 10–250 employees that want structured, accountable growth.
- Optimum Prime Solutions is a certified EOS Implementer.

BIZ ANALYST:
- Mobile business analytics app that connects to TallyPrime.
- View real-time sales, inventory, and financial reports on your phone.
- Ideal for business owners who want visibility on the go.

KRA eTIMS COMPLIANCE:
- All TallyPrime editions support Kenya Revenue Authority eTIMS (Electronic Tax Invoice Management System).
- Businesses in Kenya are required to issue eTIMS-compliant invoices.
- TallyPrime automates this — no manual submission needed.

COMMON CUSTOMER PROFILES:
- Retail shops, wholesale distributors, manufacturers, service businesses, NGOs, schools.
- Businesses currently using Excel, QuickBooks, Sage, or manual records.
- Businesses with 1–200+ employees.

BOOKING MANDATE — DEMO, CONSULTATION, OR BIZ ANALYST:
You have full authority to collect booking requests on behalf of Optimum Prime Solutions. When a user wants to book, schedule, or says anything like "book", "I want to see it", "show me", "interested", "let's proceed", "consultation", "EOS", "biz analyst", "analytics", first ask:
"What would you like to book?
1️⃣ *TallyPrime Demo* — see the accounting software in action
2️⃣ *EOS® Business Consultation* — a 90-min session on the Entrepreneurial Operating System
3️⃣ *Biz Analyst Enquiry* — learn how Biz Analyst integrates with TallyPrime for business intelligence"

FIRST, TAKE WHAT THEY HAVE ALREADY GIVEN YOU. Before you ask anything, re-read the whole conversation and pull out every booking detail the user has already supplied — including several in a single message, and including ones they volunteered before you asked. People often write "Hi, I'm James Mwangi from Acme Ltd, 0712 345 678, can we do Tuesday at 10am online?" — that is five of the six details. Treat every one of them as collected.

NEVER ask for a detail you have already been given. Re-asking is the single most irritating thing you can do: it tells the customer you weren't listening, and it is the fastest way to lose a booking. If you are unsure whether something was meant as an answer, confirm it back ("I have Acme Ltd as the company — correct?") rather than asking the question again from scratch.

Then collect ONLY THE DETAILS STILL MISSING, one at a time, in this order:

1. Full name — if you already know it (from earlier in this conversation or the WHATSAPP PROFILE note below), confirm it briefly instead of asking from scratch, e.g. "I'll use {name} for this booking — is that right?"
2. WhatsApp phone number (Kenyan format, e.g. 0712 345 678) — if this conversation is happening on WhatsApp (i.e. there is a WHATSAPP PROFILE note below), you already have their number. Do NOT ask for it — skip straight to company name, and use an empty string "" for phone in the final JSON below (our system fills it in automatically). Only ask for a phone number if there is no WHATSAPP PROFILE note at all (i.e. this is the website chat widget).
3. Company name
4. Email address — ask for it plainly: "What email should I send the confirmation to?". This is how we reach them if WhatsApp does not get through, so it is worth asking for, but it is NOT compulsory. If they would rather not give one, say that is fine, use an empty string "" for email in the JSON, and carry on. Never ask twice and never hold up the booking over it.
5. Preferred date (remind them: Mon–Fri 8AM–5PM, Sat 8AM–12PM, no Sundays or public holidays)
6. Preferred time slot (e.g. 10:00 AM, 2:00 PM)
7. Session type: Online (Google Meet) or Physical (at our Nairobi office)

RULES:
- Ask ONE question at a time. Do not ask multiple questions in one message. This governs how you ASK — it does not limit how much you ACCEPT: if one message from the user answers four questions, take all four and move on to the first one still outstanding.
- If a single message completes every detail you need, do not ask anything further — go straight to the summary and ask them to confirm.
- Keep a running tally of what you have. Before each question, ask yourself "do I already have this?" — if yes, skip it.
- If they give an invalid date (Sunday, public holiday, or past date), politely explain and ask again.
- Kenya public holidays to block: 1 Jan, 1 May, 1 Jun, 10 Oct, 20 Oct, 12 Dec, 25 Dec, 26 Dec, and Easter (Good Friday + Easter Monday).
- If they pick Saturday, remind them slots are 8AM–12PM only.
- Once you have ALL 6 details, confirm them back to the user in a friendly summary and ask them to confirm.
- After they confirm, respond with ONLY this exact JSON (no other text before or after):
  {"booking": true, "name": "<name>", "phone": "<phone>", "email": "<email, or empty string>", "company": "<company>", "demoDate": "<YYYY-MM-DD>", "demoTime": "<HH:MM>", "demoType": "<online|physical>", "requestType": "<demo|consultation|bizanalyst>"}
- The demoDate MUST be in YYYY-MM-DD format. The demoTime MUST be in 24-hour HH:MM format (e.g. 10:00, 14:30).
- Set requestType to "consultation" if the user chose EOS® Business Consultation, "bizanalyst" if they chose Biz Analyst Enquiry, otherwise "demo".
- IMPORTANT: The booking is NOT immediately confirmed. Our team reviews and approves the slot. Tell the user: "We've received your request and our team will confirm your slot shortly via WhatsApp."
- Do NOT tell the user the demo is confirmed or give them a Meet link — that comes later from our team.
- If the user declines to provide any detail, offer the website form: www.optimumprimesolutions.co.ke/contact#demo-form

GENERAL HANDOFF (non-booking enquiries):
When the user wants to speak to a person, get a quote, or be called back — the same rule applies: whatever they have already told you counts, and is never asked for twice.
When the user wants to speak to a person, get a quote, or be called back:
1. Name: if you already know their name — either from earlier in this conversation, or from the WHATSAPP PROFILE note below — do NOT ask again from scratch. Just confirm it briefly, e.g. "I'll pass this to our team as {name} — is that the right name to use?" Only ask "What's your name?" outright if you truly have no name to work with.
2. Phone number: if this conversation is happening on WhatsApp, you already have their number — do NOT ask for it. Only ask for a phone number if there is no WHATSAPP PROFILE note at all (i.e. this is the website chat widget, not WhatsApp).
3. Once you have a name (confirmed or given) and a phone number (known from WhatsApp, or given on the website), respond with ONLY this exact JSON:
   {"handoff": true, "name": "<their name>", "phone": "<their phone, or empty string if on WhatsApp and not separately given>", "interest": "<brief summary>"}
4. Do NOT include any other text before or after the JSON.

If this is the website widget (no WHATSAPP PROFILE note) and the user declines to provide their number, respond:
"No problem! You can reach us anytime on WhatsApp at +254 727 209 720 or book a demo at www.optimumprimesolutions.co.ke/contact#demo-form"

PROACTIVE ESCALATION (different from the handoff above — this is YOUR call, not the user's request):
Sometimes you should hand off even though the user never asked for a person — for example: they've asked essentially the same question 2+ times without a satisfying answer, they express frustration ("this isn't helping", "you don't understand", "forget it", "never mind"), their question is genuinely outside TallyPrime / Cloud Hosting / EOS® / Biz Analyst and you cannot help, or the conversation is clearly going in circles.
When that happens, respond with ONLY this exact JSON (no other text before or after):
{"escalate": true, "reason": "<brief description, e.g. 'user frustrated after repeated pricing questions'>"}
Do NOT use this for questions you CAN answer — only when you genuinely cannot help further. This is rare; most conversations should not trigger it.

PAST EVENTS (already held — mention only if the user asks about previous/recent events, or to show our track record; never present these as upcoming or invite people to register for them):
- Free TallyPrime 7.1 webinar — held Wednesday 15th July 2026 (online). This event has already taken place.
- Inventory Management Breakfast Workshop (FREE) — held Friday, 24th July 2026 at Ndanga Hotel, Ruiru. Topics covered: stock control & reorder points, TallyPrime inventory features, audit & reconciliation tips, and a live Q&A. This event has already taken place.
UPCOMING EVENTS: There are no upcoming events currently scheduled. If the user is interested in the next webinar, workshop, or training, invite them to contact us on +254 116 246 074 so we can notify them when the next one is announced. Do NOT proactively mention events unless the user asks about events, webinars, workshops, or upcoming training, and never invent event dates — if unsure whether an event is upcoming, treat it as not scheduled and direct the user to +254 116 246 074.

QUICK REPLY BUTTONS:
When your message ends with a question whose answer is a small, closed set, put a marker on the very last line so the website widget can offer the answers as buttons to tap. Format:
  [[chips: Option one | Option two | Option three]]

Two of these you must NOT write out yourself — our system holds the real opening days and hours, and a slot you invent is one we then have to ring back and take away:
  [[chips:dates]]                 — offers the next days we are open
  [[chips:times:YYYY-MM-DD]]      — offers the hours we work on that date (use the date already agreed)

Rules for the marker:
- On its own line, at the very end of the message, with nothing after it.
- At most 8 options, each under 40 characters.
- Each option must read as a complete answer to the question you just asked — tapping one sends it back as the customer's own reply, word for word.
- Use it for: what they want to book, session type, the date, the time, and yes/no confirmations.
- Do NOT use it for open questions — a name, company or phone number has nothing to offer.
- NEVER put it on a message that is JSON (booking, handoff or escalate). Those must stay ONLY the JSON.
- The customer never sees the marker itself, so never mention buttons, never describe them, and never repeat the options in your text as well.

Examples:
  "What would you like to book?" → [[chips: TallyPrime Demo | EOS® Consultation | Biz Analyst]]
  "Would you like this online or at our Nairobi office?" → [[chips: Online | Physical]]
  "Which day suits you?" → [[chips:dates]]
  "What time works on that day?" → [[chips:times:2026-09-15]]
  "Shall I put that through?" → [[chips: Yes, book it | Change something]]

CONVERSATION STYLE:
- Warm, professional, and concise. Use simple English suitable for Kenyan business owners.
- Ask one question at a time to understand the user's business before recommending.
- Use bullet points or short paragraphs — avoid walls of text.
- Use bold for product names and key terms.
- Never make up prices — say "contact us for current pricing" if unsure.
- Always end with a clear next step (book a demo or chat on WhatsApp). Only suggest the webinar if the user has asked about events or training.
- If the user greets you, greet back warmly and ask their name.
- If you know their name, use it naturally in conversation.
"""

CHIP_MARKER = re.compile(r"\[\[\s*chips\s*:(.*?)\]\]", re.IGNORECASE | re.DOTALL)
MAX_CHIPS = 8
MAX_CHIP_LEN = 40


def extract_chips(reply: str) -> tuple:
    """
    Pull Zawadi's suggested quick replies out of a message and strip the marker.

    Zawadi ends a message with `[[chips: A | B | C]]` when the next answer is a
    small closed set, so the website widget can offer them as buttons instead of
    asking someone to type "physical" on a phone keyboard. Two of the sets it
    must not invent — the open days and the hours we work — so it writes
    `[[chips:dates]]` / `[[chips:times:YYYY-MM-DD]]` and they are filled in here
    from the real rules. That is the difference between a chip a visitor taps
    and a slot we then have to ring back and take away from them.

    Returns (clean_reply, chips). The marker is always removed, whatever the
    channel: a WhatsApp recipient has nothing to tap and must never see it.
    """
    if not reply:
        return reply, []

    matches = CHIP_MARKER.findall(reply)
    clean = CHIP_MARKER.sub("", reply).strip()
    if not matches:
        return clean, []

    # Only the last marker counts — if the model emitted two, the later one
    # belongs to the question it actually finished on.
    spec = matches[-1].strip()
    chips = []

    if spec.lower() == "dates":
        chips = [label for _iso, label in bookable_days(5)]
    elif spec.lower().startswith("times"):
        _, _, date_str = spec.partition(":")
        chips = bookable_hours(date_str.strip())
    else:
        chips = [c.strip() for c in spec.split("|")]

    # A chip is sent back verbatim as the visitor's next message, so anything
    # too long to read on a button is also too long to be a useful answer.
    seen = set()
    out = []
    for c in chips:
        c = " ".join(c.split())[:MAX_CHIP_LEN]
        if c and c.lower() not in seen:
            seen.add(c.lower())
            out.append(c)
    return clean, out[:MAX_CHIPS]


def get_zawadi_reply(messages: list, contact_name: str = "") -> str:
    """
    Call Google Gemini 2.5 Flash with the Zawadi system prompt.

    The system prompt is passed via `system_instruction` (the correct Gemini API
    field) so it is always active regardless of conversation length.

    Conversation history is rebuilt as a strictly alternating user/model sequence
    — the Gemini API rejects histories where two consecutive turns share the same
    role, which was causing Gemini to lose context and re-ask answered questions.

    `contact_name` is the sender's WhatsApp profile name (unavailable on the
    website widget) — told to Zawadi explicitly so it doesn't draw a blank if
    the customer says "I already gave you my name," and can confirm/use it
    for booking or handoff instead of just re-asking.
    """
    try:
        from google import genai
        from google.genai import types as genai_types
        gemini_key = os.environ.get("GEMINI_API_KEY", "")
        client = genai.Client(api_key=gemini_key)

        # Inject today's date so the AI always knows the correct year/date
        now_eat = datetime.now(timezone(timedelta(hours=3)))
        today_str = now_eat.strftime("%A, %d %B %Y")
        dynamic_prompt = (
            ZAWADI_SYSTEM_PROMPT
            + f"\n\nCURRENT DATE: Today is {today_str} (East Africa Time). "
            "Always use this when calculating dates, days of the week, or referring "
            "to upcoming events. Never assume the year is 2024."
        )
        if contact_name:
            dynamic_prompt += (
                f"\n\nWHATSAPP PROFILE: This customer's WhatsApp display name is \"{contact_name}\". "
                "This might be their personal name, or a business/generic name — it is NOT confirmed as "
                "their actual name yet. If you need their name for a booking or handoff and they haven't "
                "typed it in the conversation, ask them to confirm: e.g. \"I see your WhatsApp is set up "
                f"as '{contact_name}' — is that the name I should use, or would you like to give me another?\" "
                "If they say they \"already gave\" their name, this is almost certainly what they mean — "
                "use it (after confirming) rather than saying you have no name on record."
            )

        # ── Build a strictly alternating user/model history ───────────────────
        # The frontend sends roles as 'user' or 'assistant'; Gemini expects 'user'/'model'.
        # We merge any consecutive same-role turns into one to satisfy the API.
        raw: list[dict] = []
        for msg in messages:
            role = "user" if msg.get("role") == "user" else "model"
            text = (msg.get("content") or "").strip()
            if not text:
                continue
            if raw and raw[-1]["role"] == role:
                # Merge consecutive same-role turns (avoids API rejection)
                raw[-1]["text"] += "\n" + text
            else:
                raw.append({"role": role, "text": text})

        # Convert to Gemini contents format
        contents = [
            {"role": turn["role"], "parts": [{"text": turn["text"]}]}
            for turn in raw
        ]

        # Gemini requires the last turn to be from the user
        if not contents or contents[-1]["role"] != "user":
            contents.append({"role": "user", "parts": [{"text": "Hello"}]})

        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=contents,
            config=genai_types.GenerateContentConfig(
                system_instruction=dynamic_prompt,
                max_output_tokens=1500,
                temperature=0.7,
            ),
        )
        return response.text.strip()
    except Exception as e:
        print(f"Gemini error: {e}")
        return "I'm having a little trouble connecting right now. Please reach us directly on WhatsApp at +254 116 246 074 or visit www.optimumprimesolutions.co.ke"


def process_zawadi_reply(reply: str, from_phone: str = "", from_name: str = "") -> dict:
    """
    Detect whether Zawadi's reply is a booking/handoff/escalate JSON payload and,
    if so, run the same side effects (Firebase save, team alert, client
    confirmation) regardless of which channel (website widget or WhatsApp)
    triggered it. Always returns a dict with a 'reply' key holding text safe to
    show/send back.

    `from_phone`/`from_name` are the known WhatsApp sender identity (unavailable
    for the website widget) — used for the `escalate` signal, which doesn't ask
    Zawadi to collect contact details since WhatsApp already provides them.
    """
    import json as _json

    # Strip the quick-reply marker first, so nothing downstream ever sees it:
    # not the JSON detection below, not a WhatsApp recipient, not the customer.
    reply, chips = extract_chips(reply)

    # Where the lead actually came from, in the CRM's own vocabulary. Zawadi
    # answers on two channels and this function serves both: the website widget
    # posts to /chat with no sender identity, a WhatsApp message always carries
    # one. Writing the channel here — rather than a label like "Zawadi Chatbot
    # Booking" — is what stops these leads landing in the admin's "no source
    # recorded" queue: the CRM only recognises its own source values, and
    # anything else reads as Unknown however descriptive it looks.
    channel_source = "whatsapp" if from_phone else "website"

    try:
        clean = reply.strip()
        if clean.startswith('```'):
            clean = clean.split('```')[1]
            if clean.startswith('json'):
                clean = clean[4:]
        clean = clean.strip().lstrip('`').rstrip('`').strip()

        if clean.startswith('{') and ('"booking"' in clean or '"handoff"' in clean or '"escalate"' in clean):
            # Gemini sometimes appends a trailing sentence after the JSON despite
            # being told to reply with ONLY the JSON — e.g.
            # '{"booking": true, ...}We\'ve received your request...' — which
            # json.loads() rejects outright as invalid (trailing data after a
            # valid value). That exception used to be swallowed below and the
            # raw, half-JSON text got sent straight to the customer as if it
            # were an ordinary reply, with no lead ever created. raw_decode()
            # parses just the JSON object at the start and ignores anything
            # Gemini tacked on after it.
            parsed, _ = _json.JSONDecoder().raw_decode(clean)

            # ── DEMO BOOKING (end-to-end) ─────────────────────────────────────
            if parsed.get('booking'):
                name       = parsed.get('name') or from_name or 'Unknown'
                phone      = parsed.get('phone') or from_phone or ''
                email      = (parsed.get('email') or '').strip()
                company    = parsed.get('company', '')
                demo_date  = parsed.get('demoDate', '')   # YYYY-MM-DD
                demo_time  = parsed.get('demoTime', '')   # HH:MM 24h
                demo_type    = parsed.get('demoType', 'online').lower()
                request_type = parsed.get('requestType', 'demo').lower()

                norm_phone = normalize_phone(phone)

                try:
                    dt = datetime.strptime(demo_date, '%Y-%m-%d')
                    display_date = dt.strftime('%A, %d %B %Y')
                except Exception:
                    display_date = demo_date

                try:
                    t = datetime.strptime(demo_time, '%H:%M')
                    display_time = t.strftime('%I:%M %p').lstrip('0')
                except Exception:
                    display_time = demo_time

                try:
                    lead_record = {
                        'name':        name,
                        'phone':       phone,
                        # Written even when blank, so the CRM shows an empty
                        # address rather than no field at all - the admin
                        # panel reads this to decide whether a client can be
                        # confirmed by email when WhatsApp does not land.
                        'email':       email,
                        'company':     company,
                        'demoDate':    demo_date,
                        'demoTime':    demo_time,
                        'demoType':    demo_type,
                        'requestType': request_type,
                        'status':      'New',
                        'source':      channel_source,
                        # The raw origin, kept alongside the source rather than
                        # instead of it, so "which of these did the bot take?"
                        # is still answerable.
                        'capturedVia': 'Zawadi chatbot booking',
                        'message':     f'Preferred: {display_date} at {display_time} ({demo_type}) — {"Consultation" if request_type == "consultation" else "Demo"}',
                        'createdAt':   datetime.now(timezone.utc).isoformat(),
                    }
                    requests.post(FIREBASE_LEADS_URL, json=lead_record, headers=_firebase_auth_headers(), timeout=5)
                except Exception as e:
                    print(f'Firebase save error: {e}')

                try:
                    req_label = '🤝 Consultation (EOS®)' if request_type == 'consultation' else ('📱 Biz Analyst Enquiry' if request_type == 'bizanalyst' else '📊 TallyPrime Demo')
                    req_title = 'Consultation' if request_type == 'consultation' else ('Biz Analyst' if request_type == 'bizanalyst' else 'Demo')
                    office_body = (
                        f'🤖 *New {req_title} Request via Zawadi*\n\n'
                        f'📌 *Request type:* {req_label}\n'
                        f'👤 *Client:* {name}\n'
                        f'🏢 *Company:* {company}\n'
                        f'📞 *Phone:* {phone}\n'
                        f'📆 *Preferred Date:* {display_date}\n'
                        f'🕐 *Preferred Time:* {display_time} (EAT)\n'
                        f'📌 *Session type:* {"🌐 Online" if demo_type == "online" else "🤝 Physical"}\n\n'
                        f'⚠️ *Pending your confirmation* — please review and confirm the slot.\n'
                        f'👉 Admin panel: https://www.optimumprimesolutions.co.ke/admin'
                    )
                    for team_num in TEAM_NUMBERS:
                        _wa_notify(team_num, "team_alert",
                                   [f"{req_title.lower()} request", name, phone,
                                    f"{display_date} at {display_time} EAT, pending confirmation"],
                                   office_body)
                except Exception as e:
                    print(f'Office notify error: {e}')

                # Only send the "working on it" WhatsApp confirmation when this booking
                # came from the website widget — a WhatsApp-originated booking already
                # has this reply text delivered directly as the bot's response.
                try:
                    client_body = (
                        f'Hello {name}! 👋\n\n'
                        f'Thank you for requesting a TallyPrime demo. We have received your preferred slot:\n\n'
                        f'📆 *Date:* {display_date}\n'
                        f'🕐 *Time:* {display_time} (EAT)\n'
                        f'📌 *Type:* {"🌐 Online" if demo_type == "online" else "🤝 Physical"}\n\n'
                        f'Our team is reviewing your request and will confirm the slot shortly. '
                        f'You will receive a confirmation message with all the details once approved.\n\n'
                        f'Questions? Call or WhatsApp us: +254 116 246 074'
                    )
                    _wa_notify(norm_phone, "booking_received",
                               [name,
                                "a consultation" if request_type == "consultation" else "a TallyPrime demo",
                                display_date, display_time],
                               client_body)
                except Exception as e:
                    print(f'Client notify error: {e}')

                # The same acknowledgement by email, whenever we have an
                # address. Not a nicety: WhatsApp is the only channel a bot
                # booking had, and when it stops - a declined card hold, a
                # paused template, a closed 24h window - the person who just
                # booked is told nothing at all and nobody finds out.
                booking_email_sent = False
                if email:
                    try:
                        what = ('a consultation' if request_type == 'consultation'
                                else 'a Biz Analyst session' if request_type == 'bizanalyst'
                                else 'a TallyPrime demo')
                        where = 'Online (Google Meet)' if demo_type == 'online' else 'At our Nairobi office'
                        rows = [('Date', display_date), ('Time', display_time + ' (EAT)'), ('Type', where)]
                        rows_html = ''.join(
                            f'<tr>'
                            f'<td style="padding:8px 0;color:{EMAIL_TEXT_DIM};font-size:14px;width:90px;">{label}</td>'
                            f'<td style="padding:8px 0;color:{EMAIL_TEXT};font-size:15px;font-weight:600;">{html.escape(str(value))}</td>'
                            f'</tr>' for label, value in rows)
                        email_html = (
                            f'<div style="background:{EMAIL_BG};padding:32px;font-family:Arial,Helvetica,sans-serif;">'
                            f'<div style="max-width:560px;margin:0 auto;border:1px solid {EMAIL_BORDER};'
                            f'border-radius:14px;padding:32px;">'
                            f'<h1 style="margin:0 0 8px;color:{EMAIL_TEXT};font-size:22px;">We have your request</h1>'
                            f'<p style="margin:0 0 24px;color:{EMAIL_TEXT_DIM};font-size:15px;line-height:1.6;">'
                            f'Hello {html.escape(name)}, thank you for requesting {what} with Optimum Prime '
                            f'Solutions. You asked for:</p>'
                            f'<table style="width:100%;border-collapse:collapse;">{rows_html}</table>'
                            f'<p style="margin:24px 0 0;color:{EMAIL_TEXT_DIM};font-size:14px;line-height:1.6;">'
                            f'Our team is reviewing the slot and will confirm it shortly. This is not a '
                            f'confirmation yet.<br/>Questions? Call or WhatsApp us on +254 116 246 074.</p>'
                            f'</div></div>')
                        er = _send_email(email, f'We have your request — {display_date} at {display_time}', email_html)
                        booking_email_sent = er.get('success', False)
                    except Exception as e:
                        print(f'Client booking email error: {e}')

                return {
                    'booking': True,
                    'name': name,
                    'phone': phone,
                    'email': email,
                    'emailed': booking_email_sent,
                    'company': company,
                    'demoDate': demo_date,
                    'demoTime': display_time,
                    'demoType': demo_type,
                    'reply': (
                        f"✅ Thank you, {name}! We've received your demo request for {display_date} at {display_time}. "
                        f"Our team will review and confirm your slot shortly — you'll get a WhatsApp message once it's confirmed. "
                        f"Questions? Call us on +254 116 246 074."
                    )
                }

            # ── GENERAL HANDOFF (non-booking) ─────────────────────────────────
            if parsed.get('handoff'):
                name     = parsed.get('name') or from_name or 'Unknown'
                phone    = parsed.get('phone') or from_phone or ''
                interest = parsed.get('interest', 'General enquiry via Zawadi chatbot')

                try:
                    alert = (
                        f'🤖 *Zawadi Handoff — New Lead*\n\n'
                        f'👤 *Name:* {name}\n'
                        f'📞 *Phone:* {phone}\n'
                        f'💼 *Interest:* {interest}\n\n'
                        f'Reply quickly — leads convert best within 5 minutes! ⚡\n'
                        f'👉 Admin panel: https://www.optimumprimesolutions.co.ke/admin'
                    )
                    for team_num in TEAM_NUMBERS:
                        _wa_notify(team_num, "team_alert",
                                   ["lead handoff", name, phone or "not captured", interest],
                                   alert)
                except Exception:
                    pass

                try:
                    lead_record = {
                        'name':      name,
                        'phone':     phone,
                        'message':   interest,
                        'source':    channel_source,
                        'capturedVia': 'Zawadi chatbot handoff',
                        'status':    'New',
                        'createdAt': datetime.now(timezone.utc).isoformat(),
                    }
                    requests.post(FIREBASE_LEADS_URL, json=lead_record, headers=_firebase_auth_headers(), timeout=5)
                except Exception:
                    pass

                return {
                    'handoff': True,
                    'name': name,
                    'phone': phone,
                    'interest': interest,
                    'whatsapp_url': f"https://wa.me/254727209720?text=Hi%2C%20I%27m%20{name.replace(' ', '%20')}%20and%20I%27m%20interested%20in%20{interest.replace(' ', '%20')}",
                    'reply': f"Thanks {name}! 🙌 A member of our team will reach out to you shortly. Feel free to ask anything else in the meantime.",
                }

            # ── PROACTIVE ESCALATION (Zawadi's own call, not user-requested) ────
            if parsed.get('escalate'):
                reason = parsed.get('reason', 'Zawadi was unable to help further')
                name   = from_name or 'Unknown'
                phone  = from_phone or ''

                try:
                    alert = (
                        f'🆘 *Zawadi Escalation*\n\n'
                        f'👤 *From:* {name}\n'
                        f'📞 *Phone:* {phone or "(not captured — website chat)"}\n'
                        f'💬 *Why:* {reason}\n\n'
                        f'Zawadi flagged this conversation as needing a human — please check in.\n'
                        f'👉 Admin panel: https://www.optimumprimesolutions.co.ke/admin'
                    )
                    for team_num in TEAM_NUMBERS:
                        _wa_notify(team_num, "team_alert",
                                   ["escalation", name, phone or "not captured", reason],
                                   alert)
                except Exception:
                    pass

                reply_text = (
                    "I want to make sure you get the best help with this — let me connect you with a member of our team, they'll reach out to you shortly! 🙏"
                    if phone else
                    "I want to make sure you get the best help with this — please reach our team directly on WhatsApp at +254 727 209 720 or call +254 116 246 074."
                )
                return {'escalate': True, 'reason': reason, 'handoff': False, 'reply': reply_text}
    except Exception as e:
        print(f'Zawadi reply JSON parse error: {e}')

    # Chips ride along only on an ordinary reply. The booking, handoff and
    # escalation returns above are the end of a flow, with nothing left to tap.
    return {'reply': reply, 'handoff': False, 'quickReplies': chips}


# ────────────────────────────────────────────────────────────────────────────
# Message template admin
#
# Two templates this service sends constantly have never existed in Meta:
#
#   team_alert       - six call sites (Zawadi booking, handoff and escalation
#                      alerts, newsletter signup, and book_demo's office and
#                      team-assignment messages). Every one of them has only
#                      ever reached the free-text fallback.
#   booking_received - what a customer gets the moment Zawadi takes their
#                      booking. Someone who booked through the website widget
#                      has never messaged us, so no 24h window is open and
#                      Meta drops it in silence. They hear nothing at all.
#
# Defined here rather than clicked into WhatsApp Manager because that form is
# six steps and the Business Suite kept failing partway through, which leaves
# a half-saved draft behind. One POST either succeeds or it does not.
#
# Placeholder order is taken from the call sites, not invented - see
# _wa_notify(..., "team_alert", [kind, name, phone, detail], ...) and
# _wa_notify(..., "booking_received", [name, what, date, time], ...). A
# template whose parameter count does not match is rejected by Meta and falls
# back to free text exactly like a missing one, which is how demo_reminder
# went undelivered without anyone noticing.

TEMPLATE_DEFINITIONS = [
    {
        "name": "team_alert",
        "language": "en",
        "category": "UTILITY",
        "components": [{
            "type": "BODY",
            "text": chr(10).join([
                "🔔 New {{1}} - Optimum Prime Solutions",
                "",
                "Client: {{2}}",
                "Phone: {{3}}",
                "Details: {{4}}",
                "",
                "Open the admin panel to action it.",
            ]),
            "example": {"body_text": [[
                "demo booking",
                "John Mark",
                "+254712345678",
                "Tuesday, 15 September 2026 at 2:00 PM EAT, Online",
            ]]},
        }],
    },
    {
        "name": "booking_received",
        "language": "en",
        "category": "UTILITY",
        "components": [{
            "type": "BODY",
            "text": chr(10).join([
                "Hello {{1}} 👋",
                "",
                "Thank you for requesting {{2}} with Optimum Prime Solutions.",
                "",
                "📆 Date: {{3}}",
                "🕐 Time: {{4}} (EAT)",
                "",
                "Our team is reviewing your request and will confirm the slot shortly.",
                "",
                "Questions? Call or WhatsApp us on +254 116 246 074.",
            ]),
            "example": {"body_text": [[
                "John Mark",
                "a TallyPrime demo",
                "Tuesday, 15 September 2026",
                "2:00 PM",
            ]]},
        }],
    },
]


# What each call site passes. Kept beside the definitions so the check below
# cannot drift from the code it is checking.
TEMPLATE_EXPECTED_PARAMS = {
    "demo_confirmation": 4, "lead_confirmation": 2, "new_lead_alert": 4,
    "demo_reminder": 3, "team_demo_reminder": 4, "delivery_failed_alert": 3,
    "whatsapp_message_alert": 3, "new_review_alert_": 4,
    "webinar_registration_alert": 4, "team_alert": 4, "booking_received": 4,
}


def _template_body_params(tpl: dict) -> int:
    """Highest {{n}} in a template body - the number of parameters it wants."""
    for c in tpl.get("components", []):
        if c.get("type") == "BODY":
            text = c.get("text", "")
            highest = 0
            for i in range(1, 11):
                if ("{{" + str(i) + "}}") in text:
                    highest = i
            return highest
    return 0


@app.route("/admin/templates", methods=["GET", "POST"])
def admin_templates():
    """
    GET  - list every template this code sends, with the placeholder count the
           template actually has beside the count the call site passes, so a
           mismatch is visible without counting variables by eye in the
           WhatsApp Manager UI.
    POST - create any template in TEMPLATE_DEFINITIONS that is not already
           there. Never edits or deletes an existing one, and skips by name,
           so calling it twice is safe.

    Gated on TEMPLATE_ADMIN_KEY via the X-Admin-Key header. With the env var
    unset it refuses outright rather than defaulting to open.
    """
    if not TEMPLATE_ADMIN_KEY:
        return jsonify({"error": "TEMPLATE_ADMIN_KEY is not set - endpoint disabled"}), 503
    if not hmac.compare_digest(request.headers.get("X-Admin-Key", ""), TEMPLATE_ADMIN_KEY):
        return jsonify({"error": "bad or missing X-Admin-Key"}), 403
    if not META_WA_TOKEN:
        return jsonify({"error": "META_WA_TOKEN is not set"}), 503

    listing = requests.get(
        "https://graph.facebook.com/v20.0/" + META_WABA_ID + "/message_templates",
        params={"limit": 200, "access_token": META_WA_TOKEN}, timeout=20,
    ).json()
    if "data" not in listing:
        return jsonify({"error": "could not list templates", "meta_response": listing}), 502
    live = {t["name"]: t for t in listing["data"]}

    if request.method == "GET":
        report = []
        for name in sorted(TEMPLATE_EXPECTED_PARAMS):
            want = TEMPLATE_EXPECTED_PARAMS[name]
            tpl = live.get(name)
            if not tpl:
                report.append({"name": name, "status": "MISSING", "code_sends": want})
                continue
            got = _template_body_params(tpl)
            report.append({
                "name": name, "status": tpl.get("status"),
                "category": tpl.get("category"), "language": tpl.get("language"),
                "template_has": got, "code_sends": want,
                "verdict": "ok" if got == want else
                           "MISMATCH - Meta rejects the send and it silently falls back to free text",
            })
        return jsonify({
            "waba_id": META_WABA_ID,
            "checked": report,
            "on_waba_but_unused_by_code": sorted(set(live) - set(TEMPLATE_EXPECTED_PARAMS)),
        })

    results = []
    for definition in TEMPLATE_DEFINITIONS:
        name = definition["name"]
        if name in live:
            results.append({"name": name, "action": "skipped - already exists",
                            "status": live[name].get("status")})
            continue
        r = requests.post(
            "https://graph.facebook.com/v20.0/" + META_WABA_ID + "/message_templates",
            headers={"Authorization": "Bearer " + META_WA_TOKEN,
                     "Content-Type": "application/json"},
            json=definition, timeout=20,
        )
        results.append({"name": name,
                        "action": "created" if r.status_code == 200 else "FAILED",
                        "http_status": r.status_code, "meta_response": r.json()})
    return jsonify({"waba_id": META_WABA_ID, "results": results})

@app.route("/admin/account", methods=["GET"])
def admin_account():
    """
    The account-level gates a message has to clear before template validity
    even matters:

      business_verification_status - unverified caps who you may message
      account_review_status        - a rejected WABA sends nothing
      messaging_limit_tier         - unique recipients per 24h
      quality_rating / status      - a flagged number gets throttled

    Added after every template checked out correct and sends still failed
    with 131042, Business eligibility payment issue - which is a billing
    problem on the account, invisible from the template list entirely.

    Read-only. Gated on TEMPLATE_ADMIN_KEY like the templates route.
    """
    if not TEMPLATE_ADMIN_KEY:
        return jsonify({"error": "TEMPLATE_ADMIN_KEY is not set - endpoint disabled"}), 503
    if not hmac.compare_digest(request.headers.get("X-Admin-Key", ""), TEMPLATE_ADMIN_KEY):
        return jsonify({"error": "bad or missing X-Admin-Key"}), 403
    if not META_WA_TOKEN:
        return jsonify({"error": "META_WA_TOKEN is not set"}), 503

    def graph(path, fields):
        return requests.get(
            "https://graph.facebook.com/v20.0/" + path,
            params={"fields": fields, "access_token": META_WA_TOKEN}, timeout=20,
        ).json()

    waba = graph(META_WABA_ID, 
                 "id,name,currency,timezone_id,account_review_status,business_verification_status,ownership_type,primary_funding_id")
    numbers = graph(META_WABA_ID + "/phone_numbers",
                    "display_phone_number,verified_name,status,quality_rating,messaging_limit_tier,code_verification_status,throughput")
    configured = graph(META_WA_PHONE_ID,
                       "display_phone_number,verified_name,status,quality_rating,messaging_limit_tier")

    notes = []
    # An errored Graph call is not a finding. Reading a missing field off an
    # error response and reporting it as "no payment method attached" is how
    # a permissions problem gets written up as a billing problem.
    if "error" in waba:
        notes.append(
            "Could not read the WABA itself: " + str(waba["error"].get("message", "")) +
            " This token can send messages but cannot read account-level fields, so payment method, business verification and account review status CANNOT be checked from here - they have to be looked at in Business Manager. Nothing below is evidence either way about billing.")
    else:
        if not waba.get("primary_funding_id"):
            notes.append(
                "No primary_funding_id on the WABA - no payment method is attached. Business-initiated (template) sends fail with 131042.")
        if waba.get("business_verification_status") not in (None, "verified"):
            notes.append(
                "Business is " + str(waba.get("business_verification_status")) +
                " - until verified, Meta caps which and how many recipients you may message.")
        if waba.get("account_review_status") not in (None, "APPROVED"):
            notes.append("WABA account_review_status is " + str(waba.get("account_review_status")))

    for n in numbers.get("data", []) if isinstance(numbers, dict) else []:
        if n.get("quality_rating") not in (None, "GREEN"):
            notes.append(str(n.get("display_phone_number")) + " quality is " +
                         str(n.get("quality_rating")) + " - throttled or at risk.")
        if n.get("status") != "CONNECTED":
            notes.append(str(n.get("display_phone_number")) + " status is " + str(n.get("status")))
    return jsonify({
        "waba": waba,
        "phone_numbers_on_waba": numbers.get("data", numbers),
        "configured_sending_number": configured,
        "what_this_means": notes or ["No account-level blocker found in these fields."],
    })

@app.route("/admin/deliveries", methods=["GET"])
def admin_deliveries():
    """
    What Meta actually did with recent sends, newest last.

    The send side reports success off an HTTP 200, which is why every
    diagnosis so far has had to be inferred. This is the verdict itself,
    with the error code attached.
    """
    if not TEMPLATE_ADMIN_KEY:
        return jsonify({"error": "TEMPLATE_ADMIN_KEY is not set - endpoint disabled"}), 503
    if not hmac.compare_digest(request.headers.get("X-Admin-Key", ""), TEMPLATE_ADMIN_KEY):
        return jsonify({"error": "bad or missing X-Admin-Key"}), 403
    entries = list(WA_DELIVERY_LOG)
    return jsonify({
        "count": len(entries),
        "note": "Empty means the status webhook has delivered nothing since the last restart - either no sends, or the webhook is not reaching this service.",
        "deliveries": entries,
    })

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "Optimum Prime Lead Notifier"})


@app.route("/chat", methods=["POST"])
def chat():
    """
    Zawadi AI chat endpoint.
    Expects: { "messages": [{"role": "user"|"assistant", "content": "..."}] }
    Returns: { "reply": "...", "handoff": false } or { "handoff": true, "name": "...", "phone": "...", "interest": "..." }
    """
    data = request.get_json(force=True, silent=True) or {}
    messages = data.get("messages", [])
    if not messages:
        return jsonify({"error": "No messages provided"}), 400

    reply = get_zawadi_reply(messages)
    return jsonify(process_zawadi_reply(reply))


@app.route("/webhook/meta-status", methods=["GET", "POST"])
def meta_status_webhook():
    """
    Meta WhatsApp Cloud API webhook.
    GET  — used by Meta to verify the webhook endpoint during setup.
    POST — receives delivery status updates and incoming message events.
    Sends a WhatsApp alert to the team if a client message fails to deliver.
    """
    # ── Webhook verification (GET) ───────────────────────────────────────────
    if request.method == "GET":
        verify_token = os.environ.get("META_WA_VERIFY_TOKEN", "optimum_prime_verify")
        mode      = request.args.get("hub.mode")
        token     = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        if mode == "subscribe" and token == verify_token:
            return challenge, 200
        return "Forbidden", 403

    # ── Status / message events (POST) ────────────────────────────────────────
    data = request.get_json(force=True, silent=True) or {}
    print(f"[Meta webhook] Received payload: {data}")
    try:
        for entry in data.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                # Delivery status updates
                for status_obj in value.get("statuses", []):
                    status    = status_obj.get("status", "")
                    to_number = status_obj.get("recipient_id", "")
                    msg_id    = status_obj.get("id", "")
                    # Keep every verdict, not just the failures that get alerted
                    # on: "it says sent but nothing arrived" is only answerable
                    # if the successes were kept too.
                    err0 = (status_obj.get("errors") or [{}])[0]
                    WA_DELIVERY_LOG.append({
                        "at": datetime.now(timezone.utc).isoformat(),
                        "to": "+" + to_number,
                        "status": status,
                        "message_id": msg_id,
                        "error_code": err0.get("code"),
                        "error_title": err0.get("title"),
                        "error_message": err0.get("message"),
                        "error_details": (err0.get("error_data") or {}).get("details"),
                    })
                    is_team_recipient = to_number.lstrip("+") in {n.lstrip("+") for n in TEAM_NUMBERS}
                    if status in {"failed", "undelivered"}:
                        errors = status_obj.get("errors", [{}])
                        err_msg = errors[0].get("message", "Unknown error") if errors else "Unknown error"
                        # Always log. Meta accepts a send with HTTP 200 and only fails it
                        # here, asynchronously, so without this line a failed message
                        # leaves no trace anywhere.
                        print(f"[Meta WA] Delivery {status} to +{to_number} ({msg_id}): {err_msg}")
                        # Only ALERT when the failed message went to a customer. Alerting
                        # the team about a failed team alert is a feedback loop: that alert
                        # can fail too, alerting again, forever.
                        if not is_team_recipient:
                            alert_body = (
                                f"⚠️ *WhatsApp Delivery Failed*\n\n"
                                f"📵 *Status:* {status.upper()}\n"
                                f"📞 *To:* +{to_number}\n"
                                f"🔑 *Message ID:* {msg_id}\n"
                                f"💬 *Error:* {err_msg}\n\n"
                                f"Check the admin panel for details."
                            )
                            for team_num in TEAM_NUMBERS:
                                # delivery_failed_alert: Status {{1}} · To {{2}} · Error {{3}} —
                                # a purpose-built template that already existed for this, unlike
                                # the generic "team_alert" this used to call (which was never
                                # created in Meta and only ever reached the free-text fallback).
                                _wa_notify(team_num, "delivery_failed_alert",
                                           [status.upper(), f"+{to_number}", err_msg],
                                           alert_body)

                # Incoming customer messages — handled by Zawadi, the AI assistant
                contacts = value.get("contacts", [])
                contact_name = contacts[0].get("profile", {}).get("name", "") if contacts else ""
                for msg in value.get("messages", []):
                    from_number = msg.get("from", "")
                    msg_id      = msg.get("id", "")
                    msg_type    = msg.get("type", "text")
                    if not from_number:
                        continue
                    text = msg.get("text", {}).get("body", "") if msg_type == "text" else f"[{msg_type} message]"

                    # Fetch conversation state BEFORE logging this message, so history
                    # doesn't double up when we build it for Gemini below.
                    try:
                        existing_convo = requests.get(f"{FIREBASE_WA_CONVOS_BASE}/{from_number}.json", headers=_firebase_auth_headers(), timeout=5).json() or {}
                    except Exception:
                        existing_convo = {}
                    existing_meta = existing_convo.get("meta") or {}
                    is_first_contact = not existing_meta.get("everContacted")
                    bot_paused = bool(existing_meta.get("botPaused"))

                    _log_wa_message(from_number, "in", text, name=contact_name, message_id=msg_id, force=True)

                    # Alert the team once per new conversation — Zawadi handles the
                    # back-and-forth from here, so we don't spam an alert per message.
                    if is_first_contact:
                        alert = (
                            f"💬 *New WhatsApp Conversation*\n\n"
                            f"👤 *From:* {contact_name or 'Unknown'}\n"
                            f"📞 *Phone:* +{from_number}\n\n"
                            f"Zawadi (our AI assistant) is replying. Check the admin panel's "
                            f"WhatsApp tab anytime to see the conversation or jump in yourself."
                        )
                        for team_num in TEAM_NUMBERS:
                            # whatsapp_message_alert: From {{1}} · Phone {{2}} · Message {{3}}
                            _wa_notify(team_num, "whatsapp_message_alert",
                                       [contact_name or "Unknown", f"+{from_number}",
                                        "Zawadi is replying - open the WhatsApp tab to take over"],
                                       alert)
                        try:
                            requests.patch(f"{FIREBASE_WA_CONVOS_BASE}/{from_number}/meta.json", json={"everContacted": True}, headers=_firebase_auth_headers(), timeout=5)
                        except Exception:
                            pass

                    # Non-text messages (images, audio, documents) — Zawadi can't read
                    # these, so alert the team directly rather than silently skipping.
                    if msg_type != "text":
                        alert = (
                            f"📎 *WhatsApp {msg_type.title()} Received*\n\n"
                            f"👤 *From:* {contact_name or 'Unknown'}\n"
                            f"📞 *Phone:* +{from_number}\n\n"
                            f"Zawadi can't read {msg_type} messages — please review it directly "
                            f"in the admin panel's WhatsApp tab or on WhatsApp."
                        )
                        for team_num in TEAM_NUMBERS:
                            # Same template as the first-contact alert above — this is also
                            # fundamentally "a WhatsApp message arrived", just one Zawadi can't read.
                            _wa_notify(team_num, "whatsapp_message_alert",
                                       [contact_name or "Unknown", f"+{from_number}",
                                        f"Sent a {msg_type} - Zawadi can't read it, check WhatsApp directly"],
                                       alert)
                        continue

                    if bot_paused:
                        continue

                    stored_messages = existing_convo.get("messages") or {}
                    history = sorted(stored_messages.values(), key=lambda m: m.get("timestamp", ""))
                    gemini_messages = [
                        {"role": "user" if m.get("direction") == "in" else "assistant", "content": m.get("text", "")}
                        for m in history
                    ]
                    gemini_messages.append({"role": "user", "content": text})

                    try:
                        zawadi_reply = get_zawadi_reply(gemini_messages, contact_name=contact_name)
                        result = process_zawadi_reply(zawadi_reply, from_phone=f"+{from_number}", from_name=contact_name)
                        reply_text = result.get("reply") or zawadi_reply
                        _wa_send(from_number, reply_text, name=contact_name, force_log=True)

                        # Booking/handoff/escalation alert the team, but Zawadi keeps
                        # replying (its own message tells the customer to "ask anything
                        # else in the meantime") until a team member actually takes over
                        # — either by sending a manual reply or toggling the admin panel
                        # switch, both of which set botPaused separately.
                    except Exception as e:
                        print(f"[Zawadi WhatsApp] Error generating/sending reply: {e}")
    except Exception as e:
        print(f"[Meta webhook] Error processing event: {e}")

    return "", 200


@app.route("/whatsapp/reply", methods=["POST"])
def whatsapp_reply():
    """
    Send a manual WhatsApp reply from the admin panel's WhatsApp tab.
    Pauses Zawadi for this number, since a human is now handling the conversation.
    """
    data = request.get_json(force=True, silent=True) or {}
    phone   = (data.get("phone") or "").strip()
    message = (data.get("message") or "").strip()
    if not phone or not message:
        return jsonify({"success": False, "error": "phone and message are required"}), 400

    norm_phone = normalize_phone(phone)
    result = _wa_send(norm_phone, message, force_log=True)
    try:
        requests.patch(f"{FIREBASE_WA_CONVOS_BASE}/{norm_phone.lstrip('+')}/meta.json", json={"botPaused": True}, headers=_firebase_auth_headers(), timeout=5)
    except Exception:
        pass
    return jsonify(result)


@app.route("/webhook/resend-inbound", methods=["POST"])
def resend_inbound_webhook():
    """
    Resend inbound-email webhook (event: email.received).
    Fetches the full message, runs it through Zawadi for a contextual reply,
    and emails the reply back to the sender.
    """
    raw_body = request.get_data()
    if not _verify_resend_webhook(raw_body, dict(request.headers)):
        return "Invalid signature", 401

    event = request.get_json(force=True, silent=True) or {}
    if event.get("type") != "email.received":
        return "", 200

    data      = event.get("data", {})
    email_id  = data.get("email_id", "")
    from_addr = data.get("from", "")
    subject   = data.get("subject") or "(no subject)"
    if not email_id or not from_addr:
        return "", 200

    # Never auto-reply to automated senders or our own address — avoids reply loops.
    from_lower = from_addr.lower()
    if any(tag in from_lower for tag in ("no-reply", "noreply", "mailer-daemon", "postmaster")):
        print(f"[Resend inbound] Skipping auto-reply to automated sender {from_addr}")
        return "", 200

    try:
        full_email = _resend_get_received_email(email_id)
    except Exception as e:
        print(f"[Resend inbound] Failed to fetch email {email_id}: {e}")
        return "", 200

    body_text = full_email.get("text") or ""
    if not body_text:
        body_text = re.sub("<[^<]+?>", " ", full_email.get("html") or "").strip()
    if not body_text:
        return "", 200

    try:
        zawadi_reply = get_zawadi_reply([{"role": "user", "content": f"Subject: {subject}\n\n{body_text}"}])
        result = process_zawadi_reply(zawadi_reply)
        reply_text = result.get("reply") or zawadi_reply

        reply_html = f"""
        <div style="font-family: -apple-system, Segoe UI, Roboto, sans-serif; max-width: 480px; margin: 0 auto; color: #1A1A2E;">
          <p style="font-size: 15px; line-height: 1.6; white-space: pre-wrap;">{html.escape(reply_text)}</p>
          <p style="font-size: 13px; color: #888; margin-top: 32px;">
            Optimum Prime Solutions &middot; Ruiru, Kenya &middot; +254 116 246 074
          </p>
        </div>
        """
        subject_reply = subject if subject.lower().startswith("re:") else f"Re: {subject}"
        _send_email(from_addr, subject_reply, reply_html)
    except Exception as e:
        print(f"[Resend inbound] Error generating/sending reply: {e}")

    return "", 200


@app.route("/new-lead", methods=["POST"])
def new_lead():
    data = request.get_json(force=True, silent=True) or {}
    team_results = notify_team(data)
    lead_result  = reply_to_lead(data)
    return jsonify({
        "team_notified": sum(1 for r in team_results if r.get("success")),
        "lead_replied":  lead_result.get("success", False),
        "details": {"team": team_results, "lead": lead_result}
    })

@app.route("/new-review", methods=["POST"])
def new_review():
    """Notify team on WhatsApp when a new website review is submitted."""
    data = request.get_json(force=True, silent=True) or {}
    name    = data.get("name", "Anonymous")
    company = data.get("company", "")
    role    = data.get("role", "")
    rating  = data.get("rating", 5)
    text    = data.get("text", "")

    stars = "⭐" * int(rating)
    company_line = f"\n🏢 *Company:* {company}" if company else ""
    role_line    = f"\n💼 *Role:* {role}" if role else ""

    body = (
        f"⭐ *New Website Review — Optimum Prime Solutions*\n\n"
        f"👤 *Name:* {name}"
        f"{company_line}"
        f"{role_line}\n"
        f"🌟 *Rating:* {stars} ({rating}/5)\n\n"
        f"💬 *Review:*\n{text}\n\n"
        f"👉 Approve or reject at: https://www.optimumprimesolutions.co.ke/admin"
    )

    results = []
    for to in TEAM_NUMBERS:
        # new_review_alert_: Name {{1}} · Company {{2}} · Rating {{3}} · Review text {{4}} —
        # a template already built for exactly this, unlike the generic "team_alert" this
        # used to call, which was never created and only ever reached the free-text fallback.
        r = _wa_notify(to, "new_review_alert_",
                       [name, company or "Not provided", str(rating), text or "No comment left"],
                       body)
        results.append({"to": to, "message_id": r.get("message_id", ""), "success": r["success"], "error": r.get("error", "")})

    return jsonify({
        "notified": sum(1 for r in results if r.get("success")),
        "details": results
    })



@app.route("/newsletter-subscribe", methods=["POST"])
def newsletter_subscribe():
    """Save newsletter subscriber, email them a welcome message, and notify team on WhatsApp."""
    data = request.get_json(force=True, silent=True) or {}
    email = data.get("email", "").strip()
    name  = (data.get("name") or "").strip()

    if not email:
        return jsonify({"error": "Email is required"}), 400

    # ── Save to Firebase newsletter collection ────────────────────────────────
    try:
        subscriber_record = {
            "email": email,
            "subscribedAt": datetime.now(timezone.utc).isoformat(),
            "status": "active",
        }
        if name:
            subscriber_record["name"] = name
        requests.post(FIREBASE_NEWSLETTER_URL, json=subscriber_record, headers=_firebase_auth_headers(), timeout=5)
    except Exception as e:
        print(f"Firebase newsletter save error: {e}")

    # ── Send congratulatory email to the subscriber ───────────────────────────
    greeting_name = _first_name({"email": email, "name": name})
    headline = f"You're on the list, {html.escape(greeting_name)}! 🎉" if greeting_name else "You're on the list! 🎉"
    email_html = f"""
    <div style="font-family: -apple-system, Segoe UI, Roboto, sans-serif; max-width: 480px; margin: 0 auto; background:{EMAIL_BG}; color:{EMAIL_TEXT}; padding: 32px 28px; border-radius: 12px;">
      <h1 style="color: {EMAIL_ACCENT}; font-size: 22px;">{headline}</h1>
      <p style="font-size: 15px; line-height: 1.6;">
        Thanks for subscribing to Optimum Prime Solutions updates. You'll get TallyPrime tips,
        cloud hosting guides, and EOS&reg; business insights straight to your inbox.
      </p>
      <p style="font-size: 15px; line-height: 1.6;">
        In the meantime, feel free to explore
        <a href="https://www.optimumprimesolutions.co.ke" style="color: {EMAIL_ACCENT};">our site</a>
        or <a href="https://www.optimumprimesolutions.co.ke/contact#demo-form" style="color: {EMAIL_ACCENT};">book a free demo</a>.
      </p>
      <p style="font-size: 13px; color: {EMAIL_TEXT_DIM}; margin-top: 32px;">
        Optimum Prime Solutions &middot; Ruiru, Kenya &middot; +254 116 246 074
      </p>
      {_email_footer(email)}
    </div>
    """
    email_result = _send_email(email, "You're subscribed — Optimum Prime Solutions", email_html)

    # ── Notify team on WhatsApp ──────────────────────────────────────────────
    try:
        body = (
            f"📧 *New Newsletter Subscriber — Optimum Prime Solutions*\n\n"
            + (f"👤 *Name:* {name}\n" if name else "")
            + f"📨 *Email:* {email}\n"
            f"⏰ *Subscribed:* {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC\n\n"
            f"👉 Manage subscribers at: https://www.optimumprimesolutions.co.ke/admin"
        )
        results = []
        for to in TEAM_NUMBERS:
            r = _wa_notify(to, "team_alert",
                           ["newsletter signup", name or "Not provided", email,
                            "Subscribed through the website"],
                           body)
            results.append({"to": to, "message_id": r.get("message_id", ""), "success": r["success"], "error": r.get("error", "")})
    except Exception as e:
        print(f"Newsletter notification error: {e}")
        results = []

    return jsonify({
        "success": True,
        "email": email,
        "email_sent": email_result.get("success", False),
        "email_error": email_result.get("error", ""),
        "notified": sum(1 for r in results if r.get("success")),
        "details": results
    })


@app.route("/unsubscribe", methods=["GET"])
def unsubscribe():
    """
    One-click unsubscribe link target — every subscriber-facing email footer
    points here via _email_footer(). No login required; the token proves the
    request matches the address it claims (see _unsub_token).
    """
    email = (request.args.get("email") or "").strip().lower()
    token = (request.args.get("token") or "").strip()

    if not email or not token or not UNSUB_SECRET or not hmac.compare_digest(token, _unsub_token(email)):
        return "This unsubscribe link is invalid or has expired.", 400

    subscribers = fetch_firebase(FIREBASE_NEWSLETTER_URL)
    updated = 0
    for key, r in subscribers.items():
        if isinstance(r, dict) and r.get("email", "").strip().lower() == email:
            try:
                requests.patch(f"{FIREBASE_NEWSLETTER_BASE}/{key}.json", json={"status": "unsubscribed"}, headers=_firebase_auth_headers(), timeout=5)
                updated += 1
            except Exception as e:
                print(f"[Unsubscribe] Firebase patch error: {e}")

    return f"""
    <div style="font-family: -apple-system, Segoe UI, Roboto, sans-serif; max-width: 420px; margin: 80px auto; text-align: center; color: #1A1A2E;">
      <h2 style="color: #C0392B;">You're unsubscribed</h2>
      <p style="font-size: 15px; color: #555;">
        {html.escape(email)} won't receive any more emails from Optimum Prime Solutions.
      </p>
    </div>
    """, 200


def _active_subscribers() -> list:
    """De-duplicated {email, name} dicts for every subscriber whose status
    isn't 'unsubscribed' — `name` may be '' for subscribers who signed up
    before the name field existed; _first_name() handles that case."""
    subscribers = fetch_firebase(FIREBASE_NEWSLETTER_URL)
    seen = set()
    result = []
    for r in subscribers.values():
        if not isinstance(r, dict) or r.get("status", "active") != "active":
            continue
        addr = (r.get("email") or "").strip()
        key = addr.lower()
        if addr and key not in seen:
            seen.add(key)
            result.append({"email": addr, "name": (r.get("name") or "").strip()})
    return result


def _broadcast_to_subscribers(subject: str, build_html) -> dict:
    """
    Send `subject` to every active subscriber. `build_html(subscriber) -> str`
    receives the {email, name} dict so it can personalize the greeting and
    still build each recipient's own unsubscribe link. Shared by
    /notify-subscribers and /broadcast. Batches in groups of 100 (Resend's
    per-call limit); one bad address only fails its own chunk, not the
    whole send.
    """
    active = _active_subscribers()
    if not active:
        return {"success": True, "sent": 0, "total_subscribers": 0}

    sent = 0
    errors = []
    for i in range(0, len(active), 100):
        chunk = active[i:i + 100]
        batch_payload = [
            {"from": RESEND_FROM, "to": [s["email"]], "subject": subject, "html": build_html(s)}
            for s in chunk
        ]
        result = _send_email_batch(batch_payload)
        if result.get("success"):
            sent += len(chunk)
        else:
            errors.append(result.get("error", "unknown error"))

    return {
        "success": len(errors) == 0,
        "sent": sent,
        "total_subscribers": len(active),
        "errors": errors,
    }


def _notify_subscribers_of_post(title: str, excerpt: str, slug: str) -> dict:
    """
    Shared by the manual /notify-subscribers route and the scheduled-posts
    background check — emails every active subscriber about a blog post.
    """
    post_url = f"https://www.optimumprimesolutions.co.ke/blog/{slug}"

    def build_html(subscriber):
        name = _first_name(subscriber)
        greeting = f'<p style="font-size:14px;color:{EMAIL_TEXT_DIM};margin:0 0 6px;">Hi {html.escape(name)},</p>' if name else ""
        return f"""
        <div style="font-family: -apple-system, Segoe UI, Roboto, sans-serif; max-width: 480px; margin: 0 auto; background:{EMAIL_BG}; color:{EMAIL_TEXT}; padding: 32px 28px; border-radius: 12px;">
          {greeting}
          <h1 style="color: {EMAIL_ACCENT}; font-size: 20px; margin: 0 0 12px;">{html.escape(title)}</h1>
          <p style="font-size: 15px; line-height: 1.6;">{html.escape(excerpt)}</p>
          <p style="margin: 24px 0;">
            <a href="{post_url}" style="background:{EMAIL_ACCENT};color:#fff;padding:10px 20px;border-radius:6px;text-decoration:none;font-size:14px;">Read the post</a>
          </p>
          {_email_footer(subscriber["email"])}
        </div>
        """

    return _broadcast_to_subscribers(f"New post: {title}", build_html)


@app.route("/notify-subscribers", methods=["POST"])
def notify_subscribers():
    """
    Email every active newsletter subscriber about a new blog post.
    Expects: { "title": "...", "excerpt": "...", "slug": "..." }
    Triggered manually via the "Notify Subscribers Now" button in the
    admin Blog editor — never fires automatically on save/edit.
    """
    data    = request.get_json(force=True, silent=True) or {}
    title   = (data.get("title") or "").strip()
    excerpt = (data.get("excerpt") or "").strip()
    slug    = (data.get("slug") or "").strip()
    if not title or not slug:
        return jsonify({"error": "title and slug are required"}), 400

    return jsonify(_notify_subscribers_of_post(title, excerpt, slug))


@app.route("/broadcast", methods=["POST"])
def broadcast():
    """
    Email every active newsletter subscriber a custom one-off message — not
    tied to a blog post. Expects: { "subject": "...", "body": "..." }.
    `body` is plain text; blank-line-separated paragraphs become <p> tags,
    single line breaks within a paragraph become <br>. Always HTML-escaped,
    so subscriber content can never inject markup into the email.
    Triggered manually via the "Send Broadcast" panel in the admin
    Subscribers tab — never fires automatically.
    """
    data    = request.get_json(force=True, silent=True) or {}
    subject = (data.get("subject") or "").strip()
    body    = (data.get("body") or "").strip()
    if not subject or not body:
        return jsonify({"error": "subject and body are required"}), 400

    paragraphs = "".join(
        f'<p style="font-size:15px;line-height:1.6;margin:0 0 14px;">{html.escape(p).replace(chr(10), "<br>")}</p>'
        for p in body.split("\n\n") if p.strip()
    )

    def build_html(subscriber):
        name = _first_name(subscriber)
        greeting = f'<p style="font-size:14px;color:{EMAIL_TEXT_DIM};margin:0 0 6px;">Hi {html.escape(name)},</p>' if name else ""
        return f"""
        <div style="font-family: -apple-system, Segoe UI, Roboto, sans-serif; max-width: 480px; margin: 0 auto; background:{EMAIL_BG}; color:{EMAIL_TEXT}; padding: 32px 28px; border-radius: 12px;">
          {greeting}
          <h1 style="color: {EMAIL_ACCENT}; font-size: 20px; margin: 0 0 14px;">{html.escape(subject)}</h1>
          {paragraphs}
          {_email_footer(subscriber["email"])}
        </div>
        """

    return jsonify(_broadcast_to_subscribers(subject, build_html))


@app.route("/book-demo", methods=["POST"])
def book_demo():
    """
    Internal team demo booking endpoint.
    Notifies: both office numbers, assigned team member(s), and optionally the client.
    """
    data = request.get_json(force=True, silent=True) or {}

    client_name    = data.get("clientName", "Unknown")
    client_phone   = data.get("clientPhone", "")
    client_email   = data.get("clientEmail", "")
    client_company = data.get("clientCompany", "")
    client_industry = data.get("clientIndustry", "")
    demo_date      = data.get("demoDate", "")
    demo_time      = data.get("demoTime", "")
    demo_notes     = data.get("demoNotes", "")
    demo_type      = data.get("demoType", "online").lower()  # "online" or "physical"
    demo_location  = data.get("demoLocation", "")  # physical location address
    team_name      = data.get("teamMemberName", "")
    team_phone     = data.get("teamMemberPhone", "")
    team2_name     = data.get("teamMember2Name", "")
    team2_phone    = data.get("teamMember2Phone", "")
    team3_name     = data.get("teamMember3Name", "")
    team3_phone    = data.get("teamMember3Phone", "")
    source         = data.get("source", "admin_booking")
    notify_client  = data.get("notifyClient", True)
    # Defaults ON, and read as "unless you said not to". WhatsApp is the only
    # channel this used to try, and outside the 24h window that a customer's
    # own message opens, Meta drops it — which is precisely the case for
    # someone who booked through the website widget and has never messaged the
    # business number. Email is the one channel that always arrives, so a
    # caller that says nothing about it gets it.
    notify_email   = data.get("notifyClientEmail", True)

    # Format date and time nicely. `demo_time` stays as stored — it is what the
    # booking record and the calendar link are built from — while `display_time`
    # is what every person in these messages actually reads.
    display_date = format_date_display(demo_date) if demo_date else demo_date
    display_time = format_time_display(demo_time)

    # Generate Meet link only for online demos
    meet_link = ""
    if demo_type == "online" and demo_date and demo_time:
        meet_link = generate_meet_link(client_name, client_company, demo_date, demo_time)

    # ── Office notification ──────────────────────────────────────────────────
    notes_line = f"\n📝 *Notes:* {demo_notes}" if demo_notes else ""
    team2_line = f"\n👥 *2nd team member:* {team2_name} ({team2_phone})" if team2_name else ""
    team3_line = f"\n👥 *3rd team member:* {team3_name} ({team3_phone})" if team3_name else ""
    email_line = f"\n📧 *Client email:* {client_email}" if client_email else ""

    location_line = f"\n📍 *Location:* {demo_location}" if demo_location else ""
    demo_type_label = "🖥️ Online (Google Meet)" if demo_type == "online" else "🤝 Physical"

    office_body = (
        f"📅 *Demo Booked — Optimum Prime Solutions*\n\n"
        f"👤 *Client:* {client_name}\n"
        f"🏢 *Company:* {client_company}\n"
        f"🏭 *Industry:* {client_industry}\n"
        f"📞 *Client phone:* {client_phone}"
        f"{email_line}\n\n"
        f"📆 *Date:* {display_date}\n"
        f"🕐 *Time:* {display_time} (EAT)\n"
        f"📌 *Type:* {demo_type_label}"
        f"{location_line}\n"
        f"👤 *Booked by:* {team_name} ({team_phone})"
        f"{team2_line}"
        f"{team3_line}"
        f"{notes_line}\n"
    )
    if meet_link:
        office_body += f"\n📹 *Meet link:* {meet_link}\n"
    office_body += (
        f"\n👉 *Admin panel:*\n"
        f"https://www.optimumprimesolutions.co.ke/admin"
    )

    results = {"office": [], "team": [], "client": None, "client_email": None}

    # Send to both office numbers
    for to in TEAM_NUMBERS:
        r = _wa_notify(to, "team_alert",
                       ["demo booking", client_name, client_phone or "not provided",
                        f"{display_date} at {display_time} EAT, {demo_type_label}"],
                       office_body)
        results["office"].append({"to": to, "message_id": r.get("message_id", ""), "success": r["success"], "error": r.get("error", "")})

    # ── Team member notification ─────────────────────────────────────────────
    def send_team_notification(name: str, phone: str):
        if not phone:
            return
        norm_phone = phone.strip().replace(" ", "")
        if norm_phone.startswith("0") and len(norm_phone) == 10:
            norm_phone = "+254" + norm_phone[1:]
        elif norm_phone.startswith("254") and not norm_phone.startswith("+"):
            norm_phone = "+" + norm_phone
        elif not norm_phone.startswith("+"):
            norm_phone = "+254" + norm_phone

        team_body = (
            f"📅 *Demo Assignment — Optimum Prime Solutions*\n\n"
            f"Hi {name}! You've been assigned a TallyPrime demo:\n\n"
            f"👤 *Client:* {client_name}\n"
            f"🏢 *Company:* {client_company}\n"
            f"📞 *Client phone:* {client_phone}\n"
            f"📆 *Date:* {display_date}\n"
            f"🕐 *Time:* {display_time} (EAT)\n"
            f"📌 *Type:* {demo_type_label}\n"
        )
        if demo_type == "physical" and demo_location:
            team_body += f"📍 *Location:* {demo_location}\n"
        if meet_link:
            team_body += f"\n📹 *Meet link:* {meet_link}\n"
        if demo_notes:
            team_body += f"\n📝 *Notes:* {demo_notes}\n"
        team_body += "\n_Please confirm with the client 24 hours before the demo._"

        r = _wa_notify(norm_phone, "team_alert",
                       ["demo assignment", client_name, client_phone or "not provided",
                        f"{display_date} at {display_time} EAT, {demo_type_label}"],
                       team_body)
        results["team"].append({"to": norm_phone, "message_id": r.get("message_id", ""), "success": r["success"], "error": r.get("error", "")})

    send_team_notification(team_name, team_phone)
    if team2_name and team2_phone:
        send_team_notification(team2_name, team2_phone)
    if team3_name and team3_phone:
        send_team_notification(team3_name, team3_phone)

    # ── Client notification ──────────────────────────────────────────────────
    if notify_client and client_phone:
        norm_client = client_phone.strip().replace(" ", "")
        if norm_client.startswith("0") and len(norm_client) == 10:
            norm_client = "+254" + norm_client[1:]
        elif norm_client.startswith("254") and not norm_client.startswith("+"):
            norm_client = "+" + norm_client
        elif not norm_client.startswith("+"):
            norm_client = "+254" + norm_client

        # Uses the approved `demo_confirmation` template first — free text to a client
        # who has not messaged us in the last 24 hours is dropped. Unlike the office/team
        # alerts above, this used to call _wa_send_template directly with no fallback, so
        # a paused/unapproved template meant the client silently got nothing at all and
        # no one found out. Routed through _wa_notify now so it falls back to free text
        # (which carries the real Meet link, unlike the fixed template body) the same way
        # every other outbound message here already does.
        if demo_type == "online":
            details = f"Join here: {meet_link}" if meet_link else "Meeting link will be shared shortly"
        else:
            details = f"Our office: {demo_location}" if demo_location else "Location details to follow"

        client_fallback_body = (
            f"Hello {client_name}! 👋\n\n"
            f"Your TallyPrime demo with Optimum Prime Solutions is confirmed:\n\n"
            f"📆 *Date:* {display_date}\n"
            f"🕐 *Time:* {display_time} (EAT)\n"
            f"📌 *{details}*\n\n"
            f"Questions? Call or WhatsApp us: +254 116 246 074"
        )
        r = _wa_notify(norm_client, "demo_confirmation", [client_name, display_date, display_time, details], client_fallback_body, name=client_name)
        results["client"] = {
            "to": norm_client,
            "message_id": r.get("message_id", ""),
            "success": r["success"],
            "error": r.get("error", ""),
            # True when the approved template couldn't be used and this went out
            # as free text, which Meta accepts and then drops unless the client
            # messaged us in the last 24 hours. The admin panel shows this as
            # "may not have reached them" rather than a clean tick.
            "delivery_uncertain": r.get("delivery_uncertain", False),
            "template_error": r.get("template_error", ""),
        }

    # ── Client email confirmation ────────────────────────────────────────────
    # This is the half that was missing: `notifyClientEmail` was read off the
    # request and then never used, so the only confirmation a client could get
    # was the WhatsApp one above — and when that was dropped, the person who
    # booked a demo heard nothing at all about the date and time we'd agreed.
    if notify_client and notify_email and client_email:
        cal_link = build_google_calendar_link(client_name, client_company, demo_date, demo_time)
        if demo_type == "online":
            where_label = "Google Meet"
            where_value = (f'<a href="{meet_link}" style="color:{EMAIL_ACCENT};">{meet_link}</a>'
                           if meet_link else "The meeting link will be sent to you shortly.")
        else:
            where_label = "Location"
            where_value = demo_location or "Location details to follow."

        rows = [
            ("Date", display_date),
            ("Time", f"{display_time} (EAT)"),
            (where_label, where_value),
        ]
        rows_html = "".join(
            f'<tr>'
            f'<td style="padding:8px 0;color:{EMAIL_TEXT_DIM};font-size:14px;width:110px;">{label}</td>'
            f'<td style="padding:8px 0;color:{EMAIL_TEXT};font-size:15px;font-weight:600;">{value}</td>'
            f'</tr>'
            for label, value in rows
        )
        cal_html = (
            f'<p style="margin:24px 0 0;">'
            f'<a href="{cal_link}" style="display:inline-block;background:{EMAIL_ACCENT};color:#ffffff;'
            f'text-decoration:none;padding:12px 22px;border-radius:8px;font-size:14px;font-weight:600;">'
            f'Add to your calendar</a></p>'
        ) if cal_link else ""

        email_html = (
            f'<div style="background:{EMAIL_BG};padding:32px;font-family:Arial,Helvetica,sans-serif;">'
            f'<div style="max-width:560px;margin:0 auto;background:{EMAIL_BG};border:1px solid {EMAIL_BORDER};'
            f'border-radius:14px;padding:32px;">'
            f'<h1 style="margin:0 0 8px;color:{EMAIL_TEXT};font-size:22px;">Your demo is confirmed</h1>'
            f'<p style="margin:0 0 24px;color:{EMAIL_TEXT_DIM};font-size:15px;line-height:1.6;">'
            f'Hello {client_name}, your TallyPrime demo with Optimum Prime Solutions is booked. '
            f'Here are the details:</p>'
            f'<table style="width:100%;border-collapse:collapse;">{rows_html}</table>'
            f'{cal_html}'
            f'<p style="margin:24px 0 0;color:{EMAIL_TEXT_DIM};font-size:14px;line-height:1.6;">'
            f'Need to change the time? Reply to this email or WhatsApp us on +254 116 246 074.</p>'
            f'</div></div>'
        )
        er = _send_email(client_email, f"Your TallyPrime demo — {display_date} at {display_time}", email_html)
        results["client_email"] = {"to": client_email, "success": er["success"], "error": er.get("error", "")}

    # ── Save booking to Firebase ─────────────────────────────────────────────
    try:
        booking_record = {
            "clientName": client_name,
            "clientPhone": client_phone,
            "clientEmail": client_email,
            "clientCompany": client_company,
            "clientIndustry": client_industry,
            "demoDate": demo_date,
            "demoTime": demo_time,
            "demoNotes": demo_notes,
            "teamMember": team_name,
            "teamPhone": team_phone,
            "teamMember2": team2_name,
            "teamPhone2": team2_phone,
            "teamMember3": team3_name,
            "teamPhone3": team3_phone,
            "meetLink": meet_link,
            "bookedAt": datetime.now(timezone.utc).isoformat(),
            "source": source,
            "status": "scheduled",
        }
        firebase_demos_url = f"{FIREBASE_BASE}/booked_demos.json"
        requests.post(firebase_demos_url, json=booking_record, headers=_firebase_auth_headers(), timeout=5)
    except Exception:
        pass

    office_ok = sum(1 for r in results["office"] if r.get("success"))
    team_ok   = sum(1 for r in results["team"] if r.get("success"))

    return jsonify({
        "success": True,
        "office_notified": office_ok,
        "office_total": len(results["office"]),
        "team_notified": team_ok,
        "team_total": len(results["team"]),
        # What the client actually got, kept apart from "did the request work".
        # `success` above is about the request, not the person: the admin panel
        # used to read it as proof the client had been told, and say so on
        # screen, while the confirmation had in fact been dropped.
        "client_notified": results["client"].get("success", False) if results["client"] else False,
        "client_delivery_uncertain": results["client"].get("delivery_uncertain", False) if results["client"] else False,
        "client_error": results["client"].get("error", "") if results["client"] else "",
        "client_email_notified": results["client_email"].get("success", False) if results["client_email"] else False,
        "client_email_error": results["client_email"].get("error", "") if results["client_email"] else "",
        # The frontend lead record only ever had this while the response sat unread —
        # it's what lets the admin panel show/copy the real link and put it in
        # calendar invites instead of "link to follow" forever. See LeadsManager.tsx.
        "meetLink": meet_link,
        "details": results
    })


@app.route("/export-leads", methods=["GET"])
def export_leads():
    """Download all demo requests as a CSV file."""
    csv_content, count = build_leads_csv()
    return Response(
        csv_content,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=demo_leads.csv",
                 "X-Record-Count": str(count)}
    )

@app.route("/export-webinar", methods=["GET"])
def export_webinar():
    """Download all webinar registrations as a CSV file."""
    csv_content, count = build_webinar_csv()
    return Response(
        csv_content,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=webinar_registrations.csv",
                 "X-Record-Count": str(count)}
    )


# ── 2-Hour Demo Reminders ────────────────────────────────────────────────────

def normalize_phone(phone: str) -> str:
    """Normalise a Kenyan phone number to E.164 format."""
    p = phone.strip().replace(" ", "")
    if p.startswith("0") and len(p) == 10:
        return "+254" + p[1:]
    if p.startswith("254") and not p.startswith("+"):
        return "+" + p
    if not p.startswith("+"):
        return "+254" + p
    return p


@app.route("/send-reminders", methods=["POST", "GET"])
def send_reminders():
    """
    Called every 15 minutes (by the scheduler below or an external cron).
    Scans all leads in Firebase for demos whose scheduledDate + scheduledTime
    falls within the next 2 hours (±15 min window) and sends:
      - Team member(s): a reminder WhatsApp
      - Client: a reminder WhatsApp asking them to confirm attendance
    Stores a `reminderSent` flag on each lead so reminders are sent only once.
    """
    now_eat = datetime.now(timezone(timedelta(hours=3)))
    window_start = now_eat + timedelta(hours=1, minutes=45)   # 1h 45m from now
    window_end   = now_eat + timedelta(hours=2, minutes=15)   # 2h 15m from now

    # Fetch all leads
    leads_data = fetch_firebase(FIREBASE_LEADS_URL)
    # Also check siteData.leads (admin-booked demos)
    site_data  = fetch_firebase(f"{FIREBASE_BASE}/siteData.json")
    site_leads_raw = site_data.get("leads", []) if site_data else []
    if isinstance(site_leads_raw, list):
        site_leads = {str(l.get("id", i)): l for i, l in enumerate(site_leads_raw) if l}
    elif isinstance(site_leads_raw, dict):
        site_leads = site_leads_raw
    else:
        site_leads = {}

    # Merge both sources
    all_leads = {**leads_data, **site_leads}

    sent_count = 0
    skipped_count = 0
    results = []

    for lead_id, lead in all_leads.items():
        if not isinstance(lead, dict):
            continue

        status         = lead.get("status", "")
        scheduled_date = lead.get("scheduledDate") or lead.get("demoDate", "")
        scheduled_time = lead.get("scheduledTime") or lead.get("demoTime", "")
        meet_sent      = lead.get("meetSent", False)
        reminder_sent  = lead.get("reminderSent", False)

        # Only remind for confirmed scheduled demos that haven't been reminded yet.
        # The pipeline stage was renamed from "Demo Scheduled" to "Schedule a Demo"
        # (see LeadsManager.tsx's one-time migration) — this filter still checked the
        # old string, so no lead has ever matched and this reminder has never fired.
        if status != "Schedule a Demo" or not scheduled_date or not scheduled_time:
            continue
        if not meet_sent:
            continue
        if reminder_sent:
            skipped_count += 1
            continue

        # Parse the demo datetime in EAT
        try:
            # scheduled_time may be "10:00" (24h) or "10:00 AM" (12h)
            time_str = scheduled_time.strip()
            if "AM" in time_str.upper() or "PM" in time_str.upper():
                dt_naive = datetime.strptime(f"{scheduled_date} {time_str}", "%Y-%m-%d %I:%M %p")
            else:
                dt_naive = datetime.strptime(f"{scheduled_date} {time_str}", "%Y-%m-%d %H:%M")
            eat = timezone(timedelta(hours=3))
            demo_dt = dt_naive.replace(tzinfo=eat)
        except Exception:
            continue

        # Check if demo falls in the 2-hour reminder window
        if not (window_start <= demo_dt <= window_end):
            continue

        client_name    = lead.get("name", "Client")
        client_company = lead.get("company", "")
        client_phone   = lead.get("phone", "")
        team_name      = lead.get("teamMemberName", "")
        team_phone     = lead.get("teamMemberPhone", "")
        demo_type      = lead.get("demoType", "online")
        demo_location  = lead.get("demoLocation", "")
        meet_link      = lead.get("meetLink", "")
        display_date   = format_date_display(scheduled_date)
        demo_type_label = "💻 Online" if demo_type == "online" else "📍 Physical"

        reminder_results = {"lead_id": lead_id, "client": client_name, "team": [], "client_msg": None}

        # ── Team member reminder ──────────────────────────────────────────────
        def send_team_reminder(name: str, phone: str):
            if not name or not phone:
                return
            norm = normalize_phone(phone)
            body = (
                f"🔔 *Demo Reminder — 2 Hours Away*\n\n"
                f"Hi {name}! Your TallyPrime demo is coming up in 2 hours:\n\n"
                f"👤 *Client:* {client_name}\n"
                f"🏢 *Company:* {client_company}\n"
                f"📆 *Date:* {display_date}\n"
                f"🕐 *Time:* {scheduled_time} EAT\n"
                f"📌 *Type:* {demo_type_label}\n"
            )
            if demo_type == "physical" and demo_location:
                body += f"📍 *Location:* {demo_location}\n"
            if meet_link:
                body += f"\n📹 *Meet link:* {meet_link}\n"
            body += (
                f"\n✅ Please confirm the client is ready and join on time.\n"
                f"👉 *Admin panel:* https://www.optimumprimesolutions.co.ke/admin"
            )
            # team_demo_reminder: Client {{1}} · Company {{2}} · Time {{3}} · Details {{4}} —
            # a template already built for exactly this 2-hour reminder, unlike the generic
            # "team_alert" this used to call, which was never created in Meta.
            details = meet_link if (demo_type == "online" and meet_link) else (demo_location or "On-site")
            r = _wa_notify(norm, "team_demo_reminder",
                           [client_name, client_company or "Not provided", scheduled_time, details],
                           body)
            reminder_results["team"].append({"to": norm, "message_id": r.get("message_id", ""), "success": r["success"], "error": r.get("error", "")})

        send_team_reminder(team_name, team_phone)
        # Extra team members
        for extra in lead.get("extraTeam", []):
            if isinstance(extra, dict):
                send_team_reminder(extra.get("name", ""), extra.get("phone", ""))

        # ── Client reminder ───────────────────────────────────────────────────
        if client_phone:
            norm_client = normalize_phone(client_phone)
            client_body = (
                f"Hello {client_name}! 👋\n\n"
                f"This is a reminder that your TallyPrime demo with Optimum Prime Solutions "
                f"is in *2 hours*:\n\n"
                f"📆 *Date:* {display_date}\n"
                f"🕐 *Time:* {scheduled_time} EAT\n"
            )
            if demo_type == "online":
                if meet_link:
                    client_body += f"\n📹 *Your Google Meet link:*\n{meet_link}\n"
                client_body += "\n_Please join a few minutes early to test your connection._\n"
            else:
                if demo_location:
                    client_body += f"\n📍 *Location:* {demo_location}\n"
                client_body += "\n🤝 Our team will meet you at the scheduled time.\n"
            client_body += (
                f"\nReply *CONFIRM* to confirm you\'ll attend, or call us at "
                f"*+254 116 246 074* if you need to reschedule.\n\n"
                f"_Optimum Prime Solutions — TallyPrime · Cloud · EOS®_"
            )
            reminder_detail = (
                f"Your Google Meet link: {meet_link}" if (demo_type == "online" and meet_link)
                else (f"Location: {demo_location}" if demo_location
                      else "Our team will meet you at the scheduled time.")
            )
            # Three parameters, not four. The approved `demo_reminder` template
            # is {{1}} name, {{2}} time, {{3}} details — it carries no date,
            # and rightly so: it says "starts in 2 hours". Sending a fourth was
            # rejected by Meta as a parameter mismatch, which this code treats
            # as "template unusable" and answers with the free-text fallback —
            # dropped for any customer who had not messaged us in the previous
            # 24 hours. Which is all of them: this fires 2 hours before a demo
            # that was booked days earlier. So the reminder has never arrived.
            r = _wa_notify(norm_client, "demo_reminder",
                           [client_name, scheduled_time, reminder_detail],
                           client_body)
            reminder_results["client_msg"] = {"to": norm_client, "message_id": r.get("message_id", ""), "success": r["success"], "error": r.get("error", "")}

        # ── Mark reminderSent in Firebase ─────────────────────────────────────
        try:
            patch_url = f"{FIREBASE_BASE}/leads/{lead_id}.json"
            requests.patch(patch_url, json={"reminderSent": True}, headers=_firebase_auth_headers(), timeout=5)
        except Exception:
            pass

        sent_count += 1
        results.append(reminder_results)

    return jsonify({
        "success": True,
        "checked_at": now_eat.isoformat(),
        "window": {"from": window_start.isoformat(), "to": window_end.isoformat()},
        "reminders_sent": sent_count,
        "already_reminded": skipped_count,
        "details": results,
    })


@app.route("/send-scheduled-posts", methods=["POST", "GET"])
def send_scheduled_posts():
    """
    Called periodically (every ~10 min) by a background thread. Scans
    siteData.blogs for posts with a `notifyAt` timestamp (set via the
    admin Blog editor's "Auto-send to subscribers on" field) that has
    passed and aren't already `notified`, emails each one out via the
    same path as the manual "Notify Subscribers Now" button, then PATCHes
    that post's `notified` flag so it's never sent twice.
    """
    blogs = fetch_firebase(FIREBASE_BLOGS_URL)
    if not isinstance(blogs, list):
        return jsonify({"success": True, "checked_at": datetime.now(timezone.utc).isoformat(), "posts_sent": []})

    now = datetime.now(timezone.utc)
    posts_sent = []

    for i, post in enumerate(blogs):
        if not isinstance(post, dict) or post.get("notified"):
            continue
        notify_at = post.get("notifyAt")
        if not notify_at:
            continue
        try:
            scheduled = datetime.fromisoformat(notify_at.replace("Z", "+00:00"))
            if scheduled.tzinfo is None:
                scheduled = scheduled.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if scheduled > now:
            continue  # not due yet

        title = (post.get("title") or "").strip()
        excerpt = (post.get("excerpt") or "").strip()
        slug = (post.get("slug") or "").strip()
        if not title or not slug:
            print(f"[Scheduled posts] Skipping post at index {i} — missing title or slug")
            continue

        result = _notify_subscribers_of_post(title, excerpt, slug)

        try:
            requests.patch(f"{FIREBASE_BLOGS_BASE}/{i}.json", json={"notified": True}, headers=_firebase_auth_headers(), timeout=5)
        except Exception as e:
            print(f"[Scheduled posts] Failed to mark '{title}' as notified: {e}")

        posts_sent.append({"title": title, "sent": result.get("sent", 0), "total_subscribers": result.get("total_subscribers", 0)})

    return jsonify({
        "success": True,
        "checked_at": now.isoformat(),
        "posts_sent": posts_sent,
    })


# ── Self-scheduling: call /send-reminders and /send-scheduled-posts periodically ──
import threading

def _reminder_loop():
    """Background thread: hits /send-reminders every 15 minutes."""
    import time
    while True:
        time.sleep(15 * 60)  # wait 15 minutes
        try:
            requests.post(f"{SERVICE_URL}/send-reminders", timeout=30)
        except Exception:
            pass

def _scheduled_posts_loop():
    """Background thread: hits /send-scheduled-posts every 10 minutes."""
    import time
    while True:
        time.sleep(10 * 60)  # wait 10 minutes
        try:
            requests.post(f"{SERVICE_URL}/send-scheduled-posts", timeout=30)
        except Exception:
            pass

_reminder_thread = threading.Thread(target=_reminder_loop, daemon=True)
_reminder_thread.start()

_scheduled_posts_thread = threading.Thread(target=_scheduled_posts_loop, daemon=True)
_scheduled_posts_thread.start()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
