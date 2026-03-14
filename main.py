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
from dataclasses import dataclass
from typing import Optional
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
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from notion_client import Client

try:
    from systemd import journal
except ModuleNotFoundError:
    pass
from datetime import date, timedelta, datetime
from dotenv import load_dotenv
from requests.exceptions import SSLError

@dataclass
class Assignment:
    id: int
    activity_assign_id: int
    activity_uuid: str
    assign_name: str
    object_name: str
    status_desc: str
    student_status_desc: str
    student_status_desc_pending: str
    dt_publish_start: str
    dt_publish_finish: str
    dt_publish_finish_display: str
    due_date_desc: str
    dt_draft: Optional[str]
    dt_draft_display: Optional[str]
    draft_file_last_submit_date: Optional[str]
    draft_overdue_flg: str
    final_file_last_submit_date: Optional[str]
    extension_flg: str
    homework_flg: str
    isexempt_flg: str
    due_today_flg: str
    overdue_flg: str

    @staticmethod
    def from_json(data: dict) -> "Assignment":
        # map JSON keys to dataclass fields
        return Assignment(
            id=data["id"],
            activity_assign_id=data["ACTIVITY_ASSIGN_ID"],
            activity_uuid=data["ACTIVITY_UUID"],
            assign_name=data["ASSIGN_NAME"],
            object_name=data["object_name"],
            status_desc=data["STATUS_DESC"],
            student_status_desc=data["STUDENT_STATUS_DESC"],
            student_status_desc_pending=data["STUDENT_STATUS_DESC_PENDING"],
            dt_publish_start=data["DT_PUBLISH_START"],
            dt_publish_finish=data["DT_PUBLISH_FINISH"],
            dt_publish_finish_display=data["DT_PUBLISH_FINISH_DISPLAY"],
            due_date_desc=data["DUE_DATE_DESC"],
            dt_draft=data.get("DT_DRAFT"),
            dt_draft_display=data.get("DT_DRAFT_DISPLAY"),
            draft_file_last_submit_date=data.get("DRAFT_FILE_LAST_SUBMIT_DATE"),
            draft_overdue_flg=data["DRAFT_OVERDUE_FLG"],
            final_file_last_submit_date=data.get("FINAL_FILE_LAST_SUBMIT_DATE"),
            extension_flg=data["extension_flg"],
            homework_flg=data["homework_flg"],
            isexempt_flg=data["isexempt_flg"],
            due_today_flg=data["DUE_TODAY_FLG"],
            overdue_flg=data["OVERDUE_FLG"]
        )

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
    logger.critical('INTEGRATION_SECRET environment variable not set.') # TODO: Update this to not fail on missing creds
    exit(1)

# Load default API parameters
Base_URL = 'https://api.notion.com/v1'
headers = {'Authorization': f'Bearer {INTEGRATION_SECRET}','Notion-Version': '2022-06-28','Content-Type': 'application/json'}
notion = Client(auth=INTEGRATION_SECRET)
db_id = os.getenv("DB_ID") # DEPRECATED - Use DS_ID instead
ds_id = os.getenv("DS_ID")

# Functions

def tass_to_iso(tass_date):
    if not tass_date:
        return None
    try:
        # Format: "2026-05-22 09:00:00.0"
        return datetime.strptime(tass_date, "%Y-%m-%d %H:%M:%S.%f").isoformat()
    except ValueError:
        try:
            # Format: "22/05/2026 at 9:00am"
            return datetime.strptime(tass_date, "%d/%m/%Y at %I:%M%p").isoformat()
        except ValueError:
            print(f"Unknown TASS date format: {tass_date}")
            return None

