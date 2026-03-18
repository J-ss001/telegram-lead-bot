"""
Telegram Lead Extraction Bot
Extracts leads from Telegram groups, scores them with Claude AI,
and stores them in PostgreSQL (Supabase).
"""

import os
import csv
import json
import logging
import time
import io
import re
import psycopg2
import psycopg2.extras
import requests
from datetime import datetime
from dotenv import load_dotenv
from telegram import Update, InputFile
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ─────────────────────────────────────────────
# Environment & Logging Setup
# ─────────────────────────────────────────────

load_dotenv()

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CLAUDE_API_KEY = os.environ["CLAUDE_API_KEY"]
DATABASE_URL = os.environ["DATABASE_URL"]

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s",
    datefmt="%H:%M",
)
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# Database Helpers
# ─────────────────────────────────────────────

def get_connection(retries: int = 3, delay: float = 2.0):
    """Return a new psycopg2 connection, retrying on failure."""
    for attempt in range(1, retries + 1):
        try:
            conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
            return conn
        except psycopg2.OperationalError as exc:
            logger.error("DB connection attempt %d/%d failed: %s", attempt, retries, exc)
            if attempt < retries:
                time.sleep(delay)
    raise RuntimeError("Could not connect to database after %d attempts." % retries)


def init_db() -> None:
    """Create tables if they don't exist yet."""
    ddl = """
    CREATE TABLE IF NOT EXISTS users (
        telegram_id        BIGINT PRIMARY KEY,
        username           TEXT,
        subscription_status TEXT NOT NULL DEFAULT 'trial',
        subscription_start_date TIMESTAMPTZ DEFAULT NOW(),
        created_at         TIMESTAMPTZ DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS leads (
        id                 BIGSERIAL PRIMARY KEY,
        user_telegram_id   BIGINT NOT NULL REFERENCES users(telegram_id),
        name               TEXT,
        phone              TEXT,
        email              TEXT,
        company            TEXT,
        interest           TEXT,
        score              SMALLINT NOT NULL,
        created_at         TIMESTAMPTZ DEFAULT NOW()
    );
    """
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(ddl)
        logger.info("Database tables verified / created.")
    finally:
        conn.close()


def upsert_user(telegram_id: int, username: str | None) -> None:
    """Insert a user row if it doesn't already exist."""
    sql = """
    INSERT INTO users (telegram_id, username)
    VALUES (%s, %s)
    ON CONFLICT (telegram_id) DO NOTHING;
    """
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(sql, (telegram_id, username))
    finally:
        conn.close()


def insert_lead(user_telegram_id: int, lead: dict) -> None:
    """Persist a scored lead to the database."""
    sql = """
    INSERT INTO leads (user_telegram_id, name, phone, email, company, interest, score)
    VALUES (%(user_telegram_id)s, %(name)s, %(phone)s, %(email)s,
            %(company)s, %(interest)s, %(score)s);
    """
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(sql, {**lead, "user_telegram_id": user_telegram_id})
    finally:
        conn.close()


def fetch_stats(user_telegram_id: int) -> dict:
    """Return total leads and average score for a user."""
    sql = """
    SELECT COUNT(*) AS total, ROUND(AVG(score)::numeric, 1) AS avg_score
    FROM leads
    WHERE user_telegram_id = %s;
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (user_telegram_id,))
            row = cur.fetchone()
            return {"total": row["total"] or 0, "avg_score": row["avg_score"] or 0}
    finally:
        conn.close()


def fetch_leads_for_export(user_telegram_id: int) -> list[dict]:
    """Return all leads for a user as a list of dicts."""
    sql = """
    SELECT name, phone, email, company, interest, score,
           to_char(created_at, 'YYYY-MM-DD HH24:MI') AS created_at
    FROM leads
    WHERE user_telegram_id = %s
    ORDER BY created_at DESC;
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (user_telegram_id,))
            return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()


# ─────────────────────────────────────────────
# Claude API Integration
# ─────────────────────────────────────────────

CLAUDE_ENDPOINT = "https://api.anthropic.com/v1/messages"
CLAUDE_MODEL = "claude-opus-4-20250514"

