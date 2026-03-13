import os
import sys
import smtplib
from email import message_from_bytes
from email.message import EmailMessage
from email.policy import SMTP
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from email.generator import BytesGenerator
from io import BytesIO
import requests
import logging
import colorlog
import argparse
import schedule
import time
import json
import warnings
import traceback
import tempfile
import subprocess
try:
    from systemd import journal
except ModuleNotFoundError:
    pass
from datetime import date, timedelta
from dotenv import load_dotenv
from requests.exceptions import SSLError

# Custom SMTP class to log debug messages
class LoggingSMTP(smtplib.SMTP):
    def _print_debug(self, *args):
        # Join all args into a single string for logging
        msg = ' '.join(str(a) for a in args)
        # Use your logger at the desired level
        logger.debug(f"[SMTP] {msg}")

global db

# Creating Logger
logger = logging.getLogger("Main")

fmt = colorlog.ColoredFormatter(
    "%(light_blue)s  %(asctime)s | %(log_color)s%(levelname)s%(reset)s %(arrow_log_color)s>>>%(reset)s %(message_log_color)s%(message)s",
    reset=True,
    datefmt="%Y-%m-%dT%H:%M:%SZ",
    log_colors={
        'DEBUG': 'light_black,thin',
        'INFO': 'white',
        'WARNING': 'yellow,bold',
        'ERROR': 'red,bold',
        'CRITICAL': 'red,bg_light_white,bold',
    },
    secondary_log_colors={
        'message': {
            'DEBUG': 'white',
            'WARNING': 'yellow',
            'ERROR': 'red',
            'CRITICAL': 'red,bold'
        },
        'arrow': {
            'DEBUG': 'white',
            'INFO': 'white',
            'WARNING': 'yellow',
            'ERROR': 'red',
            'CRITICAL': 'red'
        }
    }
)
try:
    journal_handler = journal.JournalHandler()
    journal_handler.setLevel(logging.DEBUG)
except NameError:
    logger.warning("systemd.journal module not found, journal logging disabled")
else:
    logger.addHandler(journal_handler)

stdout = colorlog.StreamHandler(stream=sys.stdout)
stdout.setFormatter(fmt)

logger.addHandler(stdout)
logger.setLevel(logging.DEBUG)

# Debug Params
parser = argparse.ArgumentParser(description='Manages Notion and notifications')
parser.add_argument('--debug', action='store_true', help='Enable debug logging')
parser.add_argument('--no-smime', action='store_true', help='Disable S/MIME')
parser.add_argument('--development', action='store_true', help='Runs scripts immediatly instead of scheduling them')
debugParam = parser.parse_args().debug
devParam = parser.parse_args().development
global smimeParam
smimeParam = parser.parse_args().no_smime

if debugParam:
    stdout.setLevel(logging.DEBUG)
    logger.info('Executing with debug mode')
else:
    stdout.setLevel(logging.INFO)

if devParam:
    logger.info('Executing with development mode')

# Catch unhandled exceptions
def handle_unhandled_exception(exc_type, exc_value, exc_traceback):
    if issubclass(exc_type, KeyboardInterrupt):
        logger.exception("KeyboardInterrupt, exiting", exc_info=(exc_type, exc_value, exc_traceback))
        return
    logger.critical("Uncaught exception", exc_info=(exc_type, exc_value, exc_traceback))
sys.excepthook = handle_unhandled_exception

# Catch Warnings
def catch_warnings(message, category, filename, lineno, file=None, line=None):
    logger.warning(message)

warnings.showwarning = catch_warnings


# Load Environment
load_dotenv()
INTEGRATION_SECRET = os.getenv('INTEGRATION_SECRET')

if not INTEGRATION_SECRET:
    logger.error('INTEGRATION_SECRET environment variable not set.')

# Load default API parameters
Base_URL = 'https://api.notion.com/v1'
headers = {'Authorization': f'Bearer {INTEGRATION_SECRET}','Notion-Version': '2022-06-28','Content-Type': 'application/json'}

