# E-Bill Python MVP

Python/Flask starter application for the E-Bill project.

## Current features

- Store signup
- Username/email login
- Store/business information
- Dashboard
- Create a sale
- Multiple sale items
- Customer email
- Automatic email attempt after bill creation
- PDF bill generation
- View full bill
- Download PDF
- Resend email
- Daily/monthly sales counts
- 30-day expiration field on sales

## Run locally

### 1. Create a virtual environment

Windows:

```powershell
python -m venv .venv
.venv\Scripts\activate
```

### 2. Install packages

```powershell
pip install -r requirements.txt
```

### 3. Run

```powershell
python app.py
```

Open:

http://127.0.0.1:5000

## Email setup

The app uses SMTP. Set these environment variables before testing email:

```text
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_USERNAME=your-email@example.com
SMTP_PASSWORD=your-app-password
SENDER_EMAIL=your-email@example.com
```

For Gmail, use a Google App Password rather than your normal Google password.

On Render, put these values in the service's Environment Variables.

## Render

Push this project to GitHub, create a Render Web Service from the repository, and use:

Build command:

```text
pip install -r requirements.txt
```

Start command:

```text
gunicorn app:app
```

Then add the SMTP environment variables in Render.

## Important MVP note

SQLite is used here to make the first version easy to understand and run. The `/tmp` database configuration in render.yaml is suitable for a temporary demo, not permanent production business data. For a real deployed version, move the database to PostgreSQL before relying on the app for important records.
