#!/usr/bin/env python3
"""
Optimum Prime Solutions — Lead Auto-Reply & Webinar Notification System
"""

import os
import csv
import io
import requests
from twilio.rest import Client
from flask import Flask, request, jsonify, Response
from flask_cors import CORS

ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
AUTH_TOKEN  = os.environ.get("TWILIO_AUTH_TOKEN")
FROM_WA     = "whatsapp:+14155238886"

FIREBASE_BASE = "https://optimum-prime-website-default-rtdb.europe-west1.firebasedatabase.app"
FIREBASE_WEBINAR_URL = f"{FIREBASE_BASE}/webinar_registrants.json"
FIREBASE_LEADS_URL   = f"{FIREBASE_BASE}/leads.json"

TEAM_NUMBERS = [
    "whatsapp:+254758449475",
    "whatsapp:+254116246074",
]

app = Flask(__name__)
CORS(app)

def _client():
    return Client(ACCOUNT_SID, AUTH_TOKEN)

def get_registration_count() -> int:
    """Fetch the current total number of webinar registrants from Firebase."""
    try:
        resp = requests.get(FIREBASE_WEBINAR_URL, timeout=5)
        data = resp.json()
        if data and isinstance(data, dict):
            return len(data)
        return 0
    except Exception:
        return -1  # -1 signals a fetch error

def notify_team(lead: dict) -> list:
    name     = lead.get("name", "Unknown")
    phone    = lead.get("phone", "Not provided")
    email    = lead.get("email", "Not provided")
    company  = lead.get("company", "Not provided")
    interest = lead.get("interest", "General enquiry")
    source   = lead.get("source", "Website")
    message  = lead.get("message", "")

    # Use appropriate header based on interest type
    if "Webinar" in interest:
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
    phone = lead.get("phone", "")
    if not phone:
        return {"success": False, "reason": "No phone number provided"}
    if not phone.startswith("+"):
        phone = "+" + phone.lstrip("0")

    name = lead.get("name", "there")

    # If a custom confirmation message is provided (e.g. webinar registration), use it
    custom_msg = lead.get("confirmation_message", "")
    if custom_msg:
        body = custom_msg
    else:
        interest = lead.get("interest", "our services")
        body = (
            f"Hello {name}! 👋\n\n"
            f"Thank you for reaching out to *Optimum Prime Solutions* — "
            f"Kenya's Certified TallyPrime Partner.\n\n"
            f"We've received your enquiry about *{interest}* and will get back to you shortly.\n\n"
            f"Feel free to explore our website or reach us directly:\n"
            f"📞 *+254 116 246 074*\n"
            f"🌐 *www.optimumprimesolutions.co.ke*\n\n"
            f"_Optimum Prime Solutions — TallyPrime · Cloud · EOS® · HubSpot CRM_"
        )

    client = _client()
    try:
        msg = client.messages.create(from_=FROM_WA, to=f"whatsapp:{phone}", body=body)
        return {"success": True, "sid": msg.sid, "to": phone}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ── CSV Export Helpers ────────────────────────────────────────────────────────

def fetch_firebase(url: str) -> dict:
    try:
        resp = requests.get(url, timeout=8)
        data = resp.json()
        return data if isinstance(data, dict) and "error" not in data else {}
    except Exception:
        return {}

def build_leads_csv() -> str:
    data = fetch_firebase(FIREBASE_LEADS_URL)
    output = io.StringIO()
    fields = ["Name", "Company", "Phone", "Email", "Business Type",
              "Current Software", "Preferred Demo Date", "Message", "Status", "Submitted At"]
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
            "Message":             r.get("message", ""),
            "Status":              r.get("status", "New"),
            "Submitted At":        r.get("createdAt", ""),
        })
    rows.sort(key=lambda x: x["Submitted At"])
    writer.writerows(rows)
    return output.getvalue(), len(rows)

def build_webinar_csv() -> str:
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

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "Optimum Prime Lead Notifier"})

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