LEAD_PROMPT = """\
Analyze the following Telegram message and extract lead information.
Return ONLY a valid JSON object — no markdown, no explanation.

JSON format:
{{
  "name": "Full Name or null",
  "phone": "+27XXXXXXXXX or null",
  "email": "email@domain.com or null",
  "company": "Company Name or null",
  "interest": "Product or service they seem interested in, or null",
  "score": <integer 1-10 purchase-intent score>
}}

Score guide: 1-4 = likely spam/noise, 5-7 = potential lead, 8-10 = strong intent.

Message:
{message}
"""


def analyze_with_claude(message_text: str) -> dict | None:
    """
    Send a message to Claude for lead extraction.
    Returns a parsed lead dict or None on failure.
    """
    prompt = LEAD_PROMPT.format(message=message_text)
    payload = {
        "model": CLAUDE_MODEL,
        "max_tokens": 500,
        "messages": [{"role": "user", "content": prompt}],
    }
    headers = {
        "Content-Type": "application/json",
        "x-api-key": CLAUDE_API_KEY,
        "anthropic-version": "2023-06-01",
    }

    try:
        resp = requests.post(CLAUDE_ENDPOINT, json=payload, headers=headers, timeout=20)
        resp.raise_for_status()
        raw_text = resp.json()["content"][0]["text"].strip()

        # Strip optional markdown code fences
        raw_text = re.sub(r"^```(?:json)?\s*", "", raw_text)
        raw_text = re.sub(r"\s*```$", "", raw_text)

        lead = json.loads(raw_text)

        # Validate required field
        if not isinstance(lead.get("score"), int):
            logger.warning("Claude returned invalid score: %s", lead)
            return None

        return lead

    except requests.RequestException as exc:
        logger.error("Claude API request failed: %s", exc)
    except (json.JSONDecodeError, KeyError) as exc:
        logger.error("Could not parse Claude response: %s", exc)

    return None


# ─────────────────────────────────────────────
# Regex pre-screen (skip obvious non-leads)
# ─────────────────────────────────────────────

PHONE_RE = re.compile(r"(\+27\d{9}|0\d{9})")
EMAIL_RE = re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+")


def message_has_contact_signals(text: str) -> bool:
    """Return True if the message contains a phone, email, or a name-like token."""
    if PHONE_RE.search(text):
        return True
    if EMAIL_RE.search(text):
        return True
    # Rough heuristic: message has at least two capitalised words (possible name)
    capitalised = re.findall(r"\b[A-Z][a-z]{1,}\b", text)
    return len(capitalised) >= 2


