#!/usr/bin/env python3
"""
Day-of Webinar Reminder Script
================================
Fetches all registrants from Firebase and sends each one a WhatsApp reminder
with the Google Meet join link.

Run this on the morning of 15th July 2026 (e.g. 9:00 AM EAT) and again
30 minutes before the webinar (2:30 PM EAT).

Usage:
    python3 send_webinar_reminder.py
    python3 send_webinar_reminder.py --dry-run   # preview without sending
"""

import sys
import csv
import io
import time
import requests

# ── Config ────────────────────────────────────────────────────────────────────
EXPORT_URL     = "https://optimum-prime-lead-notifier.onrender.com/export-webinar"
NOTIFIER_URL   = "https://optimum-prime-lead-notifier.onrender.com/new-lead"
WEBINAR_DATE   = "Wednesday, 15th July 2026"
WEBINAR_TIME   = "3:00 PM – 4:00 PM (EAT)"
MEET_LINK      = "https://meet.google.com/ded-fdcf-aac"
DRY_RUN        = "--dry-run" in sys.argv

# ── Fetch registrants ─────────────────────────────────────────────────────────
def fetch_registrants():
    resp = requests.get(EXPORT_URL, timeout=30)
    reader = csv.DictReader(io.StringIO(resp.text))
    seen_phones = set()
    registrants = []
    for r in reader:
        name  = r.get("Name", "").strip()
        phone = r.get("Phone", "").strip()
        if name and phone and phone not in seen_phones:
            seen_phones.add(phone)
            registrants.append({"name": name, "phone": phone, "company": r.get("Company", "")})
    return registrants

# ── Build reminder message ────────────────────────────────────────────────────
def build_reminder(name: str) -> str:
    return (
        f"Hello {name}! 👋\n\n"
        f"*Reminder:* Our FREE TallyPrime 7.1 Webinar is *today!* 🎉\n\n"
        f"📅 *Date:* {WEBINAR_DATE}\n"
        f"🕒 *Time:* {WEBINAR_TIME}\n\n"
        f"📹 *Your Google Meet Join Link:*\n"
        f"{MEET_LINK}\n"
        f"_(Click to join at 3:00 PM EAT)_\n\n"
        f"*What We'll Cover:*\n"
        f"✅ Auto Wrap Text\n"
        f"✅ Professional Invoice Print Templates\n"
        f"✅ Scheduled Auto Backup\n"
        f"✅ Reuse Deleted Voucher Numbers\n"
        f"✅ Live Q&A\n\n"
        f"We look forward to seeing you shortly!\n\n"
        f"📞 *+254 116 246 074*\n"
        f"🌐 *www.optimumprimesolutions.co.ke*\n\n"
        f"_Optimum Prime Solutions — TallyPrime · Cloud · EOS® · HubSpot CRM · Biz Analyst_"
    )

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print("Fetching registrants from Firebase...")
    registrants = fetch_registrants()
    print(f"Found {len(registrants)} registrant(s).\n")

    if not registrants:
        print("No registrants found. Exiting.")
        return

    for i, r in enumerate(registrants, 1):
        name    = r["name"]
        phone   = r["phone"]
        company = r["company"]
        msg     = build_reminder(name)

        print(f"[{i}/{len(registrants)}] Sending to {name} ({phone})...")
        if DRY_RUN:
            print(f"  [DRY RUN] Would send:\n{msg}\n")
            continue

        payload = {
            "name":                 name,
            "phone":                phone,
            "company":              company,
            "interest":             "Webinar Reminder — TallyPrime 7.1",
            "source":               "Day-of Reminder Script",
            "confirmation_message": msg,
        }
        try:
            resp = requests.post(NOTIFIER_URL, json=payload, timeout=30)
            result = resp.json()
            lead_ok = result.get("lead_replied", False)
            print(f"  {'✅ Sent' if lead_ok else '❌ Failed'} — {result.get('details', {}).get('lead', {})}")
        except Exception as e:
            print(f"  ❌ Error: {e}")

        # Pace requests — avoid Twilio rate limits
        if i < len(registrants):
            time.sleep(1)

    print("\nDone.")

if __name__ == "__main__":
    main()
