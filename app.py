#!/usr/bin/env python3
"""
Optimum Prime Solutions — Lead Auto-Reply & Webinar Notification System
"""

import os
import csv
import io
import uuid
import hashlib
import urllib.parse
import requests
from datetime import datetime, timedelta, timezone
from twilio.rest import Client
from flask import Flask, request, jsonify, Response
from flask_cors import CORS
from openai import OpenAI

ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
AUTH_TOKEN  = os.environ.get("TWILIO_AUTH_TOKEN")
FROM_WA     = "whatsapp:+14155238886"

FIREBASE_BASE        = "https://optimum-prime-website-default-rtdb.europe-west1.firebasedatabase.app"
FIREBASE_WEBINAR_URL = f"{FIREBASE_BASE}/webinar_registrants.json"
FIREBASE_LEADS_URL   = f"{FIREBASE_BASE}/leads.json"

TEAM_NUMBERS = [
    "whatsapp:+254758449475",
    "whatsapp:+254116246074",
]

# Public URL of this Render service (used as Twilio status callback)
SERVICE_URL = os.environ.get("SERVICE_URL", "https://optimum-prime-lead-notifier.onrender.com")
STATUS_CALLBACK_URL = f"{SERVICE_URL}/webhook/twilio-status"

# In-memory store: SID -> recipient info (for failure alerts)
_pending_messages: dict = {}

app = Flask(__name__)
CORS(app)

def _client():
    return Client(ACCOUNT_SID, AUTH_TOKEN)


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

    if is_webinar:
        count = get_registration_count()
        count_line = f"📊 *Total registrations so far:* {count}\n" if count != -1 else "📊 *Total registrations:* (unavailable)\n"
        header = "🔔 *New Webinar Registration — Optimum Prime Solutions*"
    else:
        count_line = ""
        header = "🔔 *New Demo Request — Optimum Prime Solutions*"

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

    # Add preferred demo slot — NO Meet link at this stage (sent only when demo is confirmed)
    if not is_webinar and demo_date and demo_time:
        display_date = format_date_display(demo_date)
        body += (
            f"\n📅 *Preferred slot:* {display_date}\n"
            f"🕐 *Time:* {demo_time} (EAT)\n"
        )

    if count_line:
        body += f"\n{count_line}"

    body += (
        f"\n👉 *Manage in admin panel:*\n"
        f"https://www.optimumprimesolutions.co.ke/admin\n"
        f"\n_Reply quickly — leads convert best within 5 minutes!_ ⚡"
    )

    client = _client()
    results = []
    for to in TEAM_NUMBERS:
        try:
            msg = client.messages.create(from_=FROM_WA, to=to, body=body)
            results.append({"to": to, "sid": msg.sid, "success": True})
        except Exception as e:
            results.append({"to": to, "error": str(e), "success": False})
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

    name      = lead.get("name", "there")
    company   = lead.get("company", "")
    demo_date = lead.get("demoDate", "")
    demo_time = lead.get("demoTime", "")

    # Custom message (e.g. webinar confirmation) takes priority
    custom_msg = lead.get("confirmation_message", "")
    if custom_msg:
        body = custom_msg
    else:
        cal_link = build_google_calendar_link(name, company, demo_date, demo_time)

        business_type    = lead.get("businessType", "")
        current_software = lead.get("currentSoftware", "")

        # ── "Working on it" message — no Meet link yet ──────────────────────
        preferred_date_line = ""
        if demo_date:
            preferred_date_line = f"\n📅 *Your preferred date:* {format_date_display(demo_date)}"
            if demo_time:
                preferred_date_line += f"\n🕐 *Preferred time:* {demo_time} (EAT)"

        body = (
            f"Hello {name}! 👋\n\n"
            f"Thank you for your interest in TallyPrime! 🎉\n\n"
            f"We've received your demo request and our team is reviewing it. "
            f"We'll confirm your demo slot and send you all the details shortly.\n"
            f"{preferred_date_line}\n\n"
            f"In the meantime, feel free to explore our website:\n"
            f"🌐 *www.optimumprimesolutions.co.ke*\n\n"
            f"Or reach us directly:\n"
            f"📞 *+254 116 246 074*\n\n"
            f"_Optimum Prime Solutions — TallyPrime · Cloud · EOS® · HubSpot CRM · Biz Analyst_"
        )

    client = _client()
    try:
        msg = client.messages.create(
            from_=FROM_WA,
            to=f"whatsapp:{phone}",
            body=body,
            status_callback=STATUS_CALLBACK_URL,
        )
        # Track pending message for failure alerting
        _pending_messages[msg.sid] = {
            "to": phone,
            "name": name,
            "type": "client_confirmation",
        }
        return {"success": True, "sid": msg.sid, "to": phone}
    except Exception as e:
        return {"success": False, "error": str(e)}


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
- Services: TallyPrime accounting software, Cloud Hosting, EOS® Business Consulting, HubSpot CRM, Biz Analyst
- Phone: +254 116 246 074
- Website: www.optimumprimesolutions.co.ke
- Location: Nairobi, Kenya
- Free webinar: TallyPrime 7.1 — Wednesday 15th July 2026, 3PM–4PM EAT (online)

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

