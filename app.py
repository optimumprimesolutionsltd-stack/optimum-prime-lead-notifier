#!/usr/bin/env python3
"""
Optimum Prime Solutions — Lead Auto-Reply & Webinar Notification System
"""

import os
import re
import html
import csv
import io
import uuid
import hashlib
import urllib.parse
import requests
from datetime import datetime, timedelta, timezone
from flask import Flask, request, jsonify, Response
from flask_cors import CORS
from openai import OpenAI

# ── Meta WhatsApp Cloud API config ───────────────────────────────────────────
# Set these in Render environment variables:
#   META_WA_TOKEN        — permanent system user access token from Meta Business Manager
#   META_WA_PHONE_ID     — Phone Number ID from WhatsApp Manager (not the phone number itself)
META_WA_TOKEN    = os.environ.get("META_WA_TOKEN", "").strip()
META_WA_PHONE_ID = os.environ.get("META_WA_PHONE_ID", "").strip()
META_WA_API_URL  = f"https://graph.facebook.com/v20.0/{META_WA_PHONE_ID}/messages"

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

FIREBASE_BASE           = "https://optimum-prime-website-default-rtdb.europe-west1.firebasedatabase.app"
FIREBASE_WEBINAR_URL   = f"{FIREBASE_BASE}/webinar_registrants.json"
FIREBASE_LEADS_URL     = f"{FIREBASE_BASE}/leads.json"
FIREBASE_NEWSLETTER_URL = f"{FIREBASE_BASE}/newsletter_subscribers.json"
FIREBASE_WA_CONVOS_BASE = f"{FIREBASE_BASE}/whatsapp_conversations"

# Team numbers (E.164 format, no 'whatsapp:' prefix needed for Meta API)
# Messages are SENT FROM +254727209720 (the registered API number)
# Notifications are DELIVERED TO these numbers
TEAM_NUMBERS = [
    "+254758449475",
    "+254116246074",
]

SERVICE_URL = os.environ.get("SERVICE_URL", "https://optimum-prime-lead-notifier.onrender.com")

app = Flask(__name__)
CORS(app)


