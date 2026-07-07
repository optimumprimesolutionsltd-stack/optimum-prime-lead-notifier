#!/usr/bin/env python3
"""
Optimum Prime Solutions — Lead Auto-Reply & Webinar Notification System
"""

import os
import requests
from twilio.rest import Client
from flask import Flask, request, jsonify
from flask_cors import CORS

ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
AUTH_TOKEN  = os.environ.get("TWILIO_AUTH_TOKEN")
FROM_WA     = "whatsapp:+14155238886"

# Firebase Realtime Database — webinar registrations node
FIREBASE_URL = "https://optimum-prime-website-default-rtdb.europe-west1.firebasedatabase.app/webinar_registrants.json"

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
        resp = requests.get(FIREBASE_URL, timeout=5)
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

    count = get_registration_count()
    if count == -1:
        count_line = "📊 *Total registrations:* (unavailable)\n"
    else:
        count_line = f"📊 *Total registrations so far:* {count}\n"

    body = (
        f"🔔 *New Webinar Registration — Optimum Prime Solutions*\n\n"
        f"👤 *Name:* {name}\n"
        f"🏢 *Company:* {company}\n"
        f"📞 *Phone:* {phone}\n"
        f"📧 *Email:* {email}\n"
        f"💼 *Interest:* {interest}\n"
        f"📍 *Source:* {source}\n"
    )
    if message:
        body += f"💬 *Message:* {message}\n"
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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
