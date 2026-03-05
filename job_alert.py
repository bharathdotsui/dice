"""
Dice ServiceNow Job Alert - Bharath Reddy
Checks Dice every hour for new ServiceNow jobs, scores them against resume using Claude AI,
and sends Email + SMS notifications for all matching tiers.
"""

import os
import json
import time
import hashlib
import requests
import smtplib
import logging
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# ─────────────────────────────────────────────
# CONFIGURATION — Fill these in before running
# ─────────────────────────────────────────────
CONFIG = {
    # Email (Gmail recommended — use an App Password, NOT your real password)
    # Guide: https://support.google.com/accounts/answer/185833
    "email_sender":   "your_gmail@gmail.com",
    "email_password": "your_gmail_app_password",   # 16-char App Password
    "email_recipient": "bharath921r@gmail.com",

    # SMS via Twilio (free trial at twilio.com — gives you ~$15 credit)
    # Guide: https://www.twilio.com/docs/sms/quickstart/python
    "twilio_account_sid": "ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
    "twilio_auth_token":  "your_twilio_auth_token",
    "twilio_from_number": "+1XXXXXXXXXX",   # Your Twilio number
    "twilio_to_number":   "+16782504141",   # Bharath's mobile

    # Anthropic API key (get from https://console.anthropic.com)
    "anthropic_api_key": "sk-ant-xxxxxxxxxxxxxxxxxxxx",

    # How often to check (seconds). 3600 = every hour
    "check_interval_seconds": 3600,

    # Minimum tier to notify: 1 = Tier 1 only, 2 = Tier 1+2, 3 = all tiers
    "min_tier": 3,
}

# ─────────────────────────────────────────────
# BHARATH'S RESUME SUMMARY (used for AI scoring)
# ─────────────────────────────────────────────
RESUME_SUMMARY = """
Name: Bharath Reddy
Title: Senior ServiceNow Developer
Experience: 9+ years
Current: Senior ServiceNow Developer/Admin at Fiserv, Alpharetta GA (Dec 2024–Present)
Previous: JP Morgan Chase (2023–2024), Optum (2021–2023), Lowe's (2019–2021), Carbynetech India (2016–2018)

Core Modules: ITSM (Incident, Problem, Change, Request), ITOM (Event Management, Discovery, Service Mapping),
CMDB (CSDM-aligned), ITAM (SAM Pro, HAM Pro), IRM/GRC (Risk, Policy & Compliance, TPRM),
SecOps (Vulnerability Response, SIR), ITBM/SPM/PPM, CSM, Service Catalog, Service Portal,
Performance Analytics, ATF, App Engine Studio, UI Builder, Agent Workspace, Flow Designer, IntegrationHub.

Scripting: JavaScript, GlideRecord/Ajax/Aggregate, Script Includes, Business Rules, Client Scripts,
UI Policies/Actions, Catalog Client Scripts, Scheduled Jobs, ACL scripting, AngularJS.

Integrations: REST/Scripted REST, SOAP, OAuth 2.0, SSO (SAML/OIDC), MID Server, IntegrationHub spokes,
Import Sets/Transform Maps, Azure Logic Apps, Splunk, Dynatrace, Rapid7, Tenable, SCCM, BigFix.

Certifications (10 total): CSA, CIS (Implementation Specialist), CAD (App Developer), CMDB Health,
Playbooks Advanced, ATF, Flow Designer, IntegrationHub, App Engine Studio, UI Builder.

Industry: Banking/Payments (Fiserv, JPMC), Healthcare (Optum), Retail (Lowe's)
Education: MS Information Technology
Location: Atlanta, GA
"""

# ─────────────────────────────────────────────
# SEEN JOBS PERSISTENCE
# ─────────────────────────────────────────────
SEEN_JOBS_FILE = "seen_jobs.json"

def load_seen_jobs():
    if os.path.exists(SEEN_JOBS_FILE):
        with open(SEEN_JOBS_FILE, "r") as f:
            return set(json.load(f))
    return set()

def save_seen_jobs(seen):
    with open(SEEN_JOBS_FILE, "w") as f:
        json.dump(list(seen), f)

# ─────────────────────────────────────────────
# DICE JOB SEARCH
# ─────────────────────────────────────────────
def fetch_dice_jobs():
    """Fetch ServiceNow jobs posted in the last 24 hours from Dice."""
    url = "https://job-search-api.svc.dhigroupinc.com/v1/dice/jobs/search"
    params = {
        "q": "ServiceNow Developer",
        "countryCode2": "US",
        "radius": 30,
        "radiusUnit": "mi",
        "page": 1,
        "pageSize": 50,
        "facets": "employmentType|postedDate|workplaceTypes",
        "filters.postedDate": "ONE",  # Last 24 hours
        "sort": "score",
        "language": "en",
    }
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json",
        "x-api-key": "1YAt0R9wBg4WfsF9VB2778F5CHLAPMVW3WAZcKd8",  # Public Dice API key
    }
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        return data.get("data", [])
    except Exception as e:
        logging.error(f"Dice API error: {e}")
        return []

