#!/usr/bin/env python3
"""
Telegram reminder bot with:
- Message-first input ("drink water in 10 minutes", "take vitamins daily at 09:00")
- Also supports time-first ("tomorrow 14:00 call mom", "daily 09:00 take vitamins")
- One-off + daily / weekly / monthly recurrence
- Upcoming-only /list
- Snooze actions on reminder:
    Row 1: 5m, 15m, 30m, 1h, 3h, 1d
    Row 2: Enter time, edit, done
- Per-user timezone (default Asia/Yerevan)
- Export to CSV (/export)
- /users to inspect last_seen

Missed follow-up:
- If user does NOTHING with the reminder (no snooze / no done / no edit),
  send a follow-up message after 2 hours:
    "⚠️ missed 2 hours ago (at <time>)"
    "📝 <text>"

Time format logic (for user-facing messages like "added at" and snoozing):
- If same day: only "HH:MM"
- If next day: "tomorrow HH:MM"
- Otherwise: "DD-MM-YYYY HH:MM"

Recurring labels:
- (daily), (weekly), (monthly) are shown in user-facing messages,
  but not stored in the DB text itself.

Text cleaning:
- Phrases like "remind me to take vitamins" -> "take vitamins"
"""

import os
import csv
import logging
import sqlite3
from datetime import datetime, timezone, timedelta
from io import StringIO
from typing import Optional, Tuple
import calendar
import re

import dateparser
import pytz
from html import escape

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    InputFile,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    CallbackQueryHandler,
    JobQueue,
    filters,
)

# ------------------ Configuration ------------------
DB = os.getenv("DB_PATH", "reminders.db")
DEFAULT_TZ = "Asia/Yerevan"
LOG_LEVEL = logging.INFO

logging.basicConfig(level=LOG_LEVEL)
logger = logging.getLogger(__name__)

# For JobQueue naming
JOB_NAME_TEMPLATE = "rem_{}"       # reminder job
FOLLOW_NAME_TEMPLATE = "follow_{}" # follow-up job


# ------------------ DB helpers ------------------

