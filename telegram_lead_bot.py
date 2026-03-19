import os
import re
import psycopg2
from datetime import datetime
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL")

if not TELEGRAM_BOT_TOKEN or not DATABASE_URL:
    raise ValueError("Missing TELEGRAM_BOT_TOKEN or DATABASE_URL")

# ──────────────────────────────────────────────
# DATABASE
# ──────────────────────────────────────────────

def get_connection():
    try:
        conn = psycopg2.connect(DATABASE_URL, connect_timeout=5)
        return conn
    except Exception as e:
        print(f"[ERROR] DB connection failed: {e}")
        raise

def init_db():
    print("[INFO] Initialising database…")
    conn = get_connection()
    cursor = conn.cursor()
    try:
        # Users who /start the bot (the marketers/owners)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id BIGINT PRIMARY KEY,
                username    VARCHAR(255),
                created_at  TIMESTAMP DEFAULT NOW()
            )
        """)

        # Maps a Telegram group → the owner who added the bot
        # FIX: This is the key table that was MISSING from your original code.
        # Without it the bot had no way to know which marketer owns which group.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS group_owners (
                group_id   BIGINT PRIMARY KEY,
                owner_id   BIGINT NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
                group_name VARCHAR(255),
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)

        # Leads extracted from groups — stored against the GROUP OWNER, not the lead sender
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS leads (
                id               SERIAL PRIMARY KEY,
                owner_id         BIGINT NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
                group_id         BIGINT,
                sender_id        BIGINT,
                name             VARCHAR(255),
                phone            VARCHAR(50),
                email            VARCHAR(255),
                company          VARCHAR(255),
                interest         TEXT,
                raw_message      TEXT,
                score            INT DEFAULT 7,
                created_at       TIMESTAMP DEFAULT NOW()
            )
        """)

        cursor.execute("CREATE INDEX IF NOT EXISTS idx_leads_owner ON leads(owner_id)")
        conn.commit()
        print("[INFO] Database tables verified / created.")
    except Exception as e:
        print(f"[ERROR] Database init failed: {e}")
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()

def register_user(telegram_id, username=None):
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "INSERT INTO users (telegram_id, username) VALUES (%s, %s) ON CONFLICT (telegram_id) DO NOTHING",
            (telegram_id, username)
        )
        conn.commit()
    finally:
        cursor.close()
        conn.close()

