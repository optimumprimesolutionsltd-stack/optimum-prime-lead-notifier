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

    # Add preferred demo slot + meet link to team alert (no calendar URL — keeps message short)
    if not is_webinar and demo_date and demo_time:
        display_date = format_date_display(demo_date)
        meet_link = generate_meet_link(name, company if company != "Not provided" else "", demo_date, demo_time)
        body += (
            f"\n📅 *Preferred slot:* {display_date}\n"
            f"🕐 *Time:* {demo_time} (EAT)\n"
        )
        if meet_link:
            body += f"\n📹 *Meet link:* {meet_link}\n"

    if count_line:
        body += f"\n{count_line}"

    body += "\n_Reply quickly — leads convert best within 5 minutes!_ ⚡"

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

        body = (
            f"Hello {name}! 👋\n\n"
            f"Your TallyPrime demo request has been received! 🎉\n\n"
        )

        if demo_date and demo_time:
            display_date = format_date_display(demo_date)
            meet_link = generate_meet_link(name, company, demo_date, demo_time)
            body += (
                f"📅 *Date:* {display_date}\n"
                f"🕐 *Time:* {demo_time} (EAT)\n"
            )
            if business_type:
                body += f"💼 *Business type:* {business_type}\n"
            if current_software:
                body += f"💻 *Current software:* {current_software}\n"
            body += "\n"
            if meet_link:
                body += (
                    f"📹 *Your Google Meet link:*\n"
                    f"{meet_link}\n"
                    f"_(Click to join at your scheduled time)_\n\n"
                )
            if cal_link:
                body += (
                    f"🗓️ *Add to Google Calendar:*\n"
                    f"{cal_link}\n\n"
                )
            # Check if demo is today (EAT timezone)
            try:
                eat = timezone(timedelta(hours=3))
                today_eat = datetime.now(eat).strftime("%Y-%m-%d")
                is_today = (demo_date == today_eat)
            except Exception:
                is_today = False

            if is_today:
                body += (
                    f"⏰ *Your demo is today!* We'll send you a reminder 30 minutes before your session.\n\n"
                )
            else:
                body += (
                    f"⏰ *We'll send you a reminder* the day before and 30 minutes before your demo.\n\n"
                )
        else:
            body += "Our team will reach out shortly to confirm your demo slot.\n\n"

        body += (
            f"One of our TallyPrime experts will walk you through exactly what the software can do for your business.\n\n"
            f"Any questions before the demo? Reach us anytime:\n"
            f"📞 *+254 116 246 074*\n"
            f"🌐 *www.optimumprimesolutions.co.ke*\n\n"
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
            "Phone":               r.get("phone", ""),
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
            "Phone":         r.get("phone", ""),
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

HANDOFF TO HUMAN — SMART HANDOFF PROTOCOL:
When the user expresses any of the following intents, trigger the smart handoff:
- Wants to speak to a person / consultant / expert
- Wants a quote or pricing
- Wants to book a demo or consultation
- Asks to be called back
- Says "call me", "contact me", "reach me", "I'm interested", "let's proceed"

SMART HANDOFF STEPS:
1. First, warmly acknowledge their interest.
2. Ask for their name (if you don't already know it) and their WhatsApp number.
3. Once you have BOTH name and phone number, respond with ONLY this exact JSON format (no other text):
   {"handoff": true, "name": "<their name>", "phone": "<their phone>", "interest": "<brief summary of what they want>"}
4. Do NOT include any other text before or after the JSON when triggering a handoff.
5. If you already know their name from earlier in the conversation, only ask for their phone number.

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

    # Detect if Zawadi returned a handoff JSON
    try:
        # Strip markdown code fences if present
        clean = reply.strip().lstrip('`').rstrip('`')
        if clean.startswith('{') and '"handoff"' in clean:
            handoff_data = _json.loads(clean)
            if handoff_data.get('handoff'):
                name     = handoff_data.get('name', 'Unknown')
                phone    = handoff_data.get('phone', '')
                interest = handoff_data.get('interest', 'General enquiry via Zawadi chatbot')

                # Fire team alert via WhatsApp
                try:
                    client = _client()
                    alert = (
                        f"\U0001f916 *Zawadi Handoff — New Lead*\n\n"
                        f"\U0001f464 *Name:* {name}\n"
                        f"\U0001f4de *Phone:* {phone}\n"
                        f"\U0001f4bc *Interest:* {interest}\n\n"
                        f"Reply quickly — leads convert best within 5 minutes! \u26a1"
                    )
                    for team_num in TEAM_NUMBERS:
                        client.messages.create(
                            from_=FROM_WA,
                            to=team_num,
                            body=alert,
                            status_callback=STATUS_CALLBACK_URL
                        )
                except Exception:
                    pass

                # Also save to Firebase as a lead
                try:
                    lead_record = {
                        "name": name,
                        "phone": phone,
                        "message": interest,
                        "source": "Zawadi Chatbot Handoff",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                    requests.post(FIREBASE_LEADS_URL, json=lead_record, timeout=5)
                except Exception:
                    pass

                return jsonify({
                    "handoff": True,
                    "name": name,
                    "phone": phone,
                    "interest": interest,
                    "whatsapp_url": f"https://wa.me/254116246074?text=Hi%2C%20I%27m%20{name.replace(' ', '%20')}%20and%20I%27m%20interested%20in%20{interest.replace(' ', '%20')}"
                })
    except Exception:
        pass

    return jsonify({"reply": reply, "handoff": False})


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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