# ─────────────────────────────────────────────
# Telegram Command Handlers
# ─────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Welcome message and user registration."""
    user = update.effective_user
    upsert_user(user.id, user.username)
    logger.info("User %s (%d) started the bot.", user.username or "unknown", user.id)

    await update.message.reply_text(
        "👋 *Welcome to LeadBot!*\n\n"
        "Add me to your Telegram group and I will automatically:\n"
        "• Extract potential leads from every message\n"
        "• Score each lead 1–10 using AI\n"
        "• Store high-quality leads (score ≥ 5) for you\n\n"
        "*Commands:*\n"
        "/stats – View your lead statistics\n"
        "/export – Download leads as CSV\n"
        "/help – Show this message",
        parse_mode="Markdown",
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show help text."""
    await cmd_start(update, context)


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show lead statistics for the requesting user."""
    user = update.effective_user
    upsert_user(user.id, user.username)

    try:
        stats = fetch_stats(user.id)
        await update.message.reply_text(
            f"📊 *Your Lead Stats*\n\n"
            f"Total leads captured: *{stats['total']}*\n"
            f"Average lead score:   *{stats['avg_score']} / 10*",
            parse_mode="Markdown",
        )
        logger.info("Stats served to user %d: %s", user.id, stats)
    except Exception as exc:
        logger.error("Failed to fetch stats for user %d: %s", user.id, exc)
        await update.message.reply_text("❌ Could not retrieve stats. Please try again later.")


async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Export all leads as a CSV file sent to the user."""
    user = update.effective_user
    upsert_user(user.id, user.username)

    try:
        leads = fetch_leads_for_export(user.id)

        if not leads:
            await update.message.reply_text("📭 You have no leads yet.")
            return

        # Build CSV in-memory
        buffer = io.StringIO()
        fieldnames = ["name", "phone", "email", "company", "interest", "score", "created_at"]
        writer = csv.DictWriter(buffer, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(leads)
        buffer.seek(0)

        filename = f"leads_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.csv"
        await update.message.reply_document(
            document=InputFile(io.BytesIO(buffer.getvalue().encode()), filename=filename),
            caption=f"✅ {len(leads)} lead(s) exported.",
        )
        logger.info("Exported %d leads to user %d.", len(leads), user.id)

    except Exception as exc:
        logger.error("Export failed for user %d: %s", user.id, exc)
        await update.message.reply_text("❌ Export failed. Please try again later.")


# ─────────────────────────────────────────────
# Group Message Handler (Lead Extraction Core)
# ─────────────────────────────────────────────

async def handle_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Called for every non-command group message.
    Pre-screens the text, calls Claude, and stores qualifying leads.
    The lead is attributed to whoever added the bot (tracked via bot_data).
    """
    message = update.effective_message
    if not message or not message.text:
        return

    text = message.text.strip()
    sender = message.from_user
    chat = message.chat

    logger.info(
        "Message received in '%s' from %s: %.80s…",
        chat.title or chat.id,
        sender.full_name if sender else "unknown",
        text,
    )

    # Cheap pre-screen: only hit Claude if the message has contact signals
    if not message_has_contact_signals(text):
        return

    # Determine which bot owner to credit this lead to.
    # We store a mapping of group_id → owner_telegram_id in bot_data.
    owner_id: int | None = context.bot_data.get(f"owner_{chat.id}")
    if owner_id is None:
        logger.info("No owner registered for group %s — skipping lead.", chat.id)
        return

    lead = analyze_with_claude(text)
    if lead is None:
        return

    score = lead.get("score", 0)
    if score < 5:
        logger.info(
            "Lead scored %d (< 5) — discarded. Email: %s", score, lead.get("email")
        )
        return

    try:
        insert_lead(owner_id, lead)
        logger.info(
            "Lead stored for user %d: email=%s score=%d",
            owner_id, lead.get("email"), score,
        )
    except Exception as exc:
        logger.error("Failed to store lead: %s", exc)


# ─────────────────────────────────────────────
# Bot Added / Removed From Group
# ─────────────────────────────────────────────

async def handle_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Fires when the bot's membership status in a chat changes.
    Records the user who added the bot as the group owner.
    """
    event = update.my_chat_member
    if not event:
        return

    new_status = event.new_chat_member.status
    chat = event.chat
    actor = event.from_user  # the user who performed the action

    if new_status in ("member", "administrator"):
        # Bot was added — register actor as group owner
        upsert_user(actor.id, actor.username)
        context.bot_data[f"owner_{chat.id}"] = actor.id
        logger.info(
            "Bot added to group '%s' (%d) by %s (%d).",
            chat.title, chat.id, actor.username or actor.full_name, actor.id,
        )
    elif new_status in ("kicked", "left"):
        # Bot was removed — clean up mapping
        context.bot_data.pop(f"owner_{chat.id}", None)
        logger.info("Bot removed from group '%s' (%d).", chat.title, chat.id)


# ─────────────────────────────────────────────
# Application Entry Point
# ─────────────────────────────────────────────

def main() -> None:
    logger.info("Initialising database …")
    init_db()

    logger.info("Building Telegram application …")
    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .build()
    )

    # Private chat commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("export", cmd_export))

    # Group message listener (non-command text in groups/supergroups)
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & (filters.ChatType.GROUP | filters.ChatType.SUPERGROUP),
            handle_group_message,
        )
    )

    # Track bot being added/removed from groups
    from telegram.ext import ChatMemberHandler
    app.add_handler(ChatMemberHandler(handle_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))

    logger.info("Bot started. Polling for updates …")
    app.run_polling(
        allowed_updates=["message", "my_chat_member"],
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
