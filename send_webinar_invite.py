#!/usr/bin/env python3
"""Send a webinar invite (Message 1) to a single number."""

import os
import requests

NOTIFIER_URL = "https://optimum-prime-lead-notifier.onrender.com/new-lead"

WEBINAR_DATE = "Wednesday, 15th July 2026"
WEBINAR_TIME = "3:00 PM – 4:00 PM (EAT)"
WEBINAR_URL  = "https://www.optimumprimesolutions.co.ke/webinar"

invite_message = (
    f"Hello! 👋\n\n"
    f"You're invited to our *FREE TallyPrime 7.1 Webinar* — exclusively for businesses in Kenya!\n\n"
    f"📅 *Date:* {WEBINAR_DATE}\n"
    f"🕒 *Time:* {WEBINAR_TIME}\n"
    f"📍 *Venue:* Online via Google Meet\n"
    f"💰 *Cost:* FREE\n\n"
    f"*What We'll Cover:*\n"
    f"✅ Auto Wrap Text\n"
    f"✅ Professional Invoice Print Templates (8 templates)\n"
    f"✅ Scheduled Auto Backup\n"
    f"✅ Reuse Deleted Voucher Numbers\n"
    f"✅ Live Q&A\n\n"
    f"👉 *Register here (takes 1 minute):*\n"
    f"{WEBINAR_URL}\n\n"
    f"Spots are limited — secure yours today!\n\n"
    f"📞 *+254 116 246 074*\n"
    f"🌐 *www.optimumprimesolutions.co.ke*\n\n"
    f"_Optimum Prime Solutions — TallyPrime · Cloud · EOS® · HubSpot CRM · Biz Analyst_"
)

payload = {
    "name": "Mr. Chege",
    "phone": "0758449475",
    "email": "optimumprimesolutionsltd@gmail.com",
    "company": "Optimum Prime Solutions",
    "interest": "Webinar Invite — TallyPrime 7.1",
    "source": "Manual Invite Script",
    "message": f"Webinar invite sent for {WEBINAR_DATE}",
    "confirmation_message": invite_message,
}

resp = requests.post(NOTIFIER_URL, json=payload, timeout=30)
print(resp.json())