def init_db():
    conn = sqlite3.connect(DB)
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            tz TEXT,
            last_seen TEXT
        )
        """
    )

    # Add columns if missing (safe)
    try:
        cur.execute("ALTER TABLE users ADD COLUMN last_seen TEXT")
    except sqlite3.OperationalError:
        pass

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            user_id INTEGER,
            text TEXT,
            run_at TEXT, -- ISO UTC
            recurrence TEXT, -- NULL, 'daily', 'weekly', 'monthly'
            recurrence_detail TEXT,
            active INTEGER DEFAULT 1
        )
        """
    )

    # Follow-up columns (safe migration)
    try:
        cur.execute("ALTER TABLE reminders ADD COLUMN last_sent_at TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        cur.execute("ALTER TABLE reminders ADD COLUMN last_message_id INTEGER")
    except sqlite3.OperationalError:
        pass
    try:
        cur.execute("ALTER TABLE reminders ADD COLUMN pending_followup INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass

    conn.commit()
    conn.close()


def update_last_seen(user_id: int):
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    now_iso = datetime.now(timezone.utc).isoformat()
    cur.execute(
        """
        INSERT INTO users (user_id, last_seen)
        VALUES (?, ?)
        ON CONFLICT(user_id) DO UPDATE SET last_seen=excluded.last_seen
        """,
        (user_id, now_iso),
    )
    conn.commit()
    conn.close()


def get_user_tz(user_id: int) -> str:
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute("SELECT tz FROM users WHERE user_id=?", (user_id,))
    r = cur.fetchone()
    conn.close()
    return r[0] if r and r[0] else DEFAULT_TZ


def set_user_tz(user_id: int, tz_name: str):
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO users (user_id, tz)
        VALUES (?, ?)
        ON CONFLICT(user_id) DO UPDATE SET tz=excluded.tz
        """,
        (user_id, tz_name),
    )
    conn.commit()
    conn.close()


def mark_followup_pending(rid: int, sent_at_iso: str, message_id: Optional[int]):
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute(
        "UPDATE reminders SET last_sent_at=?, last_message_id=?, pending_followup=1 WHERE id=?",
        (sent_at_iso, message_id, rid),
    )
    conn.commit()
    conn.close()


def mark_followup_cleared(rid: int):
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute(
        "UPDATE reminders SET pending_followup=0 WHERE id=?",
        (rid,),
    )
    conn.commit()
    conn.close()


def get_followup_state(rid: int) -> Tuple[Optional[str], int]:
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute("SELECT last_sent_at, pending_followup FROM reminders WHERE id=?", (rid,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None, 0
    return row[0], int(row[1] or 0)


def cancel_jobs_by_name(job_queue: JobQueue, name: str):
    for job in job_queue.get_jobs_by_name(name):
        job.schedule_removal()


def add_reminder(
    chat_id: int,
    user_id: int,
    text: str,
    run_at_iso: str,
    recurrence: Optional[str] = None,
    recurrence_detail: Optional[str] = None,
) -> int:
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO reminders (chat_id,user_id,text,run_at,recurrence,recurrence_detail)
        VALUES (?,?,?,?,?,?)
        """,
        (chat_id, user_id, text, run_at_iso, recurrence, recurrence_detail),
    )
    conn.commit()
    rid = cur.lastrowid
    conn.close()
    return rid


def update_reminder(rid: int, **kwargs):
    if not kwargs:
        return
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    parts = []
    vals = []
    for k, v in kwargs.items():
        parts.append(f"{k}=?")
        vals.append(v)
    vals.append(rid)
    cur.execute(f"UPDATE reminders SET {', '.join(parts)} WHERE id=?", tuple(vals))
    conn.commit()
    conn.close()


def delete_reminder(rid: int) -> bool:
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute("DELETE FROM reminders WHERE id=?", (rid,))
    changed = cur.rowcount
    conn.commit()
    conn.close()
    return bool(changed)


def get_reminder(rid: int):
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, chat_id, user_id, text, run_at, recurrence,
               recurrence_detail, active
        FROM reminders
        WHERE id=?
        """,
        (rid,),
    )
    r = cur.fetchone()
    conn.close()
    return r


def list_reminders(user_id: int):
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, text, run_at, recurrence, recurrence_detail, active
        FROM reminders
        WHERE user_id=?
        ORDER BY run_at
        """,
        (user_id,),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


# ------------------ Time + recurrence helpers ------------------

def parse_time_human(text: str, user_tz_name: str) -> Optional[datetime]:
    """
    Parse user input like '22:24', 'in 2 hours', 'tomorrow 9pm'
    using the user's timezone, then convert result to UTC.
    """
    try:
        user_tz = pytz.timezone(user_tz_name)
    except Exception:
        user_tz = pytz.timezone(DEFAULT_TZ)

    settings = {
        "RETURN_AS_TIMEZONE_AWARE": True,
        "PREFER_DATES_FROM": "future",
        "TIMEZONE": user_tz.zone,
    }

    dt = dateparser.parse(text, settings=settings)
    if dt is None:
        return None

    if dt.tzinfo is None:
        dt = user_tz.localize(dt)

    return dt.astimezone(timezone.utc)


def format_dt_for_user(dt_utc_iso: str, user_tz_name: str) -> str:
    """Full date (DD-MM-YYYY HH:MM) for things like /list."""
    dt = dateparser.parse(
        dt_utc_iso,
        settings={"RETURN_AS_TIMEZONE_AWARE": True, "TO_TIMEZONE": "UTC"},
    )
    if not dt:
        return dt_utc_iso
    user_tz = pytz.timezone(user_tz_name)
    local_dt = dt.astimezone(user_tz)
    return local_dt.strftime("%d-%m-%Y %H:%M")


def format_when_for_user(dt_utc_iso: str, user_tz_name: str) -> str:
    """
    Smart formatting for user-facing times:
    - same day: 'HH:MM'
    - next day: 'tomorrow HH:MM'
    - other: 'DD-MM-YYYY HH:MM'
    """
    dt = dateparser.parse(
        dt_utc_iso,
        settings={"RETURN_AS_TIMEZONE_AWARE": True, "TO_TIMEZONE": "UTC"},
    )
    if not dt:
        return dt_utc_iso

    user_tz = pytz.timezone(user_tz_name)
    local_dt = dt.astimezone(user_tz)
    now_local = datetime.now(user_tz)

    local_date = local_dt.date()
    today = now_local.date()
    tomorrow = today + timedelta(days=1)

    time_part = local_dt.strftime("%H:%M")

    if local_date == today:
        return time_part
    elif local_date == tomorrow:
        return f"tomorrow {time_part}"
    else:
        return local_dt.strftime("%d-%m-%Y %H:%M")


def add_months(dt, months=1):
    """Add N months to a timezone-aware datetime."""
    month = dt.month - 1 + months
    year = dt.year + month // 12
    month = month % 12 + 1
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


def recurrence_suffix(recurrence: Optional[str]) -> str:
    """Return ' (daily)' / ' (weekly)' / ' (monthly)' or ''."""
    if recurrence == "daily":
        return " (daily)"
    if recurrence == "weekly":
        return " (weekly)"
    if recurrence == "monthly":
        return " (monthly)"
    return ""


# ------------------ Text cleaning ------------------

REMIND_ME_PATTERN = re.compile(
    r"^(please\s+)?(remind me to|remind me|напомни мне|напомни)\s+",
    re.IGNORECASE,
)

def clean_message_text(msg: str) -> str:
    stripped = msg.strip()
    cleaned = REMIND_ME_PATTERN.sub("", stripped)
    return cleaned if cleaned else stripped


# ------------------ Snooze keyboard builders ------------------

def build_snooze_keyboard(rid: int) -> InlineKeyboardMarkup:
    """
    Always show ONLY 'done' (no activate).
    If reminder is already inactive, show 'done ✅' as a no-op.
    """
    rem = get_reminder(rid)
    active = rem[7] if rem else 1

    if active:
        done_btn = InlineKeyboardButton("done", callback_data=f"done:{rid}")
    else:
        done_btn = InlineKeyboardButton("done ✅", callback_data="noop")

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("5m", callback_data=f"snooze:{rid}:5m"),
                InlineKeyboardButton("15m", callback_data=f"snooze:{rid}:15m"),
                InlineKeyboardButton("30m", callback_data=f"snooze:{rid}:30m"),
                InlineKeyboardButton("1h", callback_data=f"snooze:{rid}:1h"),
                InlineKeyboardButton("3h", callback_data=f"snooze:{rid}:3h"),
                InlineKeyboardButton("1d", callback_data=f"snooze:{rid}:1d"),
            ],
            [
                InlineKeyboardButton("enter time", callback_data=f"snooze_custom:{rid}"),
                InlineKeyboardButton("edit", callback_data=f"edit_text:{rid}"),
                done_btn,
            ],
        ]
    )