# Functions
def refresh_database():
    global db
    try:
        logger.info("Refreshing database")
        r = requests.post(
            f'{Base_URL}/databases/1308dcf755cc8018be80dfeb8276b410/query',
            headers=headers,
            verify=True
        )
        if not r.ok:
            logger.error("Error while receiving new datasets from notion")
            logger.debug(f"{r.status_code}: {r.reason}")
            logger.debug(f"{r.text}")

    except SSLError:
        try:
            r = requests.post(
                f'{Base_URL}/databases/1308dcf755cc8018be80dfeb8276b410/query',
                headers=headers,
                verify=False
            )
            if not r.ok:
                logger.error("Error while receiving new datasets from notion")
                logger.debug(f"{r.status_code}: {r.reason}")
                logger.debug(f"{r.text}")
        except Exception as e:
            logger.error("Error while updating datasets")
            logger.debug(e)
            db = None
        else:
            db = r.json()
    except Exception as e:
        logger.error("Error while updating datasets")
        logger.debug(e)
        db = None
    else:
        db = r.json()
    finally:
        if not db:
            logger.info("Did not update database")
        else:
            with open("db.json", 'w') as file:
                json.dump(db,file)
            logger.info("Completed refreshing database")

        now = round(time.time())
        updated = round(os.path.getmtime("db.json"))
        if (now - updated) > 172800:
            logger.warning("Database hasn't been updated in 48 hours")

def reminders():
    global db
    logger.info("Getting reminders for the day")
    today = date.today().isoformat()
    day_1 = (date.today() + timedelta(days=1)).isoformat()
    day_2 = (date.today() + timedelta(days=2)).isoformat()
    data = db

    unsubmitted_due_today = [
        page for page in data["results"]
        if
        (task_type := page["properties"].get("Task Type", {}).get("select", {}).get("name", "").lower()) == "assignment"
        and (
                (
                        (draft := page["properties"].get("Draft Date", {}).get("date", {}))
                        and draft.get("start") == today
                        and page["properties"].get("Status", {}).get("status", {}).get("name",
                                                                                       "").lower() == "not submitted"
                ) or (
                        (due := page["properties"].get("Due Date", {}).get("date", {}))
                        and due.get("start") == today
                        and page["properties"].get("Status", {}).get("status", {}).get("name", "").lower() in [
                            "not submitted", "draft submitted"]
                )
        )
    ]
    logger.debug("Unsubmitted tasks due today: {}".format(unsubmitted_due_today))

    exams_today = [
        page for page in data['results']
        if page['properties'].get("Task Type", {}).get("select", {}).get("name", "").lower() in ['exam', 'practical']
        and (
            (page['properties'].get("Due Date", {}).get("date", {}) or {}).get("start") == today
            or (page['properties'].get("Draft Date", {}).get("date", {}) or {}).get("start") == today
        )
    ]
    logger.debug("Exams today: {}".format(exams_today))

    unsubmitted_due_soon = [
        page for page in data["results"]
        if (
            (
                (draft := page["properties"].get("Draft Date", {}).get("date", {}))
                and day_1 <= draft.get("start", "") <= day_2
                and page["properties"].get("Status", {}).get("status", {}).get("name", "").lower() == "not submitted"
            ) or (
                (due := page["properties"].get("Due Date", {}).get("date", {}))
                and day_1 <= due.get("start", "") <= day_2
                and page["properties"].get("Status", {}).get("status", {}).get("name", "").lower() in [
                    "not submitted", "draft submitted"]
            )
        )
    ]
    logger.debug("Unsubmitted tasks due soon: {}".format(unsubmitted_due_soon))

    notify(unsubmitted_due_today,exams_today,unsubmitted_due_soon,"daily")

def weekly_summary():
    global db
    logger.info("Getting weekly summary")
    today = date.today()
    week_later = today + timedelta(days=7)
    data = db

    assignments = [
        page for page in data["results"]
        if (task_type := page["properties"].get("Task Type", {}).get("select", {}).get("name", "").lower()) == "assignment"
        and (
            (
                (draft := page["properties"].get("Draft Date", {}).get("date", {}))
                and today.isoformat() <= draft.get("start", "") <= week_later.isoformat()
                and page["properties"].get("Status", {}).get("status", {}).get("name", "").lower() == "not submitted"
            ) or (
                (due := page["properties"].get("Due Date", {}).get("date", {}))
                and today.isoformat() <= due.get("start", "") <= week_later.isoformat()
                and page["properties"].get("Status", {}).get("status", {}).get("name", "").lower() in [
                    "not submitted", "draft submitted"]
            )
        )
    ]

    exams = [
        page for page in data['results']
        if page['properties'].get("Task Type", {}).get("select", {}).get("name", "").lower() in ['exam', 'practical']
        and (
            (due := page['properties'].get("Due Date", {}).get("date", {}))
            and today.isoformat() <= due.get("start", "") <= week_later.isoformat()
            or
            (draft := page['properties'].get("Draft Date", {}).get("date", {}))
            and today.isoformat() <= draft.get("start", "") <= week_later.isoformat()
        )
    ]

    notify(assignments, exams, [], "weekly")