# ─────────────────────────────────────────────
# AI SCORING VIA CLAUDE
# ─────────────────────────────────────────────
def score_job_with_claude(job):
    """Use Claude AI to score the job against Bharath's resume."""
    title = job.get("title", "")
    summary = job.get("summary", "")[:1500]  # Trim to save tokens
    company = job.get("companyName", "")
    location = job.get("jobLocation", {})
    location_str = location.get("displayName", "Remote") if location else "Remote"
    salary = job.get("salary", "Not specified")
    workplace = ", ".join(job.get("workplaceTypes") or ["Not specified"])

    prompt = f"""You are evaluating a job posting for a ServiceNow professional. Score the match and assign a tier.

CANDIDATE RESUME SUMMARY:
{RESUME_SUMMARY}

JOB POSTING:
Title: {title}
Company: {company}
Location: {location_str}
Salary: {salary}
Workplace: {workplace}
Description: {summary}

IMPORTANT: If the job requires US citizenship or security clearance, set tier to 0 (excluded).

Respond ONLY with a valid JSON object, no markdown, no extra text:
{{
  "tier": <1, 2, 3, or 0 if clearance/citizenship required>,
  "match_score": <0-100>,
  "tier_label": "<Near-Perfect Match | Strong Match | Good Match | Excluded>",
  "top_matching_skills": ["skill1", "skill2", "skill3"],
  "gap": "<one short sentence on any skill gap, or 'None'>",
  "why": "<2 sentences max on why this is a good match>"
}}"""

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": CONFIG["anthropic_api_key"],
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 400,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
        resp.raise_for_status()
        raw = resp.json()["content"][0]["text"].strip()
        # Strip markdown fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw.strip())
    except Exception as e:
        logging.error(f"Claude scoring error for '{title}': {e}")
        return None

