import os
import sqlite3
import logging
from contextlib import contextmanager
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from flask import Flask, render_template, request, redirect, url_for, jsonify, flash, send_from_directory
from werkzeug.utils import secure_filename
from apscheduler.schedulers.background import BackgroundScheduler

try:
    from twilio.rest import Client
except ImportError:
    Client = None

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("MEDALERT_DATA_DIR", str(ROOT)))
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB = DATA_DIR / "medalert.db"
UPLOADS = DATA_DIR / "uploads"
UPLOADS.mkdir(parents=True, exist_ok=True)
UPLOADS.mkdir(exist_ok=True)
TZ = ZoneInfo(os.getenv("MEDALERT_TIMEZONE", "Asia/Kolkata"))
ALLOWED_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "change-this-development-key")
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024
scheduler = BackgroundScheduler(timezone=TZ)
logging.basicConfig(level=logging.INFO)


def now():
    return datetime.now(TZ)


@contextmanager
def connect():
    # Always close SQLite connections; the timeout gives concurrent jobs time to finish.
    db = sqlite3.connect(DB, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA busy_timeout=30000")
    try:
        with db:
            yield db
    finally:
        db.close()


def init_db():
    with connect() as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("""
            CREATE TABLE IF NOT EXISTS contacts (
                id INTEGER PRIMARY KEY CHECK(id=1),
                patient_name TEXT NOT NULL DEFAULT 'Patient',
                patient_phone TEXT NOT NULL DEFAULT '',
                caregiver_name TEXT NOT NULL DEFAULT 'Caregiver',
                caregiver_phone TEXT NOT NULL DEFAULT ''
            )
        """)
        db.execute("INSERT OR IGNORE INTO contacts(id) VALUES(1)")
        db.execute("""
            CREATE TABLE IF NOT EXISTS medicines (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                dosage TEXT NOT NULL,
                dose_time TEXT NOT NULL,
                start_date TEXT NOT NULL,
                end_date TEXT NOT NULL DEFAULT '',
                photo TEXT NOT NULL DEFAULT '',
                active INTEGER NOT NULL DEFAULT 1
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                medicine_id INTEGER NOT NULL,
                scheduled_for TEXT NOT NULL,
                created_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'Waiting',
                acknowledged_at TEXT NOT NULL DEFAULT '',
                escalated INTEGER NOT NULL DEFAULT 0,
                UNIQUE(medicine_id, scheduled_for),
                FOREIGN KEY(medicine_id) REFERENCES medicines(id)
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                reminder_id INTEGER,
                recipient TEXT NOT NULL,
                channel TEXT NOT NULL,
                stage TEXT NOT NULL,
                status TEXT NOT NULL,
                details TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            )
        """)


def log_notification(reminder_id, recipient, channel, stage, status, details=""):
    with connect() as db:
        db.execute("""
            INSERT INTO notifications
            (reminder_id, recipient, channel, stage, status, details, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (reminder_id, recipient, channel, stage, status, details,
              now().isoformat(timespec="seconds")))


def send_channel(reminder_id, phone, recipient, channel, stage, message):
    if not phone:
        log_notification(reminder_id, recipient, channel, stage, "Not configured",
                         "Phone number is missing")
        return

    sid = os.getenv("TWILIO_ACCOUNT_SID", "")
    token = os.getenv("TWILIO_AUTH_TOKEN", "")
    from_number = os.getenv("TWILIO_PHONE_NUMBER", "")

    if not (sid and token and from_number and Client):
        log_notification(reminder_id, recipient, channel, stage, "Simulation only",
                         "Configure Twilio credentials to send real notifications")
        return

    try:
        client = Client(sid, token)
        if channel == "SMS":
            result = client.messages.create(to=phone, from_=from_number, body=message)
        else:
            safe_message = (message.replace("&", "&amp;")
                                    .replace("<", "&lt;")
                                    .replace(">", "&gt;"))
            result = client.calls.create(
                to=phone,
                from_=from_number,
                twiml=f"<Response><Say>{safe_message}</Say></Response>"
            )
        log_notification(reminder_id, recipient, channel, stage, "Sent",
                         f"Provider reference: {result.sid}")
    except Exception as exc:
        logging.exception("Notification delivery failed")
        log_notification(reminder_id, recipient, channel, stage, "Failed", str(exc)[:250])


def notify_person(reminder_id, phone, recipient, stage, message):
    send_channel(reminder_id, phone, recipient, "SMS", stage, message)
    send_channel(reminder_id, phone, recipient, "Call", stage, message)


def due_job():
    current = now()
    today = current.date().isoformat()
    clock = current.strftime("%H:%M")
    scheduled_for = f"{today} {clock}"

    # Read and create reminder records in one short transaction.
    reminders_to_send = []
    with connect() as db:
        medicines = db.execute("""
            SELECT * FROM medicines
            WHERE active=1 AND dose_time=?
              AND start_date<=?
              AND (end_date='' OR end_date>=?)
        """, (clock, today, today)).fetchall()
        contacts_row = db.execute("SELECT * FROM contacts WHERE id=1").fetchone()
        contacts = dict(contacts_row) if contacts_row else {
            "patient_name": "Patient", "patient_phone": "",
            "caregiver_name": "Caregiver", "caregiver_phone": ""
        }

        for medicine in medicines:
            try:
                cursor = db.execute("""
                    INSERT INTO reminders(medicine_id, scheduled_for, created_at)
                    VALUES (?, ?, ?)
                """, (medicine["id"], scheduled_for,
                      current.isoformat(timespec="seconds")))
            except sqlite3.IntegrityError:
                # This medicine already has a reminder for this scheduled time.
                continue

            reminders_to_send.append((cursor.lastrowid, dict(medicine)))

    # IMPORTANT: send/log notifications only after the DB transaction is closed.
    for reminder_id, medicine in reminders_to_send:
        patient_message = (
            f"Hello {contacts['patient_name']}. It is time for "
            f"{medicine['name']} ({medicine['dosage']}). Please follow your prescription."
        )
        caregiver_message = (
            f"MedAlert reminder: {contacts['patient_name']} is due for "
            f"{medicine['name']} ({medicine['dosage']})."
        )
        notify_person(reminder_id, contacts["patient_phone"], "Patient",
                      "Initial reminder", patient_message)
        notify_person(reminder_id, contacts["caregiver_phone"], "Caregiver",
                      "Initial reminder", caregiver_message)


def escalation_job():
    current = now()
    reminders_to_escalate = []

    # Mark reminders as escalated in a short transaction.
    with connect() as db:
        pending = db.execute("""
            SELECT r.*, m.name, m.dosage, c.patient_name,
                   c.patient_phone, c.caregiver_phone
            FROM reminders r
            JOIN medicines m ON m.id=r.medicine_id
            JOIN contacts c ON c.id=1
            WHERE r.status='Waiting' AND r.acknowledged_at=''
              AND r.escalated=0
        """).fetchall()

        for reminder in pending:
            try:
                created = datetime.fromisoformat(reminder["created_at"])
            except ValueError:
                continue
            if current < created + timedelta(minutes=5):
                continue

            cursor = db.execute("""
                UPDATE reminders SET escalated=1, status='Escalated'
                WHERE id=? AND escalated=0 AND status='Waiting'
            """, (reminder["id"],))
            if cursor.rowcount:
                reminders_to_escalate.append(dict(reminder))

    # IMPORTANT: send/log notifications only after the DB transaction is closed.
    for reminder in reminders_to_escalate:
        patient_message = (
            f"Reminder again: it is time for {reminder['name']} "
            f"({reminder['dosage']}). Please check your schedule and acknowledge."
        )
        caregiver_message = (
            f"MedAlert alert: {reminder['patient_name']} has not acknowledged "
            f"the {reminder['name']} reminder after 5 minutes. Please check on them."
        )
        notify_person(reminder["id"], reminder["patient_phone"], "Patient",
                      "Repeat after 5 minutes", patient_message)
        notify_person(reminder["id"], reminder["caregiver_phone"], "Caregiver",
                      "Caregiver escalation", caregiver_message)


def start_scheduler():
    if not scheduler.running:
        scheduler.add_job(due_job, "interval", seconds=20,
                          id="medicine_due_check", replace_existing=True,
                          max_instances=1, coalesce=True)
        scheduler.add_job(escalation_job, "interval", seconds=20,
                          id="reminder_escalation", replace_existing=True,
                          max_instances=1, coalesce=True)
        scheduler.start()


@app.get("/")
def home():
    with connect() as db:
        medicines = db.execute("""
            SELECT * FROM medicines WHERE active=1 ORDER BY dose_time
        """).fetchall()
        reminders = db.execute("""
            SELECT r.*, m.name, m.dosage FROM reminders r
            JOIN medicines m ON m.id=r.medicine_id
            WHERE r.status IN ('Waiting','Escalated')
            ORDER BY r.id DESC LIMIT 10
        """).fetchall()
        notifications = db.execute("""
            SELECT * FROM notifications ORDER BY id DESC LIMIT 12
        """).fetchall()
        contacts = db.execute("SELECT * FROM contacts WHERE id=1").fetchone()
        total = db.execute("SELECT COUNT(*) FROM reminders").fetchone()[0]
        acknowledged = db.execute("""
            SELECT COUNT(*) FROM reminders WHERE status='Acknowledged'
        """).fetchone()[0]
        pending = db.execute("""
            SELECT COUNT(*) FROM reminders WHERE status IN ('Waiting','Escalated')
        """).fetchone()[0]

    return render_template(
        "index.html",
        medicines=medicines,
        reminders=reminders,
        notifications=notifications,
        contacts=contacts,
        total=total,
        acknowledged=acknowledged,
        pending=pending,
        today=now().strftime("%A, %d %B %Y")
    )


@app.post("/contacts")
def save_contacts():
    with connect() as db:
        db.execute("""
            UPDATE contacts
            SET patient_name=?, patient_phone=?, caregiver_name=?, caregiver_phone=?
            WHERE id=1
        """, (
            request.form.get("patient_name", "").strip(),
            request.form.get("patient_phone", "").strip(),
            request.form.get("caregiver_name", "").strip(),
            request.form.get("caregiver_phone", "").strip()
        ))
    flash("Patient and caregiver details saved.", "success")
    return redirect(url_for("home") + "#contacts")


@app.post("/medicines")
def add_medicine():
    name = request.form.get("name", "").strip()
    dosage = request.form.get("dosage", "").strip()
    dose_time = request.form.get("dose_time", "").strip()
    start_date = request.form.get("start_date") or now().date().isoformat()
    end_date = request.form.get("end_date", "").strip()
    photo = request.files.get("photo")
    photo_name = ""

    if not name or not dosage or not dose_time:
        flash("Enter medicine name, dosage and reminder time.", "error")
        return redirect(url_for("home") + "#medicines")

    if end_date and end_date < start_date:
        flash("End date cannot be earlier than start date.", "error")
        return redirect(url_for("home") + "#medicines")

    if photo and photo.filename:
        extension = photo.filename.rsplit(".", 1)[-1].lower()
        if extension not in ALLOWED_EXTENSIONS:
            flash("Medicine photo must be JPG, PNG or WEBP.", "error")
            return redirect(url_for("home") + "#medicines")
        photo_name = (
            datetime.now().strftime("%Y%m%d%H%M%S%f") + "_" +
            secure_filename(photo.filename)
        )
        photo.save(UPLOADS / photo_name)

    with connect() as db:
        db.execute("""
            INSERT INTO medicines(name, dosage, dose_time, start_date, end_date, photo)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (name, dosage, dose_time, start_date, end_date, photo_name))

    flash("Medicine reminder added.", "success")
    return redirect(url_for("home") + "#medicines")


@app.post("/medicines/<int:medicine_id>/delete")
def delete_medicine(medicine_id):
    with connect() as db:
        db.execute("UPDATE medicines SET active=0 WHERE id=?", (medicine_id,))
    flash("Medicine reminder removed.", "success")
    return redirect(url_for("home") + "#medicines")


@app.post("/reminders/<int:reminder_id>/acknowledge")
def acknowledge(reminder_id):
    with connect() as db:
        db.execute("""
            UPDATE reminders
            SET acknowledged_at=?, status='Acknowledged'
            WHERE id=? AND status IN ('Waiting','Escalated')
        """, (now().isoformat(timespec="seconds"), reminder_id))
    flash("Reminder acknowledged. This does not confirm that medicine was swallowed.",
          "success")
    return redirect(url_for("home") + "#reminders")


@app.get("/uploads/<path:filename>")
def uploaded_photo(filename):
    return send_from_directory(UPLOADS, filename)


@app.get("/api/health")
def api_health():
    return jsonify(status="online", application="MedAlert", timezone=str(TZ))


@app.get("/api/medicines")
def api_medicines():
    with connect() as db:
        rows = db.execute("""
            SELECT id, name, dosage, dose_time, start_date, end_date, photo
            FROM medicines WHERE active=1 ORDER BY dose_time
        """).fetchall()
    return jsonify([dict(row) for row in rows])


init_db()
start_scheduler()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False, use_reloader=False)
