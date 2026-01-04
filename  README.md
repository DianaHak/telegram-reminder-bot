# Telegram Reminder Bot (Production-style)

A Telegram bot for creating reminders using natural language input.

✅ Features
- Message-first and time-first parsing  
  Examples: `drink water in 10 minutes`, `tomorrow 14:00 call mom`
- Recurring reminders: daily / weekly / monthly
- Per-user timezone support (default: Asia/Yerevan)
- Inline snooze buttons + custom snooze time
- CSV export of reminders
- Missed reminder follow-up (if user takes no action after 2 hours)

## Tech
- Python
- python-telegram-bot (JobQueue)
- SQLite
- dateparser + pytz

## Run locally

### 1) Install
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