def register_group(group_id, owner_id, group_name=None):
    """Link a Telegram group to the marketer who owns it."""
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO group_owners (group_id, owner_id, group_name)
            VALUES (%s, %s, %s)
            ON CONFLICT (group_id) DO UPDATE
                SET owner_id = EXCLUDED.owner_id,
                    group_name = EXCLUDED.group_name
        """, (group_id, owner_id, group_name))
        conn.commit()
        print(f"[INFO] Group {group_id} registered to owner {owner_id}")
        return True
    except Exception as e:
        print(f"[ERROR] register_group failed: {e}")
        conn.rollback()
        return False
    finally:
        cursor.close()
        conn.close()

def get_group_owner(group_id):
    """Return the owner_id for a group, or None if not registered."""
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT owner_id FROM group_owners WHERE group_id = %s", (group_id,))
        row = cursor.fetchone()
        return row[0] if row else None
    finally:
        cursor.close()
        conn.close()

def store_lead(owner_id, group_id, sender_id, lead_info, raw_message):
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO leads (owner_id, group_id, sender_id, name, email, phone, company, interest, raw_message, score)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            owner_id,
            group_id,
            sender_id,
            lead_info.get('name'),
            lead_info.get('email'),
            lead_info.get('phone'),
            lead_info.get('company'),
            lead_info.get('interest'),
            raw_message[:500],
            7
        ))
        conn.commit()
        print(f"[INFO] ✅ Lead stored for owner {owner_id}: {lead_info.get('email', lead_info.get('phone', 'unknown'))}")
        return True
    except Exception as e:
        print(f"[ERROR] Failed to store lead: {e}")
        conn.rollback()
        return False
    finally:
        cursor.close()
        conn.close()

def get_user_stats(owner_id):
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT COUNT(*), AVG(score) FROM leads WHERE owner_id = %s",
            (owner_id,)
        )
        total, avg_score = cursor.fetchone()
        total = total or 0
        avg_score = round(float(avg_score), 1) if avg_score else 0.0
        print(f"[INFO] Stats for owner {owner_id}: total={total}, avg={avg_score}")
        return {'total_leads': total, 'avg_score': avg_score}
    except Exception as e:
        print(f"[ERROR] Stats query failed: {e}")
        return {'total_leads': 0, 'avg_score': 0}
    finally:
        cursor.close()
        conn.close()

def export_leads_csv(owner_id):
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT name, email, phone, company, interest, score, created_at FROM leads WHERE owner_id = %s ORDER BY created_at DESC",
            (owner_id,)
        )
        leads = cursor.fetchall()
        if not leads:
            return None
        csv = "name,email,phone,company,interest,score,captured_at\n"
        for lead in leads:
            csv += f'"{lead[0] or ""}","{lead[1] or ""}","{lead[2] or ""}","{lead[3] or ""}","{lead[4] or ""}",{lead[5]},"{lead[6]}"\n'
        return csv
    except Exception as e:
        print(f"[ERROR] Export failed: {e}")
        return None
    finally:
        cursor.close()
        conn.close()

def list_registered_groups(owner_id):
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT group_id, group_name, created_at FROM group_owners WHERE owner_id = %s",
            (owner_id,)
        )
        return cursor.fetchall()
    finally:
        cursor.close()
        conn.close()

# ──────────────────────────────────────────────
# LEAD EXTRACTION
# ──────────────────────────────────────────────

def extract_lead_info(text):
    lead = {'name': None, 'email': None, 'phone': None, 'company': None, 'interest': None}

    # Email
    email_match = re.search(r'[\w\.\+\-]+@[\w\.-]+\.\w{2,}', text)
    if email_match:
        lead['email'] = email_match.group()

    # Phone — more flexible pattern
    phone_match = re.search(r'(\+?\d[\d\s\-\(\)]{7,17}\d)', text)
    if phone_match:
        lead['phone'] = phone_match.group().strip()

    # Company
    company_match = re.search(
        r'(?:company|corp|co|ltd|inc|agency|business|firm)[:\s]+([A-Za-z0-9\s&\-]+?)(?:[,\.\n]|$)',
        text, re.IGNORECASE
    )
    if company_match:
        lead['company'] = company_match.group(1).strip()

    # Name — also catches "My name is X" and Telegram first-name patterns
    name_match = re.search(
        r"(?:my name is|name[:\s]+|i'?m|i am|hi[,\s]+i'?m|hello[,\s]+i'?m)\s+([A-Za-z][A-Za-z\s]{1,30}?)(?:[,\.\n]|$)",
        text, re.IGNORECASE
    )
    if name_match:
        lead['name'] = name_match.group(1).strip()

    # Interest
    interest_match = re.search(
        r'(?:looking for|interested in|need|want|require)[:\s]+([A-Za-z0-9\s&\-]+?)(?:[,\.\n]|$)',
        text, re.IGNORECASE
    )
    if interest_match:
        lead['interest'] = interest_match.group(1).strip()

    # Return only if we found something useful
    if lead['email'] or lead['phone'] or lead['name']:
        return lead
    return None

# ──────────────────────────────────────────────
# COMMAND HANDLERS
# ──────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """DM /start — registers the marketer as a user."""
    user = update.effective_user
    register_user(user.id, user.username)

    message = (
        "🎉 *Welcome to LeadBot!*\n\n"
        "I automatically extract leads from Telegram groups.\n\n"
        "*Setup steps:*\n"
        "1️⃣  Add me to your Telegram group\n"
        "2️⃣  In the group, type `/register` — this links the group to your account\n"
        "3️⃣  Done! I'll capture every lead posted in that group\n\n"
        "*Your commands (use in DM or group):*\n"
        "/register — Link a group to your account _(run inside the group)_\n"
        "/mygroups — See your registered groups\n"
        "/stats — View your lead statistics\n"
        "/export — Download leads as CSV\n"
        "/help — Show all commands"
    )
    await update.message.reply_text(message, parse_mode="Markdown")
    print(f"[INFO] /start from user {user.id}")


async def register_group_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /register — must be run INSIDE the group by the marketer/owner.
    Links this group to their account so leads are attributed correctly.
    """
    chat = update.effective_chat
    user = update.effective_user

    # Must be run in a group
    if chat.type not in ['group', 'supergroup']:
        await update.message.reply_text(
            "⚠️ Please run /register *inside your Telegram group*, not in DM.",
            parse_mode="Markdown"
        )
        return

    # Ensure marketer is registered
    register_user(user.id, user.username)

    success = register_group(chat.id, user.id, chat.title)

    if success:
        await update.message.reply_text(
            f"✅ *Group registered!*\n\n"
            f"Group: *{chat.title}*\n"
            f"Owner: @{user.username or user.first_name}\n\n"
            f"I'll now capture all leads posted in this group and assign them to your account.\n"
            f"Use /stats in DM to see your leads.",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text("❌ Registration failed. Please try again.")

    print(f"[INFO] /register — group {chat.id} ({chat.title}) → owner {user.id}")


async def my_groups(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the marketer's registered groups."""
    user_id = update.effective_user.id
    groups = list_registered_groups(user_id)

    if not groups:
        await update.message.reply_text(
            "You have no registered groups yet.\n"
            "Add me to a group and run /register inside it."
        )
        return

    lines = ["📋 *Your registered groups:*\n"]
    for gid, gname, created in groups:
        lines.append(f"• *{gname or 'Unknown'}* (ID: `{gid}`)")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show lead stats for the owner."""
    user_id = update.effective_user.id
    stats_data = get_user_stats(user_id)

    message = (
        f"📊 *Your Lead Statistics*\n\n"
        f"Total leads captured: *{stats_data['total_leads']}*\n"
        f"Average lead score: *{stats_data['avg_score']} / 10*\n\n"
        f"Use /export to download as CSV."
    )
    await update.message.reply_text(message, parse_mode="Markdown")
    print(f"[INFO] /stats from user {user_id}")


async def export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Export leads as CSV."""
    user_id = update.effective_user.id
    csv_data = export_leads_csv(user_id)

    if csv_data is None:
        await update.message.reply_text(
            "❌ You have no leads to export yet.\n\n"
            "Make sure you've:\n"
            "1. Added me to your group\n"
            "2. Run /register inside the group\n"
            "3. Had people post messages with contact info"
        )
        return

    await update.message.reply_document(
        document=csv_data.encode(),
        filename=f"leads_{user_id}_{datetime.now().strftime('%Y%m%d')}.csv",
        caption="📥 Your extracted leads (CSV format)"
    )
    print(f"[INFO] /export from user {user_id}")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = (
        "🤖 *LeadBot Help*\n\n"
        "*Setup (one-time):*\n"
        "1. Add me to your Telegram group\n"
        "2. Run /register *inside the group*\n\n"
        "*Commands:*\n"
        "/start — Welcome & setup guide\n"
        "/register — Link this group to your account _(run in group)_\n"
        "/mygroups — See your registered groups\n"
        "/stats — View your lead statistics\n"
        "/export — Download leads as CSV\n"
        "/help — Show this message\n\n"
        "*What counts as a lead?*\n"
        "Any message containing an email address, phone number, or name pattern.\n\n"
        "Example: _'Hi I'm John from Acme, email: john@acme.com, +27123456789'_"
    )
    await update.message.reply_text(message, parse_mode="Markdown")


# ──────────────────────────────────────────────
# GROUP MESSAGE HANDLER
# ──────────────────────────────────────────────

async def handle_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Read group messages and extract leads — attributed to the group owner."""
    if not update.message:
        return
    if update.message.chat.type not in ['group', 'supergroup']:
        return

    text = update.message.text
    if not text:
        return

    group_id  = update.message.chat.id
    group_name = update.message.chat.title
    sender_id  = update.effective_user.id

    print(f"[DEBUG] Message in group {group_id} from sender {sender_id}: {text[:80]}")

    # Look up who owns this group
    owner_id = get_group_owner(group_id)
    if owner_id is None:
        print(f"[INFO] No owner registered for group {group_id} — skipping lead.")
        return

    lead_info = extract_lead_info(text)
    if lead_info:
        print(f"[INFO] Lead detected: {lead_info}")
        store_lead(owner_id, group_id, sender_id, lead_info, text)
    else:
        print(f"[DEBUG] No lead info extracted from message.")


# ──────────────────────────────────────────────
# BOT STARTUP
# ──────────────────────────────────────────────

def main():
    print("[INFO] Starting LeadBot…")
    init_db()

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",    start))
    app.add_handler(CommandHandler("register", register_group_command))
    app.add_handler(CommandHandler("mygroups", my_groups))
    app.add_handler(CommandHandler("stats",    stats))
    app.add_handler(CommandHandler("export",   export))
    app.add_handler(CommandHandler("help",     help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_group_message))

    print("[INFO] Bot started. Polling for updates…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
