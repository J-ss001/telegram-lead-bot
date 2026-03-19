cat > /home/claude/telegram_lead_bot_simple.py << 'EOF'
import os
import re
import psycopg2
from datetime import datetime
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from anthropic import Anthropic

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CLAUDE_API_KEY = os.environ.get("CLAUDE_API_KEY")
DATABASE_URL = os.environ.get("DATABASE_URL")

if not TELEGRAM_BOT_TOKEN or not CLAUDE_API_KEY or not DATABASE_URL:
    raise ValueError("Missing environment variables")

client = Anthropic()

def get_connection():
    try:
        return psycopg2.connect(DATABASE_URL, connect_timeout=5)
    except Exception as e:
        print(f"[ERROR] DB connection failed: {e}")
        raise

def init_db():
    print("[INFO] Initialising database...")
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id BIGINT PRIMARY KEY,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS leads (
                id SERIAL PRIMARY KEY,
                user_telegram_id BIGINT REFERENCES users(telegram_id),
                name VARCHAR(255),
                phone VARCHAR(20),
                email VARCHAR(255),
                company VARCHAR(255),
                interest TEXT,
                score INT,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        conn.commit()
        print("[INFO] Database ready")
    finally:
        cursor.close()
        conn.close()

def extract_lead(text):
    """Extract lead info from text"""
    lead = {'name': None, 'email': None, 'phone': None, 'company': None, 'interest': None}
    
    email = re.search(r'[\w\.-]+@[\w\.-]+\.\w+', text)
    if email:
        lead['email'] = email.group()
    
    phone = re.search(r'\+?\d{1,3}[\d\s\-\.]{7,}', text)
    if phone:
        lead['phone'] = phone.group()
    
    # Simple check - if has email or phone, it's a lead
    if lead['email'] or lead['phone']:
        return lead
    return None

def score_lead(lead):
    """Score with Claude"""
    try:
        response = client.messages.create(
            model="claude-opus-4-1-20250805",
            max_tokens=5,
            messages=[{"role": "user", "content": f"Rate lead {lead} on scale 1-10. Only respond with number."}]
        )
        text = response.content[0].text.strip()
        match = re.search(r'\d', text)
        return int(match.group()) if match else 7
    except Exception as e:
        print(f"[ERROR] Scoring failed: {e}")
        return 7

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("INSERT INTO users (telegram_id) VALUES (%s) ON CONFLICT (telegram_id) DO NOTHING", (user_id,))
    conn.commit()
    cursor.close()
    conn.close()
    
    msg = "✅ LeadBot Active!\n\n/stats - View leads\n/export - Download CSV"
    await update.message.reply_text(msg)

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*), AVG(score) FROM leads WHERE user_telegram_id = %s", (user_id,))
    total, avg = cursor.fetchone()
    cursor.close()
    conn.close()
    
    total = total or 0
    avg = round(avg, 1) if avg else 0
    
    msg = f"📊 Your Leads\n\nTotal: {total}\nAverage Score: {avg}/10"
    await update.message.reply_text(msg)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Process ALL messages in groups"""
    print(f"[DEBUG] Message received in {update.message.chat.type}")
    
    if update.message.chat.type not in ['group', 'supergroup']:
        return
    
    text = update.message.text
    if not text:
        return
    
    print(f"[DEBUG] Group message: {text[:50]}")
    
    lead = extract_lead(text)
    if not lead:
        print("[DEBUG] No lead extracted")
        return
    
    print(f"[INFO] LEAD FOUND: {lead}")
    
    score = score_lead(lead)
    print(f"[INFO] Score: {score}")
    
    user_id = update.effective_user.id
    conn = get_connection()
    cursor = conn.cursor()
    
    cursor.execute("INSERT INTO users (telegram_id) VALUES (%s) ON CONFLICT DO NOTHING", (user_id,))
    cursor.execute("""
        INSERT INTO leads (user_telegram_id, email, phone, score)
        VALUES (%s, %s, %s, %s)
    """, (user_id, lead['email'], lead['phone'], score))
    conn.commit()
    cursor.close()
    conn.close()
    
    print(f"[INFO] ✅ LEAD STORED for user {user_id}")

def main():
    print("[INFO] Starting bot...")
    init_db()
    
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    print("[INFO] Bot polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
EOF
cat /home/claude/telegram_lead_bot_simple.py
Output

import os
import re
import psycopg2
from datetime import datetime
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from anthropic import Anthropic

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CLAUDE_API_KEY = os.environ.get("CLAUDE_API_KEY")
DATABASE_URL = os.environ.get("DATABASE_URL")

if not TELEGRAM_BOT_TOKEN or not CLAUDE_API_KEY or not DATABASE_URL:
    raise ValueError("Missing environment variables")

client = Anthropic()

def get_connection():
    try:
        return psycopg2.connect(DATABASE_URL, connect_timeout=5)
    except Exception as e:
        print(f"[ERROR] DB connection failed: {e}")
        raise

def init_db():
    print("[INFO] Initialising database...")
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id BIGINT PRIMARY KEY,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS leads (
                id SERIAL PRIMARY KEY,
                user_telegram_id BIGINT REFERENCES users(telegram_id),
                name VARCHAR(255),
                phone VARCHAR(20),
                email VARCHAR(255),
                company VARCHAR(255),
                interest TEXT,
                score INT,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        conn.commit()
        print("[INFO] Database ready")
    finally:
        cursor.close()
        conn.close()

def extract_lead(text):
    """Extract lead info from text"""
    lead = {'name': None, 'email': None, 'phone': None, 'company': None, 'interest': None}
    
    email = re.search(r'[\w\.-]+@[\w\.-]+\.\w+', text)
    if email:
        lead['email'] = email.group()
    
    phone = re.search(r'\+?\d{1,3}[\d\s\-\.]{7,}', text)
    if phone:
        lead['phone'] = phone.group()
    
    # Simple check - if has email or phone, it's a lead
    if lead['email'] or lead['phone']:
        return lead
    return None

def score_lead(lead):
    """Score with Claude"""
    try:
        response = client.messages.create(
            model="claude-opus-4-1-20250805",
            max_tokens=5,
            messages=[{"role": "user", "content": f"Rate lead {lead} on scale 1-10. Only respond with number."}]
        )
        text = response.content[0].text.strip()
        match = re.search(r'\d', text)
        return int(match.group()) if match else 7
    except Exception as e:
        print(f"[ERROR] Scoring failed: {e}")
        return 7

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("INSERT INTO users (telegram_id) VALUES (%s) ON CONFLICT (telegram_id) DO NOTHING", (user_id,))
    conn.commit()
    cursor.close()
    conn.close()
    
    msg = "✅ LeadBot Active!\n\n/stats - View leads\n/export - Download CSV"
    await update.message.reply_text(msg)

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*), AVG(score) FROM leads WHERE user_telegram_id = %s", (user_id,))
    total, avg = cursor.fetchone()
    cursor.close()
    conn.close()
    
    total = total or 0
    avg = round(avg, 1) if avg else 0
    
    msg = f"📊 Your Leads\n\nTotal: {total}\nAverage Score: {avg}/10"
    await update.message.reply_text(msg)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Process ALL messages in groups"""
    print(f"[DEBUG] Message received in {update.message.chat.type}")
    
    if update.message.chat.type not in ['group', 'supergroup']:
        return
    
    text = update.message.text
    if not text:
        return
    
    print(f"[DEBUG] Group message: {text[:50]}")
    
    lead = extract_lead(text)
    if not lead:
        print("[DEBUG] No lead extracted")
        return
    
    print(f"[INFO] LEAD FOUND: {lead}")
    
    score = score_lead(lead)
    print(f"[INFO] Score: {score}")
    
    user_id = update.effective_user.id
    conn = get_connection()
    cursor = conn.cursor()
    
    cursor.execute("INSERT INTO users (telegram_id) VALUES (%s) ON CONFLICT DO NOTHING", (user_id,))
    cursor.execute("""
        INSERT INTO leads (user_telegram_id, email, phone, score)
        VALUES (%s, %s, %s, %s)
    """, (user_id, lead['email'], lead['phone'], score))
    conn.commit()
    cursor.close()
    conn.close()
    
    print(f"[INFO] ✅ LEAD STORED for user {user_id}")

def main():
    print("[INFO] Starting bot...")
    init_db()
    
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    print("[INFO] Bot polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