# ─────────────────────────────────────────────
# EMAIL NOTIFICATION
# ─────────────────────────────────────────────
def send_email(jobs_with_scores):
    """Send a formatted HTML email with all new matching jobs."""
    tier_emoji = {1: "🏆", 2: "🥈", 3: "🥉"}
    tier_color = {1: "#1a7f37", 2: "#0550ae", 3: "#7d4e00"}

    rows_html = ""
    for job, score in jobs_with_scores:
        tier = score["tier"]
        emoji = tier_emoji.get(tier, "📋")
        color = tier_color.get(tier, "#333")
        location = job.get("jobLocation", {})
        location_str = location.get("displayName", "Remote") if location else "Remote"
        workplace = ", ".join(job.get("workplaceTypes") or ["N/A"])
        skills_str = ", ".join(score.get("top_matching_skills", []))
        url = job.get("detailsPageUrl", "#")

        rows_html += f"""
        <tr style="border-bottom:1px solid #eee;">
          <td style="padding:16px 12px; vertical-align:top;">
            <div style="font-size:16px; font-weight:700; color:{color};">{emoji} {job.get('title','')}</div>
            <div style="color:#555; margin-top:2px;">🏢 {job.get('companyName','')} &nbsp;|&nbsp; 📍 {location_str} &nbsp;|&nbsp; 💼 {workplace}</div>
            <div style="color:#555; margin-top:2px;">💰 {job.get('salary','N/A')} &nbsp;|&nbsp; 🎯 Match Score: <b>{score['match_score']}/100</b> &nbsp;|&nbsp; {score['tier_label']}</div>
            <div style="margin-top:6px; color:#333;">✅ <b>Matching Skills:</b> {skills_str}</div>
            <div style="margin-top:4px; color:#333;">💡 {score['why']}</div>
            {"<div style='margin-top:4px; color:#888;'>⚠️ Gap: " + score['gap'] + "</div>" if score.get('gap') and score['gap'] != 'None' else ""}
            <div style="margin-top:8px;">
              <a href="{url}" style="background:#0066cc; color:white; padding:6px 14px; border-radius:4px; text-decoration:none; font-size:13px;">View & Apply →</a>
            </div>
          </td>
        </tr>"""

    html = f"""
    <html><body style="font-family:Arial,sans-serif; max-width:700px; margin:0 auto; color:#222;">
      <div style="background:#0066cc; padding:20px; border-radius:8px 8px 0 0;">
        <h2 style="color:white; margin:0;">🔔 New ServiceNow Jobs on Dice</h2>
        <p style="color:#cce0ff; margin:4px 0 0;">{len(jobs_with_scores)} new match{'es' if len(jobs_with_scores)>1 else ''} found — {datetime.now().strftime('%b %d, %Y at %I:%M %p')}</p>
      </div>
      <table width="100%" cellpadding="0" cellspacing="0" style="border:1px solid #ddd; border-top:none; border-radius:0 0 8px 8px;">
        {rows_html}
      </table>
      <p style="color:#aaa; font-size:12px; text-align:center; margin-top:16px;">
        Powered by Claude AI · Dice Job Alert for Bharath Reddy
      </p>
    </body></html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"🔔 {len(jobs_with_scores)} New ServiceNow Job{'s' if len(jobs_with_scores)>1 else ''} on Dice"
    msg["From"] = CONFIG["email_sender"]
    msg["To"] = CONFIG["email_recipient"]
    msg.attach(MIMEText(html, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(CONFIG["email_sender"], CONFIG["email_password"])
            server.sendmail(CONFIG["email_sender"], CONFIG["email_recipient"], msg.as_string())
        logging.info(f"✅ Email sent with {len(jobs_with_scores)} jobs.")
    except Exception as e:
        logging.error(f"Email send error: {e}")

# ─────────────────────────────────────────────
# SMS NOTIFICATION (Twilio)
# ─────────────────────────────────────────────
def send_sms(jobs_with_scores):
    """Send a concise SMS summary via Twilio."""
    try:
        from twilio.rest import Client
        client = Client(CONFIG["twilio_account_sid"], CONFIG["twilio_auth_token"])

        tier_emoji = {1: "🏆", 2: "🥈", 3: "🥉"}
        lines = [f"🔔 {len(jobs_with_scores)} new ServiceNow job(s) on Dice:\n"]
        for job, score in jobs_with_scores[:5]:  # SMS: top 5 only
            emoji = tier_emoji.get(score["tier"], "📋")
            title = job.get("title", "")[:40]
            company = job.get("companyName", "")[:25]
            lines.append(f"{emoji} {title} @ {company} ({score['match_score']}/100)")

        if len(jobs_with_scores) > 5:
            lines.append(f"...and {len(jobs_with_scores)-5} more. Check your email!")
        lines.append("\nCheck email for full details & apply links.")

        client.messages.create(
            body="\n".join(lines),
            from_=CONFIG["twilio_from_number"],
            to=CONFIG["twilio_to_number"],
        )
        logging.info("✅ SMS sent.")
    except ImportError:
        logging.warning("Twilio not installed. Run: pip install twilio")
    except Exception as e:
        logging.error(f"SMS send error: {e}")

# ─────────────────────────────────────────────
# MAIN LOOP
# ─────────────────────────────────────────────
def run_check():
    logging.info(f"🔍 Checking Dice for new ServiceNow jobs... [{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]")
    seen = load_seen_jobs()
    jobs = fetch_dice_jobs()

    if not jobs:
        logging.info("No jobs returned from Dice.")
        return

    logging.info(f"Found {len(jobs)} jobs from Dice. Filtering unseen jobs...")

    new_jobs = []
    for job in jobs:
        job_id = job.get("guid") or job.get("id") or hashlib.md5(job.get("title","").encode()).hexdigest()
        if job_id not in seen:
            new_jobs.append((job_id, job))

    if not new_jobs:
        logging.info("No new jobs since last check.")
        return

    logging.info(f"{len(new_jobs)} new jobs found. Scoring with Claude AI...")

    qualified = []
    for job_id, job in new_jobs:
        seen.add(job_id)  # Mark as seen regardless of score
        score = score_job_with_claude(job)
        if score is None:
            continue
        tier = score.get("tier", 0)
        if tier == 0:
            logging.info(f"  ⛔ Excluded (clearance/citizenship): {job.get('title')}")
            continue
        if tier <= CONFIG["min_tier"]:
            logging.info(f"  ✅ Tier {tier} ({score['match_score']}/100): {job.get('title')} @ {job.get('companyName')}")
            qualified.append((job, score))
        else:
            logging.info(f"  ⏭  Below threshold (Tier {tier}): {job.get('title')}")
        time.sleep(1)  # Avoid hammering the API

    save_seen_jobs(seen)

    if qualified:
        # Sort by match score descending
        qualified.sort(key=lambda x: x[1]["match_score"], reverse=True)
        logging.info(f"📬 Sending notifications for {len(qualified)} qualified jobs...")
        send_email(qualified)
        send_sms(qualified)
    else:
        logging.info("No jobs met the minimum tier threshold.")

def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler("job_alert.log"),
        ]
    )
    logging.info("🚀 Dice ServiceNow Job Alert started!")
    logging.info(f"   Checking every {CONFIG['check_interval_seconds']//60} minutes")
    logging.info(f"   Notifying: {CONFIG['email_recipient']} + SMS {CONFIG['twilio_to_number']}")

    while True:
        try:
            run_check()
        except Exception as e:
            logging.error(f"Unexpected error in run_check: {e}")
        logging.info(f"💤 Sleeping {CONFIG['check_interval_seconds']//60} min until next check...\n")
        time.sleep(CONFIG["check_interval_seconds"])

if __name__ == "__main__":
    main()