def load_db():
    global db
    try:
        with open("db.json","r") as file:
            db = json.load(file)
            return db
    except FileNotFoundError:
        open("db.json", "x")
        return None

def notify(assignments,exams,soon,scope):
    def format_task(task, date_label, date):
        title = task['properties']['Task Name']['title'][0]['plain_text']
        url = task.get("url", '#')
        return f'''
        <div class="task">
            <div class="task-title"><a href="{url}">{title}</a></div>
            <div class="task-date">{date_label}: {date}</div>
        </div>
        '''

    def build_section(title, tasks):
        if not tasks:
            return ''
        formatted_tasks = []
        for task in tasks:
            props = task["properties"]
            draft_date_data = props.get("Draft Date", {}).get("date")
            due_date_data = props.get("Due Date", {}).get("date")
            draft_date_str = draft_date_data.get("start") if draft_date_data else None
            due_date_str = due_date_data.get("start") if due_date_data else None

            if draft_date_str and (not due_date_str or draft_date_str <= due_date_str):
                formatted_tasks.append(format_task(task, "Draft Date", draft_date_str))
            elif due_date_str:
                formatted_tasks.append(format_task(task, "Due Date", due_date_str))
            else:
                formatted_tasks.append(format_task(task, "No date set", "-"))
        return f'''
        <div class="section">
            <h2>{title}</h2>
            {''.join(formatted_tasks)}
        </div>
        '''

    if scope == "daily":
        logger.info("Sending daily notification")
        terminology = "Today"
        if len(assignments) <= 0 and len(exams) <= 0 and len(soon) <= 0:
            logger.info("No notifications for today")
            return
    else:
        logger.info("Sending weekly notification")
        terminology = "This Week"
        soon = []
        if len(assignments) <= 0 and len(exams) <= 0:
            logger.info("No notifications for this week")
            return

    sections_html = f'<strong>You have {len(assignments)+len(exams)+len(soon)} task(s) due {terminology.lower()}<strong>'
    sections_html += build_section(f"Assignments Due {terminology}", assignments)
    sections_html += build_section(f"Exams/Practicals {terminology}", exams)
    if scope == "daily":
        sections_html += build_section("Tasks Due Soon", soon)

    html_template = f'''<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <style>
    body {{
      font-family: Arial, sans-serif;
      color: #333;
      padding: 20px;
      background-color: #f9f9f9;
    }}
    h1 {{
      color: #2c3e50;
    }}
    h2 {{
      color: #2c3e50;
    }}
    .section {{
      margin-bottom: 30px;
      padding: 15px;
      background-color: #ffffff;
      border-radius: 8px;
      border: 1px solid #ddd;
    }}
    .task {{
      margin-bottom: 12px;
    }}
    .task-title {{
      font-weight: bold;
      font-size: 16px;
      margin-bottom: 2px;
    }}
    .task-date {{
      font-size: 14px;
      color: #555;
    }}
    a {{
      color: #2980b9;
      text-decoration: none;
    }}
  </style>
</head>
<body>
  <h1>Your {scope.capitalize()} Task Summary</h1>
  {sections_html}
</body>
</html>'''

    email(html_template,terminology)
    return

def sign_content(msg, cert_path, key_path):
    if not os.path.isfile(cert_path):
        raise FileNotFoundError(f"Certificate file not found at {cert_path}")
    if not os.path.isfile(key_path):
        raise FileNotFoundError(f"Private key file not found at {key_path}")

    with tempfile.NamedTemporaryFile(delete=False, mode="wb") as unsigned_file:
        unsigned_file.write(msg)
        unsigned_path = unsigned_file.name

    with tempfile.NamedTemporaryFile(delete=False, mode="rb") as signed_file:
        signed_path = signed_file.name

    try:
        subprocess.run([
            "openssl", "smime", "-sign",
            "-in", unsigned_path,
            "-signer", cert_path,
            "-inkey", key_path,
            "-out", signed_path,
            "-outform", "smime",
            "-nodetach",
            "-binary"
        ], check=True)

        with open(signed_path, "rb") as f:
            return f.read()

    finally:
        os.remove(unsigned_path)
        os.remove(signed_path)

