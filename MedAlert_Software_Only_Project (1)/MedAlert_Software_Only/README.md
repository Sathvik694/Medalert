# MedAlert — Software-Only Medicine Reminder Website

MedAlert is a **software-only** medicine reminder web application. It has no physical-device integration; all reminders, schedules, acknowledgements and
notifications are handled by the website and its software services.

## Features

- Add and remove medicine reminders.
- Store medicine name, dosage, reminder time, start date and optional end date.
- Upload a medicine photo (JPG, PNG or WEBP; maximum 5 MB).
- Save patient and caregiver names and phone numbers.
- At the scheduled time, send SMS and an automated voice call to both patient and caregiver.
- Let the patient acknowledge a reminder on the website.
- If no acknowledgement is recorded within 5 minutes, repeat the patient alert and send a caregiver escalation alert.
- View reminder status and notification delivery logs.
- SQLite database; Flask backend; APScheduler reminder jobs.

## Requirements

- Python 3.10 or newer
- Internet connection for real Twilio notifications
- A Twilio account and a Twilio phone number enabled for SMS and voice calls

## Run on Windows

Open Command Prompt in this project folder:

```bat
py -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

Open http://127.0.0.1:5000 in your browser. Keep the computer and application running
for scheduled reminders.

## Enable real SMS and calls

Without Twilio credentials, the application records notification attempts as
**Simulation only**; it does not send real messages or calls.

Set these environment variables in the same Command Prompt before starting the app:

```bat
set TWILIO_ACCOUNT_SID=your_account_sid
set TWILIO_AUTH_TOKEN=your_auth_token
set TWILIO_PHONE_NUMBER=+1xxxxxxxxxx
set FLASK_SECRET_KEY=replace-with-a-long-random-secret
python app.py
```

Use a Twilio number enabled for SMS and voice. Trial accounts may require verified
recipient numbers. Provider fees, regional rules and trial restrictions may apply.
Never share your Twilio auth token or commit it to source control.

## Reminder behaviour

1. APScheduler checks for medicines due at the current minute.
2. The initial SMS and voice call are sent to patient and caregiver.
3. The patient can acknowledge the reminder in the website.
4. If it remains unacknowledged for 5 minutes, the app repeats the patient alert
   and sends an escalation SMS and call to the caregiver.

The current scheduler supports **daily reminders** only. The app runs locally and
the scheduler stops when the process or computer stops. For a real deployment,
use a reliable hosted worker/scheduler, authentication, HTTPS, secure secret storage,
database backups and monitoring.

## Software-only scope and limitations

- No hardware is part of this project.
- No medicine is physically dispensed or measured.
- No sensor or device confirms that medicine was collected or swallowed.
- A website acknowledgement is only a user response; it is not proof of ingestion.
- This is an educational prototype, not a medical device or substitute for medical advice.
- Do not expose this prototype publicly without adding authentication, authorization,
  input/security hardening and production deployment controls.