# ------------------ Follow-up job ------------------

async def followup_job(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data or {}
    rid = data.get("rid")
    expected_sent_at = data.get("sent_at")
    if not rid or not expected_sent_at:
        return

    rem = get_reminder(rid)
    if not rem:
        return

    _, chat_id, user_id, text, run_at_s, recurrence, recurrence_detail, active = rem

    last_sent_at, pending = get_followup_state(rid)
    if not pending:
        return

    # If reminder has been re-sent since, don't follow-up old one
    if last_sent_at != expected_sent_at:
        return

    user_tz_name = get_user_tz(user_id)
    pretty_sent = format_when_for_user(last_sent_at, user_tz_name) if last_sent_at else ""
    rec_label = recurrence_suffix(recurrence)

    body = f"⚠️ missed 2 hours ago (at {pretty_sent})\n📝 {text}{rec_label}"

    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=body,
            reply_markup=build_snooze_keyboard(rid),
        )
    except Exception:
        logger.exception("Failed to send follow-up for %s", rid)
        return


def schedule_followup(app, rid: int, sent_at_iso: str):
    job_queue: JobQueue = app.job_queue
    name = FOLLOW_NAME_TEMPLATE.format(rid)
    cancel_jobs_by_name(job_queue, name)
    job_queue.run_once(
        followup_job,
        when=timedelta(hours=2),
        name=name,
        data={"rid": rid, "sent_at": sent_at_iso},
    )