def _wa_send(to: str, body: str, name: str = "") -> dict:
    """
    Send a WhatsApp text message via Meta Cloud API.
    `to` should be E.164 format, e.g. '+254712345678'.
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
            _log_wa_message(to_clean, "out", body, name=name, message_id=msg_id)
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
    Required for business-initiated messages once the app is published (outside the
    24h customer-service session window, Meta rejects free-form text).
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
            err = data.get("error", {}).get("message", str(data))
            print(f"[Meta WA] Template send failed to {to}: {err}")
            return {"success": False, "message_id": "", "error": err}
    except Exception as e:
        print(f"[Meta WA] Exception sending template to {to}: {e}")
        return {"success": False, "message_id": "", "error": str(e)}


def _log_wa_message(phone_digits: str, direction: str, text: str, name: str = "", message_id: str = "") -> None:
    """
    Append a message to a customer's WhatsApp conversation thread in Firebase,
    so the admin panel can show chat history. Team-alert numbers are excluded
    so the conversation list only shows real customer threads.
    """
    if phone_digits in {n.lstrip("+") for n in TEAM_NUMBERS}:
        return
    now = datetime.now(timezone.utc).isoformat()
    try:
        requests.post(f"{FIREBASE_WA_CONVOS_BASE}/{phone_digits}/messages.json", json={
            "direction": direction,
            "text": text,
            "timestamp": now,
            "messageId": message_id,
        }, timeout=5)
        meta = {
            "phone": f"+{phone_digits}",
            "lastMessage": text,
            "lastMessageAt": now,
            "lastDirection": direction,
            "unread": direction == "in",
        }
        if name:
            meta["name"] = name
        requests.patch(f"{FIREBASE_WA_CONVOS_BASE}/{phone_digits}/meta.json", json=meta, timeout=5)
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
        # Parse datetime in EAT (UTC+3)
        dt_naive = datetime.strptime(f"{date_str} {start_str}", "%Y-%m-%d %I:%M %p")
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
        resp = requests.get(FIREBASE_WEBINAR_URL, timeout=5)
        data = resp.json()
        return len(data) if data and isinstance(data, dict) else 0
    except Exception:
        return -1

def fetch_firebase(url: str) -> dict:
    try:
        resp = requests.get(url, timeout=8)
        data = resp.json()
        return data if isinstance(data, dict) and "error" not in data else {}
    except Exception:
        return {}


# ── WhatsApp Messaging ────────────────────────────────────────────────────────

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

    if not is_webinar:
        # Uses the approved `new_lead_alert` template (required for business-initiated
        # sends once the app is published — free text would be rejected outside a session)
        for to in TEAM_NUMBERS:
            r = _wa_send_template(to, "new_lead_alert", [name, company, phone, interest])
            results.append({"to": to, "message_id": r.get("message_id", ""), "success": r["success"], "error": r.get("error", "")})
        return results

    # Webinar registrations: `webinar_registration_alert` template not yet approved — free text for now
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
        r = _wa_send(to, body)
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

    # Custom message (e.g. webinar confirmation) takes priority — sent as free text since
    # it's arbitrary per-call content, not covered by a fixed approved template
    custom_msg = lead.get("confirmation_message", "")
    if custom_msg:
        r = _wa_send(phone, custom_msg)
    else:
        # Uses the approved `lead_confirmation` template (required for business-initiated
        # sends once the app is published — free text would be rejected outside a session)
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

Then collect the following details ONE AT A TIME in this order:

1. Full name
2. WhatsApp phone number (Kenyan format, e.g. 0712 345 678)
3. Company name
4. Preferred date (remind them: Mon–Fri 8AM–5PM, Sat 8AM–12PM, no Sundays or public holidays)
5. Preferred time slot (e.g. 10:00 AM, 2:00 PM)
6. Session type: Online (Google Meet) or Physical (at our Nairobi office)

RULES:
- Ask ONE question at a time. Do not ask multiple questions in one message.
- If they give an invalid date (Sunday, public holiday, or past date), politely explain and ask again.
- Kenya public holidays to block: 1 Jan, 1 May, 1 Jun, 10 Oct, 20 Oct, 12 Dec, 25 Dec, 26 Dec, and Easter (Good Friday + Easter Monday).
- If they pick Saturday, remind them slots are 8AM–12PM only.
- Once you have ALL 6 details, confirm them back to the user in a friendly summary and ask them to confirm.
- After they confirm, respond with ONLY this exact JSON (no other text before or after):
  {"booking": true, "name": "<name>", "phone": "<phone>", "company": "<company>", "demoDate": "<YYYY-MM-DD>", "demoTime": "<HH:MM>", "demoType": "<online|physical>", "requestType": "<demo|consultation|bizanalyst>"}
- The demoDate MUST be in YYYY-MM-DD format. The demoTime MUST be in 24-hour HH:MM format (e.g. 10:00, 14:30).
- Set requestType to "consultation" if the user chose EOS® Business Consultation, "bizanalyst" if they chose Biz Analyst Enquiry, otherwise "demo".
- IMPORTANT: The booking is NOT immediately confirmed. Our team reviews and approves the slot. Tell the user: "We've received your request and our team will confirm your slot shortly via WhatsApp."
- Do NOT tell the user the demo is confirmed or give them a Meet link — that comes later from our team.
- If the user declines to provide any detail, offer the website form: www.optimumprimesolutions.co.ke/contact#demo-form

GENERAL HANDOFF (non-booking enquiries):
When the user wants to speak to a person, get a quote, or be called back:
1. Ask for their name and WhatsApp number.
2. Once you have both, respond with ONLY this exact JSON:
   {"handoff": true, "name": "<their name>", "phone": "<their phone>", "interest": "<brief summary>"}
3. Do NOT include any other text before or after the JSON.

If the user declines to provide their number, respond:
"No problem! You can reach us anytime on WhatsApp at +254 116 246 074 or book a demo at www.optimumprimesolutions.co.ke/contact#demo-form"

PROACTIVE ESCALATION (different from the handoff above — this is YOUR call, not the user's request):
Sometimes you should hand off even though the user never asked for a person — for example: they've asked essentially the same question 2+ times without a satisfying answer, they express frustration ("this isn't helping", "you don't understand", "forget it", "never mind"), their question is genuinely outside TallyPrime / Cloud Hosting / EOS® / Biz Analyst and you cannot help, or the conversation is clearly going in circles.
When that happens, respond with ONLY this exact JSON (no other text before or after):
{"escalate": true, "reason": "<brief description, e.g. 'user frustrated after repeated pricing questions'>"}
Do NOT use this for questions you CAN answer — only when you genuinely cannot help further. This is rare; most conversations should not trigger it.

UPCOMING EVENTS (only mention if the user explicitly asks about events, webinars, or training):
- Free webinar: TallyPrime 7.1 — Wednesday 15th July 2026, 3PM–4PM EAT (online)
- Do NOT proactively mention this webinar unless the user asks about events, webinars, or upcoming training.

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

def get_zawadi_reply(messages: list) -> str:
    """
    Call Google Gemini 2.5 Flash with the Zawadi system prompt.

    The system prompt is passed via `system_instruction` (the correct Gemini API
    field) so it is always active regardless of conversation length.

    Conversation history is rebuilt as a strictly alternating user/model sequence
    — the Gemini API rejects histories where two consecutive turns share the same
    role, which was causing Gemini to lose context and re-ask answered questions.
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

    try:
        clean = reply.strip()
        if clean.startswith('```'):
            clean = clean.split('```')[1]
            if clean.startswith('json'):
                clean = clean[4:]
        clean = clean.strip().lstrip('`').rstrip('`').strip()

        if clean.startswith('{') and ('"booking"' in clean or '"handoff"' in clean or '"escalate"' in clean):
            parsed = _json.loads(clean)

            # ── DEMO BOOKING (end-to-end) ─────────────────────────────────────
            if parsed.get('booking'):
                name       = parsed.get('name', 'Unknown')
                phone      = parsed.get('phone', '')
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
                        'company':     company,
                        'demoDate':    demo_date,
                        'demoTime':    demo_time,
                        'demoType':    demo_type,
                        'requestType': request_type,
                        'status':      'New',
                        'source':      'Zawadi Chatbot Booking',
                        'message':     f'Preferred: {display_date} at {display_time} ({demo_type}) — {"Consultation" if request_type == "consultation" else "Demo"}',
                        'createdAt':   datetime.now(timezone.utc).isoformat(),
                    }
                    requests.post(FIREBASE_LEADS_URL, json=lead_record, timeout=5)
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
                        _wa_send(team_num, office_body)
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
                    _wa_send(norm_phone, client_body)
                except Exception as e:
                    print(f'Client notify error: {e}')

                return {
                    'booking': True,
                    'name': name,
                    'phone': phone,
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
                name     = parsed.get('name', 'Unknown')
                phone    = parsed.get('phone', '')
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
                        _wa_send(team_num, alert)
                except Exception:
                    pass

                try:
                    lead_record = {
                        'name':      name,
                        'phone':     phone,
                        'message':   interest,
                        'source':    'Zawadi Chatbot Handoff',
                        'status':    'New',
                        'createdAt': datetime.now(timezone.utc).isoformat(),
                    }
                    requests.post(FIREBASE_LEADS_URL, json=lead_record, timeout=5)
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
                        _wa_send(team_num, alert)
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

    return {'reply': reply, 'handoff': False}


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
                    # Skip alerting on failures where the ORIGINAL failed message was sent
                    # to a team number (i.e. it was one of our own alerts). Otherwise a
                    # failed alert triggers another alert, which can also fail, forever —
                    # an infinite feedback loop.
                    is_team_recipient = to_number.lstrip("+") in {n.lstrip("+") for n in TEAM_NUMBERS}
                    if status in {"failed", "undelivered"} and not is_team_recipient:
                        errors = status_obj.get("errors", [{}])
                        err_msg = errors[0].get("message", "Unknown error") if errors else "Unknown error"
                        alert_body = (
                            f"⚠️ *WhatsApp Delivery Failed*\n\n"
                            f"📵 *Status:* {status.upper()}\n"
                            f"📞 *To:* +{to_number}\n"
                            f"🔑 *Message ID:* {msg_id}\n"
                            f"💬 *Error:* {err_msg}\n\n"
                            f"Check the admin panel for details."
                        )
                        for team_num in TEAM_NUMBERS:
                            _wa_send(team_num, alert_body)

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
                        existing_convo = requests.get(f"{FIREBASE_WA_CONVOS_BASE}/{from_number}.json", timeout=5).json() or {}
                    except Exception:
                        existing_convo = {}
                    existing_meta = existing_convo.get("meta") or {}
                    is_first_contact = not existing_meta.get("everContacted")
                    bot_paused = bool(existing_meta.get("botPaused"))

                    _log_wa_message(from_number, "in", text, name=contact_name, message_id=msg_id)

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
                            _wa_send(team_num, alert)
                        try:
                            requests.patch(f"{FIREBASE_WA_CONVOS_BASE}/{from_number}/meta.json", json={"everContacted": True}, timeout=5)
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
                            _wa_send(team_num, alert)
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
                        zawadi_reply = get_zawadi_reply(gemini_messages)
                        result = process_zawadi_reply(zawadi_reply, from_phone=f"+{from_number}", from_name=contact_name)
                        reply_text = result.get("reply") or zawadi_reply
                        _wa_send(from_number, reply_text, name=contact_name)

                        # A booking, handoff, or escalation means a human takes it from here.
                        if result.get("booking") or result.get("handoff") or result.get("escalate"):
                            requests.patch(f"{FIREBASE_WA_CONVOS_BASE}/{from_number}/meta.json", json={"botPaused": True}, timeout=5)
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
    result = _wa_send(norm_phone, message)
    try:
        requests.patch(f"{FIREBASE_WA_CONVOS_BASE}/{norm_phone.lstrip('+')}/meta.json", json={"botPaused": True}, timeout=5)
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
        r = _wa_send(to, body)
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

    if not email:
        return jsonify({"error": "Email is required"}), 400

    # ── Save to Firebase newsletter collection ────────────────────────────────
    try:
        subscriber_record = {
            "email": email,
            "subscribedAt": datetime.now(timezone.utc).isoformat(),
            "status": "active"
        }
        requests.post(FIREBASE_NEWSLETTER_URL, json=subscriber_record, timeout=5)
    except Exception as e:
        print(f"Firebase newsletter save error: {e}")

    # ── Send congratulatory email to the subscriber ───────────────────────────
    email_html = f"""
    <div style="font-family: -apple-system, Segoe UI, Roboto, sans-serif; max-width: 480px; margin: 0 auto; color: #1A1A2E;">
      <h1 style="color: #C0392B; font-size: 22px;">You're on the list! 🎉</h1>
      <p style="font-size: 15px; line-height: 1.6;">
        Thanks for subscribing to Optimum Prime Solutions updates. You'll get TallyPrime tips,
        cloud hosting guides, and EOS&reg; business insights straight to your inbox.
      </p>
      <p style="font-size: 15px; line-height: 1.6;">
        In the meantime, feel free to explore
        <a href="https://www.optimumprimesolutions.co.ke" style="color: #C0392B;">our site</a>
        or <a href="https://www.optimumprimesolutions.co.ke/contact#demo-form" style="color: #C0392B;">book a free demo</a>.
      </p>
      <p style="font-size: 13px; color: #888; margin-top: 32px;">
        Optimum Prime Solutions &middot; Ruiru, Kenya &middot; +254 116 246 074
      </p>
    </div>
    """
    email_result = _send_email(email, "You're subscribed — Optimum Prime Solutions", email_html)

    # ── Notify team on WhatsApp ──────────────────────────────────────────────
    try:
        body = (
            f"📧 *New Newsletter Subscriber — Optimum Prime Solutions*\n\n"
            f"📨 *Email:* {email}\n"
            f"⏰ *Subscribed:* {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC\n\n"
            f"👉 Manage subscribers at: https://www.optimumprimesolutions.co.ke/admin"
        )
        results = []
        for to in TEAM_NUMBERS:
            r = _wa_send(to, body)
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
    notify_email   = data.get("notifyClientEmail", False)

    # Format date nicely
    display_date = format_date_display(demo_date) if demo_date else demo_date

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
        f"🕐 *Time:* {demo_time} (EAT)\n"
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

    results = {"office": [], "team": [], "client": None}

    # Send to both office numbers
    for to in TEAM_NUMBERS:
        r = _wa_send(to, office_body)
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
            f"🕐 *Time:* {demo_time} (EAT)\n"
            f"📌 *Type:* {demo_type_label}\n"
        )
        if demo_type == "physical" and demo_location:
            team_body += f"📍 *Location:* {demo_location}\n"
        if meet_link:
            team_body += f"\n📹 *Meet link:* {meet_link}\n"
        if demo_notes:
            team_body += f"\n📝 *Notes:* {demo_notes}\n"
        team_body += "\n_Please confirm with the client 24 hours before the demo._"

        r = _wa_send(norm_phone, team_body)
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

        # Uses the approved `demo_confirmation` template (required for business-initiated
        # sends once the app is published — free text would be rejected outside a session).
        # Covers both new bookings and reschedules; the Meet link / Google Calendar link
        # and reschedule-specific wording from the old free-text version are dropped since
        # the approved template body is fixed — client can still get the Meet link by
        # replying, or from the admin panel.
        if demo_type == "online":
            details = f"Join here: {meet_link}" if meet_link else "Meeting link will be shared shortly"
        else:
            details = f"Our office: {demo_location}" if demo_location else "Location details to follow"

        r = _wa_send_template(norm_client, "demo_confirmation", [client_name, display_date, demo_time, details])
        results["client"] = {"to": norm_client, "message_id": r.get("message_id", ""), "success": r["success"], "error": r.get("error", "")}

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
        requests.post(firebase_demos_url, json=booking_record, timeout=5)
    except Exception:
        pass

    office_ok = sum(1 for r in results["office"] if r.get("success"))
    team_ok   = sum(1 for r in results["team"] if r.get("success"))

    return jsonify({
        "success": True,
        "office_notified": office_ok,
        "team_notified": team_ok,
        "client_notified": results["client"].get("success", False) if results["client"] else False,
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

        # Only remind for confirmed scheduled demos that haven't been reminded yet
        if status != "Demo Scheduled" or not scheduled_date or not scheduled_time:
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
            r = _wa_send(norm, body)
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
            r = _wa_send(norm_client, client_body)
            reminder_results["client_msg"] = {"to": norm_client, "message_id": r.get("message_id", ""), "success": r["success"], "error": r.get("error", "")}

        # ── Mark reminderSent in Firebase ─────────────────────────────────────
        try:
            patch_url = f"{FIREBASE_BASE}/leads/{lead_id}.json"
            requests.patch(patch_url, json={"reminderSent": True}, timeout=5)
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


# ── Self-scheduling: call /send-reminders every 15 minutes ────────────────────
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

_reminder_thread = threading.Thread(target=_reminder_loop, daemon=True)
_reminder_thread.start()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