def refresh_database(): # TODO: Update this to use the new Notion Client
    global db
    filter_body = {
        "filter": {
            "property": "Archived",
            "checkbox": {
                "equals": False
            }
        }
    }
    try:
        logger.info("Refreshing database")
        if not db_id:
            logger.error("DB_ID environment variable not set.")
            raise ValueError("DB_ID environment variable not set.")
        r = requests.post(
            f'{Base_URL}/databases/{db_id}/query',
            headers=headers,
            json=filter_body,
            verify=True
        )
        if not r.ok:
            logger.error("Error while receiving new datasets from notion")
            logger.debug(f"{r.status_code}: {r.reason}")
            logger.debug(f"{r.text}")

    except SSLError:
        try:
            r = requests.post(
                f'{Base_URL}/databases/{db_id}/query',
                headers=headers,
                json=filter_body,
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
                                                                                       "").lower() == "not submitted (draft)"
                ) or (
                        (due := page["properties"].get("Due Date", {}).get("date", {}))
                        and due.get("start") == today
                        and page["properties"].get("Status", {}).get("status", {}).get("name", "").lower() in [
                            "not submitted (draft)", "not submitted (final)"]
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
                and page["properties"].get("Status", {}).get("status", {}).get("name", "").lower() == "not submitted (draft)"
            ) or (
                (due := page["properties"].get("Due Date", {}).get("date", {}))
                and day_1 <= due.get("start", "") <= day_2
                and page["properties"].get("Status", {}).get("status", {}).get("name", "").lower() in [
                    "not submitted (draft)", "not submitted (final)"]
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
                and page["properties"].get("Status", {}).get("status", {}).get("name", "").lower() == "not submitted (final)"
            ) or (
                (due := page["properties"].get("Due Date", {}).get("date", {}))
                and today.isoformat() <= due.get("start", "") <= week_later.isoformat()
                and page["properties"].get("Status", {}).get("status", {}).get("name", "").lower() in [
                    "not submitted (draft)", "not submitted (final)"]
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

def load_assignments():
    TASS_USER = os.getenv("TASS_USER")
    TASS_PASS = os.getenv("TASS_PASS")
    if not TASS_USER or not TASS_PASS:
        logger.error("TASS_USER and TASS_PASS environment variables must be set to load assignments from Student Cafe")
        return
    # Scrapes assignments from Student Cafe and adds them to the remote db
    try:
        driver = webdriver.Chrome()
        driver.get("https://alpha.tas.qld.edu.au/studentcafe/login.cfm")
        logger.debug("Navigated to login page")
        email_input = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.ID, "i0116"))
        )
        logger.debug("Email element found")
        email_input.send_keys(TASS_USER)
        WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable((By.XPATH, "//input[@type='submit' and @value='Next']"))
        ).click()
        logger.debug("Submitted email")
        password_input = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.ID, "i0118"))
        )
        logger.debug("Password element found")
        password_input.send_keys(TASS_PASS)
        WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable((By.XPATH, "//input[@type='submit' and @value='Sign in']"))
        ).click()
        logger.debug("Submitted password")
        WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable((By.XPATH, "//input[@type='button' and @value='No']"))
        ).click()
        logger.debug("Clicked 'No' on stay signed in prompt")

        result = driver.execute_async_script("""
const callback = arguments[0];

fetch("https://alpha.tas.qld.edu.au/studentcafe/remote-json.cfm?do=studentportal.activities.main.lmsactivities.grid", {
    credentials: "include",
    headers: {
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest"
    },
    body: "lmsclass=&assignmentstatus=&assign_year=2026&topicsubscribe=activity.details,activity.onlinetest",
    method: "POST"
})
.then(r => r.json())
.then(data => callback(data))
.catch(err => callback({"error": err.toString()}));
""")
        logger.info("Received response from Student Cafe")
        logger.debug(f"Response: {result}")
        driver.close()
        update_remote(result.get("data", []))
        if "error" in result:
            raise Exception(f"Error fetching assignments: {result['error']}")
    except Exception as e:
        logger.error("Error while loading assignments from Student Cafe")
        logger.debug(e)
        return