HUBSPOT CRM:
- Customer relationship management platform.
- Helps businesses track leads, manage sales pipelines, and automate follow-ups.
- Integrates with TallyPrime for a complete business management stack.

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

BOOKING MANDATE — DEMO OR CONSULTATION:
You have full authority to collect booking requests on behalf of Optimum Prime Solutions. When a user wants to book, schedule, or says anything like "book", "I want to see it", "show me", "interested", "let's proceed", "consultation", "EOS", first ask:
"Would you like to book a *TallyPrime Demo* (see the software in action) or an *EOS® Business Consultation* (a 90-minute session to explore the Entrepreneurial Operating System for your leadership team)?"

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
  {"booking": true, "name": "<name>", "phone": "<phone>", "company": "<company>", "demoDate": "<YYYY-MM-DD>", "demoTime": "<HH:MM>", "demoType": "<online|physical>", "requestType": "<demo|consultation>"}
- The demoDate MUST be in YYYY-MM-DD format. The demoTime MUST be in 24-hour HH:MM format (e.g. 10:00, 14:30).
- Set requestType to "consultation" if the user chose EOS® Business Consultation, otherwise "demo".
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

CONVERSATION STYLE:
- Warm, professional, and concise. Use simple English suitable for Kenyan business owners.
- Ask one question at a time to understand the user's business before recommending.
- Use bullet points or short paragraphs — avoid walls of text.
- Use bold for product names and key terms.
- Never make up prices — say "contact us for current pricing" if unsure.
- Always end with a clear next step (book demo, register for webinar, or chat on WhatsApp).
- If the user greets you, greet back warmly and ask their name.
- If you know their name, use it naturally in conversation.
"""

def get_zawadi_reply(messages: list) -> str:
    """Call Google Gemini 2.5 Flash with the Zawadi system prompt."""
    try:
        from google import genai
        gemini_key = os.environ.get("GEMINI_API_KEY", "")
        client = genai.Client(api_key=gemini_key)
        # Build full message list with system prompt prepended as first user/model exchange
        contents = [{"role": "user", "parts": [{"text": "System: " + ZAWADI_SYSTEM_PROMPT + "\n\nUser: " + (messages[0]["content"] if messages else "Hello")}]}
                    ] if messages else []
        # If more than one message, build proper history
        if len(messages) > 1:
            contents = []
            for i, msg in enumerate(messages):
                role = "user" if msg["role"] == "user" else "model"
                text = msg["content"]
                if i == 0 and role == "user":
                    text = "System: " + ZAWADI_SYSTEM_PROMPT + "\n\nUser: " + text
                contents.append({"role": role, "parts": [{"text": text}]})
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=contents,
            config={"max_output_tokens": 1500, "temperature": 0.7}
        )
        return response.text.strip()
    except Exception as e:
        print(f"Gemini error: {e}")
        return "I'm having a little trouble connecting right now. Please reach us directly on WhatsApp at +254 116 246 074 or visit www.optimumprimesolutions.co.ke"


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
    import json as _json
    data = request.get_json(force=True, silent=True) or {}
    messages = data.get("messages", [])
    if not messages:
        return jsonify({"error": "No messages provided"}), 400

    reply = get_zawadi_reply(messages)

    # Detect if Zawadi returned a JSON response (booking or handoff)
    try:
        # Strip markdown code fences if present
        clean = reply.strip()
        if clean.startswith('```'):
            clean = clean.split('```')[1]
            if clean.startswith('json'):
                clean = clean[4:]
        clean = clean.strip().lstrip('`').rstrip('`').strip()

        if clean.startswith('{') and ('"booking"' in clean or '"handoff"' in clean):
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

                # Format date nicely for messages
                try:
                    from datetime import date as _date
                    dt = datetime.strptime(demo_date, '%Y-%m-%d')
                    display_date = dt.strftime('%A, %d %B %Y')
                except Exception:
                    display_date = demo_date

                # Format time nicely (HH:MM → 10:00 AM)
                try:
                    t = datetime.strptime(demo_time, '%H:%M')
                    display_time = t.strftime('%I:%M %p').lstrip('0')
                except Exception:
                    display_time = demo_time

                # ── Save lead to Firebase as New (pending team confirmation) ──────
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

                # ── Notify office team (pending approval) ─────────────────────
                try:
                    twilio = _client()
                    req_label = '🤝 Consultation (EOS®)' if request_type == 'consultation' else '📊 TallyPrime Demo'
                    office_body = (
                        f'🤖 *New {"Consultation" if request_type == "consultation" else "Demo"} Request via Zawadi*\n\n'
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
                        twilio.messages.create(
                            from_=FROM_WA,
                            to=team_num,
                            body=office_body,
                            status_callback=STATUS_CALLBACK_URL
                        )
                except Exception as e:
                    print(f'Office notify error: {e}')

                # ── Send working-on-it message to client (no Meet link yet) ────
                try:
                    twilio = _client()
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
                    twilio.messages.create(
                        from_=FROM_WA,
                        to=f'whatsapp:{norm_phone}',
                        body=client_body,
                        status_callback=STATUS_CALLBACK_URL
                    )
                except Exception as e:
                    print(f'Client notify error: {e}')

                return jsonify({
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
                })

            # ── GENERAL HANDOFF (non-booking) ─────────────────────────────────
            if parsed.get('handoff'):
                name     = parsed.get('name', 'Unknown')
                phone    = parsed.get('phone', '')
                interest = parsed.get('interest', 'General enquiry via Zawadi chatbot')

                # Fire team alert via WhatsApp
                try:
                    twilio = _client()
                    alert = (
                        f'\U0001f916 *Zawadi Handoff \u2014 New Lead*\n\n'
                        f'\U0001f464 *Name:* {name}\n'
                        f'\U0001f4de *Phone:* {phone}\n'
                        f'\U0001f4bc *Interest:* {interest}\n\n'
                        f'Reply quickly \u2014 leads convert best within 5 minutes! \u26a1\n'
                        f'\U0001f449 Admin panel: https://www.optimumprimesolutions.co.ke/admin'
                    )
                    for team_num in TEAM_NUMBERS:
                        twilio.messages.create(
                            from_=FROM_WA,
                            to=team_num,
                            body=alert,
                            status_callback=STATUS_CALLBACK_URL
                        )
                except Exception:
                    pass

                # Save to Firebase
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

                return jsonify({
                    'handoff': True,
                    'name': name,
                    'phone': phone,
                    'interest': interest,
                    'whatsapp_url': f"https://wa.me/254116246074?text=Hi%2C%20I%27m%20{name.replace(' ', '%20')}%20and%20I%27m%20interested%20in%20{interest.replace(' ', '%20')}"
                })
    except Exception as e:
        print(f'Chat JSON parse error: {e}')

    return jsonify({'reply': reply, 'handoff': False})


@app.route("/webhook/twilio-status", methods=["POST"])
def twilio_status_webhook():
    """
    Receives Twilio delivery status callbacks.
    Sends a WhatsApp alert to the team if a client message fails to deliver.
    """
    sid        = request.form.get("MessageSid", "")
    status     = request.form.get("MessageStatus", "")
    to_number  = request.form.get("To", "").replace("whatsapp:", "")

    # Only alert on terminal failure statuses
    FAILED_STATUSES = {"failed", "undelivered"}
    if status in FAILED_STATUSES:
        info = _pending_messages.get(sid, {})
        name = info.get("name", "Unknown")
        msg_type = info.get("type", "message")

        alert_body = (
            f"⚠️ *WhatsApp Delivery Failed*\n\n"
            f"📵 *Status:* {status.upper()}\n"
            f"📞 *To:* {to_number}\n"
            f"👤 *Name:* {name}\n"
            f"📦 *Message type:* {msg_type}\n"
            f"🔑 *SID:* {sid}\n\n"
            f"The recipient may not be opted in to the WhatsApp sandbox.\n"
            f"Ask them to send `join <keyword>` to +1 415 523 8886 on WhatsApp, then resend."
        )

        client = _client()
        for team_num in TEAM_NUMBERS:
            try:
                client.messages.create(from_=FROM_WA, to=team_num, body=alert_body)
            except Exception:
                pass

        # Clean up tracking
        _pending_messages.pop(sid, None)

    elif status in {"delivered", "read"}:
        # Clean up on success
        _pending_messages.pop(sid, None)

    return "", 204

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

    client = _client()
    results = []
    for to in TEAM_NUMBERS:
        try:
            msg = client.messages.create(from_=FROM_WA, to=to, body=body)
            results.append({"to": to, "sid": msg.sid, "success": True})
        except Exception as e:
            results.append({"to": to, "error": str(e), "success": False})

    return jsonify({
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

    twilio_client = _client()
    results = {"office": [], "team": [], "client": None}

    # Send to both office numbers
    for to in TEAM_NUMBERS:
        try:
            msg = twilio_client.messages.create(from_=FROM_WA, to=to, body=office_body)
            results["office"].append({"to": to, "sid": msg.sid, "success": True})
        except Exception as e:
            results["office"].append({"to": to, "error": str(e), "success": False})

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

        try:
            msg = twilio_client.messages.create(from_=FROM_WA, to=f"whatsapp:{norm_phone}", body=team_body)
            results["team"].append({"to": norm_phone, "sid": msg.sid, "success": True})
        except Exception as e:
            results["team"].append({"to": norm_phone, "error": str(e), "success": False})

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

        cal_link = build_google_calendar_link(client_name, client_company, demo_date, demo_time)

        is_reschedule = source == "reschedule"
        intro_line = (
            f"Your TallyPrime demo has been *rescheduled*. Here are the updated details:"
            if is_reschedule else
            f"Your TallyPrime demo has been scheduled with Optimum Prime Solutions."
        )
        client_body = (
            f"Hello {client_name}! 👋\n\n"
            f"{intro_line}\n\n"
            f"📆 *Date:* {display_date}\n"
            f"🕐 *Time:* {demo_time} (EAT)\n"
        )
        if demo_type == "online":
            if meet_link:
                client_body += (
                    f"\n📹 *Your Google Meet link:*\n"
                    f"{meet_link}\n"
                    f"_(Click to join at your scheduled time)_\n"
                )
            if cal_link:
                client_body += (
                    f"\n🗓️ *Add to Google Calendar:*\n"
                    f"{cal_link}\n"
                )
        else:
            # Physical demo
            if demo_location:
                client_body += f"\n📍 *Location:* {demo_location}\n"
            client_body += f"\n🤝 Our team will meet you in person at the scheduled time.\n"
        client_body += (
            f"\n⏰ We'll send you a reminder the day before your demo.\n\n"
            f"Any questions? Reach us anytime:\n"
            f"📞 *+254 116 246 074*\n"
            f"🌐 *www.optimumprimesolutions.co.ke*\n\n"
            f"_Optimum Prime Solutions — TallyPrime · Cloud · EOS® · HubSpot CRM_"
        )

        try:
            msg = twilio_client.messages.create(
                from_=FROM_WA,
                to=f"whatsapp:{norm_client}",
                body=client_body,
                status_callback=STATUS_CALLBACK_URL,
            )
            results["client"] = {"to": norm_client, "sid": msg.sid, "success": True}
        except Exception as e:
            results["client"] = {"to": norm_client, "error": str(e), "success": False}

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

    twilio_client = _client()
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
            try:
                msg = twilio_client.messages.create(from_=FROM_WA, to=f"whatsapp:{norm}", body=body)
                reminder_results["team"].append({"to": norm, "sid": msg.sid, "success": True})
            except Exception as e:
                reminder_results["team"].append({"to": norm, "error": str(e), "success": False})

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
                f"_Optimum Prime Solutions — TallyPrime · Cloud · EOS® · HubSpot CRM_"
            )
            try:
                msg = twilio_client.messages.create(
                    from_=FROM_WA,
                    to=f"whatsapp:{norm_client}",
                    body=client_body,
                    status_callback=STATUS_CALLBACK_URL,
                )
                reminder_results["client_msg"] = {"to": norm_client, "sid": msg.sid, "success": True}
            except Exception as e:
                reminder_results["client_msg"] = {"to": norm_client, "error": str(e), "success": False}

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