def clear_followup_on_interaction(app, rid: int):
    mark_followup_cleared(rid)
    cancel_jobs_by_name(app.job_queue, FOLLOW_NAME_TEMPLATE.format(rid))


# ------------------ JobQueue scheduling ------------------

async def reminder_job(context: ContextTypes.DEFAULT_TYPE):
    """Called by JobQueue when a reminder is due."""
    data = context.job.data
    rid = data["rid"]
    logger.info("reminder_job fired for rid=%s", rid)

    rem = get_reminder(rid)
    if not rem:
        logger.info("reminder_job: reminder %s not found in DB", rid)
        return

    _, chat_id, user_id, text, run_at_s, recurrence, recurrence_detail, active = rem

    if not active:
        return

    user_tz_name = get_user_tz(user_id)

    run_dt = dateparser.parse(
        run_at_s,
        settings={"RETURN_AS_TIMEZONE_AWARE": True, "TO_TIMEZONE": "UTC"},
    )
    now_utc = datetime.now(timezone.utc)
    now_iso = now_utc.isoformat()

    pretty_now = format_when_for_user(now_iso, user_tz_name)
    scheduled_pretty = format_when_for_user(run_at_s, user_tz_name) if run_dt else pretty_now
    rec_label = recurrence_suffix(recurrence)

    overdue = False
    if run_dt and (now_utc - run_dt > timedelta(minutes=2)):
        overdue = True

    if overdue:
        body = (
            f"📝 {text}{rec_label}\n"
            f"⚠️ this was scheduled for {scheduled_pretty}\n"
            f"⏰ sending now at {pretty_now}\n\n"
            "Remind me again in:"
        )
    else:
        body = f"📝 {text}{rec_label}\n⏰ {pretty_now}\n\nRemind me again in:"

    try:
        msg = await context.bot.send_message(
            chat_id=chat_id,
            text=body,
            reply_markup=build_snooze_keyboard(rid),
        )
    except Exception:
        logger.exception("Failed to send reminder %s", rid)
        return

    # Mark follow-up pending + schedule follow-up in 2 hours
    sent_at_iso = datetime.now(timezone.utc).isoformat()
    mark_followup_pending(rid, sent_at_iso, msg.message_id)
    schedule_followup(context.application, rid, sent_at_iso)

    # Recurrence handling
    if not run_dt:
        return

    user_tz = pytz.timezone(user_tz_name)
    local = run_dt.astimezone(user_tz)

    if recurrence == "daily":
        next_local = local + timedelta(days=1)
    elif recurrence == "weekly":
        next_local = local + timedelta(weeks=1)
    elif recurrence == "monthly":
        next_local = add_months(local, 1)
    else:
        update_reminder(rid, active=0)
        return

    next_utc = next_local.astimezone(timezone.utc)
    update_reminder(rid, run_at=next_utc.isoformat(), active=1)
    schedule_job(context.application, rid, chat_id, text, next_utc)


def schedule_job(app, rid: int, chat_id: int, text: str, run_at_dt: datetime):
    """Schedule a reminder via JobQueue at an exact UTC time."""
    job_queue: JobQueue = app.job_queue
    job_name = JOB_NAME_TEMPLATE.format(rid)

    cancel_jobs_by_name(job_queue, job_name)

    run_at_utc = run_at_dt.astimezone(timezone.utc) if run_at_dt.tzinfo else run_at_dt.replace(tzinfo=timezone.utc)
    now_utc = datetime.now(timezone.utc)
    if run_at_utc <= now_utc:
        logger.info("Not scheduling %s; time %s is in the past.", job_name, run_at_utc)
        return

    run_at_naive = run_at_utc.replace(tzinfo=None)

    job_queue.run_once(
        reminder_job,
        when=run_at_naive,
        name=job_name,
        data={"rid": rid},
    )
    logger.info("Scheduled %s at %s (UTC)", job_name, run_at_naive)