def update_remote(assignments_list):
    logger.debug("Run update remote")
    logger.debug(f"Received assignments_list: {assignments_list}")
    for assignment_data in assignments_list:
        logger.debug("running for loop")
        assignment_obj = Assignment.from_json(assignment_data)
        upsert_assignment(assignment_obj)

def upsert_assignment(assignment):
    logger.debug(f"Upserting assignment {assignment.object_name} with Activity Assign ID {assignment.activity_assign_id}")
    if not ds_id:
        logger.error("DS_ID environment variable not set.")
        raise ValueError("DS_ID environment variable not set.")
    # Step 1: Search for existing page with Activity Assign ID
    query = notion.data_sources.query(
        **{
            "data_source_id": ds_id,
            "filter": {
                "property": "Activity Assign ID",
                "number": {"equals": assignment.activity_assign_id}
            }
        }
    )
    
    properties_payload = {
        "Task Name": {"title": [{"text": {"content": assignment.object_name}}]},
        "Activity Assign ID": {"number": assignment.activity_assign_id},
        "Draft Date": {"date": {"start": tass_to_iso(assignment.dt_draft)} if assignment.dt_draft else None},
        "Due Date": {"date": {"start": tass_to_iso(assignment.dt_publish_finish)} if assignment.dt_publish_finish else None},
        "Task Type": {"select": {"name": "Exam" if "exam" in assignment.object_name.lower() or "folio" in assignment.object_name.lower() else "Practical" if "practical" in assignment.object_name.lower() else "Assignment"}},
        "Status": {"status": {"name": assignment.student_status_desc}},
        "Results Release": {"date": {"start": tass_to_iso(assignment.dt_publish_finish_display)} if assignment.dt_publish_finish_display else None},
        # TODO: If I want to get results per activity im going to have to scrape https://alpha.tas.qld.edu.au/studentcafe/remote-html.cfm?do=studentportal.activities.main.lmsActivities.detail which has assign id in the payload. Could also help with activity types idk 
    }

    # Step 2: Update or create
    if query["results"]:
        logger.debug(f"Found existing page with ID {query['results'][0]['id']} for Activity Assign ID {assignment.activity_assign_id}, updating it")
        page_id = query["results"][0]["id"]
        notion.pages.update(page_id=page_id, properties=properties_payload)
        print(f"Updated page {page_id} for Activity Assign ID {assignment.activity_assign_id}")
    else:
        logger.debug(f"No existing page found for Activity Assign ID {assignment.activity_assign_id}, creating new page")
        notion.pages.create(parent={"data_source_id": ds_id}, properties=properties_payload)
        print(f"Created new page for Activity Assign ID {assignment.activity_assign_id}")

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
                    outer["To"] = os.getenv("RECIPIENT")
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
                msg["To"] = os.getenv("RECIPIENT")
                msg.set_content(text)
                msg.add_alternative(html, subtype="html")
                smtp.send_message(msg)
                logger.info("Unsigned email sent")

    except Exception as e:
        logger.error("Unable to send email")
        logger.debug(e)

db = load_db()

if devParam:
    logger.info("Entering development mode")
    if input("Press Enter to update remote db from Notion... (s to skip) (UNSTABLE)").lower() != "s":
        load_assignments()
        logger.info("Database update complete")
    if input("Press Enter to refresh database... (s to skip)").lower() != "s":
        refresh_database()
        logger.info("Database refresh complete")
    if input("Press Enter to send daily reminders... (s to skip)").lower() != "s":
        reminders()
        logger.info("Sending reminders complete")
    if input("Press Enter to send weekly summary... (s to skip)").lower() != "s":
        weekly_summary()
        logger.info("Sending weekly summary complete")
    sys.exit(0)
else:
    refresh_database()
    schedule.every().hour.do(refresh_database)
    schedule.every().day.at("06:00", "Australia/Brisbane").do(reminders)
    schedule.every().week.do(weekly_summary)

while True:
    schedule.run_pending()
    time.sleep(1)