def email(html,terminology):
    if not smimeParam:
        run_smime = True
    else:
        run_smime = False
    server = os.getenv("EMAIL_SERVER")
    port = int(os.getenv("EMAIL_PORT"))
    username = os.getenv("EMAIL_USER")
    password = os.getenv("EMAIL_PASS")
    cert_path = os.getenv("SMIME_CRT")
    key_path = os.getenv("SMIME_KEY")

    if not server or not port or not username or not password:
        logger.error("No email server configured. Please set up Environment variables")
        return

    if not cert_path or not key_path:
        logger.warning("No S/MIME details set up, skipping.")
        run_smime = False

    msg = EmailMessage()
    msg['Subject'] = f"Tasks Due {terminology}"
    msg["From"] = f"Task Reminders <{username}>"
    msg["To"] = os.getenv("RECIPIENT")

    text = "This email contains HTML content. Please enable HTML or view it in a HTML-Compatible client"

    try:
        with LoggingSMTP(server, port) as smtp:
            smtp.set_debuglevel(1)
            smtp.ehlo()
            smtp.starttls()
            smtp.ehlo()
            smtp.login(username, password)

            if run_smime:
                try:
                    # Step 1: Build the unsigned multipart/alternative body
                    alt = MIMEMultipart("alternative")
                    alt.attach(MIMEText(text, "plain"))
                    alt.attach(MIMEText(html, "html"))

                    # Serialize just this body
                    buf = BytesIO()
                    BytesGenerator(buf).flatten(alt)
                    unsigned_body_bytes = buf.getvalue()

                    # Step 2: Sign the content
                    signature = sign_content(unsigned_body_bytes, cert_path, key_path)

                    # Step 3: Create the multipart/signed wrapper
                    outer = EmailMessage()
                    outer["Subject"] = f"Tasks Due {terminology}"
                    outer["From"] = f"Task Reminders <{username}>"
                    outer["To"] = "lb29696@tas.qld.edu.au"
                    outer.set_type("multipart/signed")
                    outer.set_param("protocol", "application/x-pkcs7-signature")
                    outer.set_param("micalg", "sha-256")

                    # Attach signature
                    unsigned_msg = message_from_bytes(unsigned_body_bytes)
                    outer.attach(unsigned_msg)
                    outer.attach(MIMEApplication(signature, _subtype="pkcs7-signature", name="smime.p7s", disposition="attachment"))

                    smtp.send_message(outer)
                    logger.info("Signed email sent")

                except Exception as e:
                    logger.warning("Failed to sign email, sending unsigned instead")
                    logger.debug(e)
                    fallback = EmailMessage()
                    fallback["Subject"] = f"Tasks Due {terminology}"
                    fallback["From"] = f"Task Reminders <{username}>"
                    fallback["To"] = os.getenv("RECIPIENT")
                    fallback.set_content(text)
                    fallback.add_alternative(html, subtype="html")
                    smtp.send_message(fallback)
                    logger.info("Unsigned email sent")
            else:
                # S/MIME disabled
                msg = EmailMessage()
                msg["Subject"] = f"Tasks Due {terminology}"
                msg["From"] = f"Task Reminders <{username}>"
                msg["To"] = "lb29696@tas.qld.edu.au"
                msg.set_content(text)
                msg.add_alternative(html, subtype="html")
                smtp.send_message(msg)
                logger.info("Unsigned email sent")

    except Exception as e:
        logger.error("Unable to send email")
        logger.debug(e)

db = load_db()
refresh_database()

if devParam:
    reminders()
    logger.info("Sending reminders complete")
    input("Press Enter to send weekly summary...")
    weekly_summary()
    logger.info("Sending weekly summary complete")
    sys.exit(0)
else:
    schedule.every().hour.do(refresh_database)
    schedule.every().day.at("06:00", "Australia/Brisbane").do(reminders)
    schedule.every().week.do(weekly_summary)

while True:
    schedule.run_pending()
    time.sleep(1)