def load_jobs(app):
    """Re-schedule active reminders from DB on startup."""
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute("SELECT id, chat_id, text, run_at, active FROM reminders WHERE active=1")
    now = datetime.now(timezone.utc)
    for rid, chat_id, text, run_at, active in cur.fetchall():
        dt = dateparser.parse(run_at, settings={"RETURN_AS_TIMEZONE_AWARE": True, "TO_TIMEZONE": "UTC"})
        if dt and dt > now:
            schedule_job(app, rid, chat_id, text, dt)
    conn.close()


# ------------------ Telegram handlers ------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    update_last_seen(update.effective_user.id)
    keyboard = ReplyKeyboardMarkup([["Add", "List", "Export"], ["/settz"]], resize_keyboard=True)
    await update.message.reply_text(
        "Hi, I can set reminders.\n\n"
        "Just send messages like:\n"
        "• drink water in 10 minutes\n"
        "• call mom tomorrow 14:00\n"
        "• take vitamins daily at 09:00\n"
        "• pay rent weekly monday 10:00\n"
        "• go to dentist monthly 5 15:00",
        reply_markup=keyboard,
    )


async def menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Default free-text handler: custom snooze reply, edit reply, else create reminder."""
    update_last_seen(update.effective_user.id)
    txt = update.message.text.strip()
    low = txt.lower()

    # 1) waiting for custom snooze time
    if "awaiting_snooze_for" in context.user_data:
        rid = context.user_data.pop("awaiting_snooze_for")
        clear_followup_on_interaction(context.application, rid)

        user_tz_name = get_user_tz(update.effective_user.id)
        dt = parse_time_human(txt, user_tz_name)
        if not dt:
            await update.message.reply_text(
                "Couldn't parse time. Try something like:\n"
                "• in 45 minutes\n"
                "• tomorrow 09:00"
            )
            return
        now = datetime.now(timezone.utc)
        if dt <= now:
            await update.message.reply_text("That time is in the past. Try again.")
            return
        rem = get_reminder(rid)
        if not rem:
            await update.message.reply_text("Reminder not found.")
            return

        update_reminder(rid, run_at=dt.isoformat(), active=1)
        schedule_job(context.application, rid, rem[1], rem[3], dt)

        pretty = format_when_for_user(dt.isoformat(), user_tz_name)
        rec_label = recurrence_suffix(rem[5])
        await update.message.reply_text(f"💤 snoozed to {pretty} — {rem[3]}{rec_label}")
        return

    # 2) waiting for edit text
    if "editing_text_for" in context.user_data:
        rid = context.user_data.pop("editing_text_for")
        clear_followup_on_interaction(context.application, rid)

        rem = get_reminder(rid)
        if not rem:
            await update.message.reply_text("Reminder not found.")
            return
        new_text = txt
        update_reminder(rid, text=new_text)
        await update.message.reply_text(f"Updated reminder #{rid} text to:\n{new_text}")
        return

    # 3) menu buttons
    if low == "add":
        await update.message.reply_text(
            "Send a reminder like:\n"
            "• drink water in 10 minutes\n"
            "• call mom tomorrow 14:00\n"
            "• take vitamins daily at 09:00\n"
            "• pay rent weekly monday 10:00\n"
            "• go to dentist monthly 5 15:00",
            reply_markup=ReplyKeyboardRemove(),
        )
        return
    if low == "list":
        return await list_cmd(update, context)
    if low == "export":
        return await export_cmd(update, context)

    # default: treat as new reminder
    return await add_receive(update, context)


def _try_split_message_and_time(tokens: list[str], user_tz: str, message_first: bool) -> Tuple[Optional[str], Optional[datetime], Optional[str]]:
    for i in range(1, len(tokens)):
        if message_first:
            msg_candidate_tokens = tokens[:i]
            time_candidate_tokens = tokens[i:]
        else:
            time_candidate_tokens = tokens[:i]
            msg_candidate_tokens = tokens[i:]

        if not time_candidate_tokens or not msg_candidate_tokens:
            continue

        time_lower_str = " ".join(t.lower() for t in time_candidate_tokens)
        recurrence = None

        if ("daily" in time_lower_str or "every day" in time_lower_str or "everyday" in time_lower_str):
            recurrence = "daily"
            banned = {"daily", "every", "day", "everyday", "at"}
            time_tokens_clean = [t for t in time_candidate_tokens if t.lower() not in banned]
            time_candidate = " ".join(time_tokens_clean).strip() or "09:00"

        elif ("weekly" in time_lower_str or "every week" in time_lower_str):
            recurrence = "weekly"
            banned = {"weekly", "every", "week", "at"}
            time_tokens_clean = [t for t in time_candidate_tokens if t.lower() not in banned]
            time_candidate = " ".join(time_tokens_clean).strip() or "09:00"

        elif ("monthly" in time_lower_str or "every month" in time_lower_str):
            recurrence = "monthly"
            banned = {"monthly", "every", "month", "at"}
            time_tokens_clean = [t for t in time_candidate_tokens if t.lower() not in banned]
            time_candidate = " ".join(time_tokens_clean).strip() or "09:00"

        else:
            time_candidate = " ".join(time_candidate_tokens)

        dt = parse_time_human(time_candidate, user_tz)
        if dt:
            msg_str = " ".join(msg_candidate_tokens).strip()
            if not msg_str:
                continue

            # fix: avoid time tokens ending up inside message (e.g. "friday 12:00 close")
            has_time_token_msg = any(":" in t for t in msg_candidate_tokens)
            has_time_token_time = any(":" in t for t in time_candidate_tokens)
            if not has_time_token_time and has_time_token_msg:
                continue

            return msg_str, dt, recurrence

    return None, None, None


async def add_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    user_tz = get_user_tz(update.effective_user.id)

    normalized = (
        text.replace("—", " ")
            .replace("–", " ")
            .replace("-", " ")
    )
    tokens = normalized.split()

    if len(tokens) < 2:
        await update.message.reply_text(
            "Please write a message and a time.\n\n"
            "Example: drink water in 10 minutes"
        )
        return

    auto_default = False

    msg_str, parsed_dt, detected_recurrence = _try_split_message_and_time(tokens, user_tz, message_first=True)
    if not parsed_dt:
        msg_str, parsed_dt, detected_recurrence = _try_split_message_and_time(tokens, user_tz, message_first=False)

    if not parsed_dt:
        lower_text = text.lower()
        has_time_hint = (
            any(ch.isdigit() for ch in text)
            or ":" in text
            or any(
                w in lower_text
                for w in [
                    "am","pm","tomorrow","today","tonight","morning","evening","next",
                    "monday","tuesday","wednesday","thursday","friday","saturday","sunday",
                ]
            )
        )

        if not has_time_hint:
            auto_default = True
            msg_str = text
            now_utc = datetime.now(timezone.utc)
            parsed_dt = now_utc + timedelta(minutes=10)
            detected_recurrence = None
        else:
            await update.message.reply_text(
                "Couldn't find a valid time in your message.\n\n"
                "examples:\n"
                "• drink water in 10 minutes\n"
                "• call mom tomorrow 14:00\n"
                "• take vitamins daily at 09:00\n"
                "• pay rent weekly monday 10:00\n"
                "• go to dentist monthly 5 15:00"
            )
            return

    msg_str = clean_message_text(msg_str or text)

    now = datetime.now(timezone.utc)
    if parsed_dt <= now:
        await update.message.reply_text("That time is in the past. Try again.")
        return

    recurrence = detected_recurrence
    recurrence_detail = None

    rid = add_reminder(update.effective_chat.id, update.effective_user.id, msg_str, parsed_dt.isoformat(), recurrence, recurrence_detail)
    schedule_job(context.application, rid, update.effective_chat.id, msg_str, parsed_dt)

    pretty = format_when_for_user(parsed_dt.isoformat(), user_tz)
    rec_label = recurrence_suffix(recurrence)

    if auto_default:
        body = (
            f"📝 {msg_str}{rec_label}\n"
            f"⏰ added at {pretty}\n"
            "(i didn't see a time, so i set it for in 10 minutes)"
        )
    else:
        body = f"📝 {msg_str}{rec_label}\n⏰ added at {pretty}"

    await update.message.reply_text(body)


async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    update_last_seen(update.effective_user.id)
    user_id = update.effective_user.id
    rows = list_reminders(user_id)
    if not rows:
        await update.message.reply_text("No upcoming reminders.")
        return

    user_tz = get_user_tz(user_id)
    now = datetime.now(timezone.utc)
    upcoming = []

    for rid, text, run_at, recurrence, recurrence_detail, active in rows:
        if not active:
            continue
        dt = dateparser.parse(run_at, settings={"RETURN_AS_TIMEZONE_AWARE": True, "TO_TIMEZONE": "UTC"})
        if not dt or dt <= now:
            continue
        upcoming.append((rid, text, run_at, recurrence, recurrence_detail, active))

    if not upcoming:
        await update.message.reply_text("No upcoming reminders.")
        return

    for rid, text, run_at, recurrence, recurrence_detail, active in upcoming:
        pretty = format_dt_for_user(run_at, user_tz)
        rec = recurrence_suffix(recurrence)
        msg = f"{pretty}{rec} — {text}"
        buttons = [
            InlineKeyboardButton("Delete", callback_data=f"delete:{rid}"),
            InlineKeyboardButton("Toggle", callback_data=f"toggle:{rid}"),
        ]
        await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup([buttons]))


async def export_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    update_last_seen(update.effective_user.id)
    user_id = update.effective_user.id
    rows = list_reminders(user_id)
    if not rows:
        await update.message.reply_text("No reminders to export.")
        return

    bio = StringIO()
    writer = csv.writer(bio)
    writer.writerow(["id", "text", "run_at_utc", "recurrence", "recurrence_detail", "active"])
    for r in rows:
        writer.writerow(r)
    bio.seek(0)

    await update.message.reply_document(document=InputFile(bio, filename=f"reminders_{user_id}.csv"))


async def settz_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    update_last_seen(update.effective_user.id)
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /settz <Region/City> — e.g. /settz Europe/London")
        return
    tz = args[0]
    try:
        pytz.timezone(tz)
    except Exception:
        await update.message.reply_text("Invalid timezone. Use a name like Europe/London or Asia/Yerevan.")
        return
    set_user_tz(update.effective_user.id, tz)
    await update.message.reply_text(f"Timezone set to {tz}")



# ------------------ Callback queries (inline buttons) ------------------

def parse_snooze_delta(label: str) -> Optional[timedelta]:
    mapping = {
        "5m": timedelta(minutes=5),
        "15m": timedelta(minutes=15),
        "30m": timedelta(minutes=30),
        "1h": timedelta(hours=1),
        "3h": timedelta(hours=3),
        "1d": timedelta(days=1),
    }
    return mapping.get(label)


async def callback_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "noop":
        # used for "done ✅" button when reminder is already inactive
        return

    if data.startswith("delete:"):
        rid = int(data.split(":", 1)[1])
        clear_followup_on_interaction(context.application, rid)

        deleted = delete_reminder(rid)
        cancel_jobs_by_name(context.application.job_queue, JOB_NAME_TEMPLATE.format(rid))
        cancel_jobs_by_name(context.application.job_queue, FOLLOW_NAME_TEMPLATE.format(rid))

        await query.edit_message_text("Deleted reminder" if deleted else "Reminder not found")
        return

    if data.startswith("toggle:"):
        rid = int(data.split(":", 1)[1])
        clear_followup_on_interaction(context.application, rid)

        rem = get_reminder(rid)
        if not rem:
            await query.edit_message_text("Reminder not found")
            return
        active = rem[7]
        new = 0 if active else 1
        update_reminder(rid, active=new)

        job_name = JOB_NAME_TEMPLATE.format(rid)
        if new == 1:
            dt = dateparser.parse(rem[4], settings={"RETURN_AS_TIMEZONE_AWARE": True, "TO_TIMEZONE": "UTC"})
            if dt and dt > datetime.now(timezone.utc):
                schedule_job(context.application, rid, rem[1], rem[3], dt)
        else:
            cancel_jobs_by_name(context.application.job_queue, job_name)

        await query.edit_message_text(f"Reminder is now {'Active' if new == 1 else 'Inactive'}")
        return

    if data.startswith("snooze:"):
        _, rid_s, span = data.split(":", 2)
        rid = int(rid_s)
        clear_followup_on_interaction(context.application, rid)

        rem = get_reminder(rid)
        if not rem:
            await query.edit_message_text("Reminder not found")
            return

        delta = parse_snooze_delta(span)
        if not delta:
            await query.edit_message_text("Invalid snooze option.")
            return

        now = datetime.now(timezone.utc)
        new_dt = now + delta

        update_reminder(rid, run_at=new_dt.isoformat(), active=1)
        schedule_job(context.application, rid, rem[1], rem[3], new_dt)

        pretty = format_when_for_user(new_dt.isoformat(), get_user_tz(rem[2]))
        rec_label = recurrence_suffix(rem[5])
        await query.edit_message_text(f"💤 snoozed to {pretty} — {rem[3]}{rec_label}")
        return

    if data.startswith("snooze_custom:"):
        rid = int(data.split(":", 1)[1])
        clear_followup_on_interaction(context.application, rid)

        context.user_data["awaiting_snooze_for"] = rid
        await query.edit_message_text(
            "Send when to remind again.\n\n"
            "examples:\n"
            "• 18:45\n"
            "• in 45 minutes\n"
            "• tomorrow 09:00"
        )
        return

    if data.startswith("done:"):
        rid = int(data.split(":", 1)[1])
        clear_followup_on_interaction(context.application, rid)

        rem = get_reminder(rid)
        if not rem:
            await query.edit_message_text("Reminder not found")
            return

        update_reminder(rid, active=0)
        cancel_jobs_by_name(context.application.job_queue, JOB_NAME_TEMPLATE.format(rid))

        user_tz_name = get_user_tz(rem[2])
        now_iso = datetime.now(timezone.utc).isoformat()
        pretty_now = format_when_for_user(now_iso, user_tz_name)
        rec_label = recurrence_suffix(rem[5])

        text_html = f"<s>{escape(rem[3])}</s>{escape(rec_label)}"
        body_html = f"📝 {text_html}\n⏰ {escape(pretty_now)}\n\nRemind me again in:"

        await query.edit_message_text(
            body_html,
            reply_markup=build_snooze_keyboard(rid),
            parse_mode="HTML",
        )
        return

    if data.startswith("edit_text:"):
        rid = int(data.split(":", 1)[1])
        clear_followup_on_interaction(context.application, rid)

        rem = get_reminder(rid)
        if not rem:
            await query.edit_message_text("Reminder not found")
            return

        context.user_data["editing_text_for"] = rid
        await query.edit_message_text(f"Current text:\n{rem[3]}\n\nSend new text for this reminder.")
        return

    await query.edit_message_text("Unknown action.")


async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    update_last_seen(update.effective_user.id)
    await update.message.reply_text("I didn't understand that. Use /start or the menu.")


# ------------------ Main ------------------

def main():
    token = os.getenv("TELEGRAM_TOKEN")
    if not token:
        raise SystemExit("Set TELEGRAM_TOKEN in your environment")

    init_db()
    app = ApplicationBuilder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("export", export_cmd))
    app.add_handler(CommandHandler("settz", settz_cmd))
    app.add_handler(CallbackQueryHandler(callback_query_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, menu_handler))
    app.add_handler(MessageHandler(filters.COMMAND, unknown))

    load_jobs(app)
    logger.info("Bot started")
    app.run_polling()


if __name__ == "__main__":
    main()
