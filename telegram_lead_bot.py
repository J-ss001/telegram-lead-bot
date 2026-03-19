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

client = Anthropic()

def get_connection():
    try:
        return psycopg2.connect(DATABASE_URL, connect_timeout=5)
    except Exception as e:
        print(f"[ERROR] DB connection failed: {e}")
        raise

def init_db():
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id BIGINT PRIMARY KEY,
                username VARCHAR(255),
                subscription_status VARCHAR(20) DEFAULT 'free',
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS leads (
                id SERIAL PRIMARY KEY,
                user_telegram_id BIGINT NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
                name VARCHAR(255),
                phone VARCHAR(20),
                email VARCHAR(255),
                company VARCHAR(255),
                interest TEXT,
                score INT,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_leads_user ON leads(user_telegram_id)")
        conn.commit()
        print("[INFO] Database ready")
    finally:
        cursor.close()
        conn.close()

def extract_lead_info(text):
    """Extract lead data from text"""
    lead = {'name': None, 'email': None, 'phone': None, 'company': None, 'interest': None}
    
    email_match = re.search(r'[\w\.-]+@[\w\.-]+\.\w+', text)
    if email_match:
        lead['email'] = email_match.group()
    
    phone_match = re.search(r'(\+?\d{1,3}[-.\s]?\d{1,14})', text)
    if phone_match:
        lead['phone'] = phone_match.group()
    
    company_match = re.search(r'(?:company|corp|co|ltd|inc|agency|business)[:\s]+([A-Za-z\s&]+?)(?:[,\.]|$)', text, re.IGNORECASE)
    if company_match:
        lead['company'] = company_match.group(1).strip()
    
    name_match = re.search(r"(?:name|i'm|i am|hi)[:\s]+([A-Za-z\s]+?)(?:[,\.]|$)", text, re.IGNORECASE)
    if name_match:
        lead['name'] = name_match.group(1).strip()
    
    interest_match = re.search(r'(?:looking for|interested in|need)[:\s]+([A-Za-z\s&]+?)(?:[,\.]|$)', text, re.IGNORECASE)
    if interest_match:
        lead['interest'] = interest_match.group(1).strip()
    
    if lead['email'] or lead['phone'] or lead['name']:
        return lead
    return None

def score_lead(lead_info):
    """Score lead with Claude"""
    try:
        prompt = f"""Rate this lead 1-10. Respond with ONLY a number.
- Name: {lead_info.get('name', 'Unknown')}
- Email: {lead_info.get('email', 'N/A')}
- Phone: {lead_info.get('phone', 'N/A')}
- Company: {lead_info.get('company', 'N/A')}
- Interest: {lead_info.get('interest', 'N/A')}"""
        
        response = client.messages.create(
            model="claude-opus-4-1-20250805",
            max_tokens=5,
            messages=[{"role": "user", "content": prompt}]
        )
        
        text = response.content[0].text.strip()
        match = re.search(r'\d+', text)
        if match:
            return max(1, min(10, int(match.group())))
        return 7
    except Exception as e:
        print(f"[ERROR] Claude failed: {e}")
        return 7

def store_lead(user_id, lead_info, score):
    """Store lead in database"""
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO users (telegram_id) VALUES (%s) ON CONFLICT (telegram_id) DO NOTHING", (user_id,))
        cursor.execute("""
            INSERT INTO leads (user_telegram_id, name, email, phone, company, interest, score)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (user_id, lead_info.get('name'), lead_info.get('email'), lead_info.get('phone'), 
              lead_info.get('company'), lead_info.get('interest'), score))
        conn.commit()
        print(f"[INFO] Lead stored: {lead_info.get('email')} (score: {score})")
    except Exception as e:
        print(f"[ERROR] Store failed: {e}")
        conn.rollback()
    finally:
        cursor.close()
        conn.close()

def get_user_stats(user_id):
    """Get user's lead stats"""
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT COUNT(*), AVG(score) FROM leads WHERE user_telegram_id = %s", (user_id,))
        total, avg = cursor.fetchone()
        return {'total_leads': total or 0, 'avg_score': round(avg, 1) if avg else 0}
    except Exception as e:
        print(f"[ERROR] Stats failed: {e}")
        return {'total_leads': 0, 'avg_score': 0}
    finally:
        cursor.close()
        conn.close()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start"""
    user_id = update.effective_user.id
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("INSERT INTO users (telegram_id) VALUES (%s) ON CONFLICT DO NOTHING", (user_id,))
    conn.commit()
    cursor.close()
    conn.close()
    
    msg = """Welcome to LeadBot! 🎉

I extract leads from group messages automatically.

Commands:
/stats - View your leads
/export - Download as CSV
/help - More info"""
    await update.message.reply_text(msg)

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /stats"""
    user_id = update.effective_user.id
    data = get_user_stats(user_id)
    msg = f"""📊 Your Stats

Total leads: {data['total_leads']}
Average score: {data['avg_score']}/10"""
    await update.message.reply_text(msg)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /help"""
    msg = """LeadBot extracts qualified leads from Telegram groups.

/start - Welcome
/stats - Your statistics
/export - Download leads
/help - This message"""
    await update.message.reply_text(msg)

async def export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /export"""
    user_id = update.effective_user.id
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT name, email, phone, company, interest, score FROM leads WHERE user_telegram_id = %s", (user_id,))
        leads = cursor.fetchall()
        if not leads:
            await update.message.reply_text("No leads yet")
            return
        csv = "name,email,phone,company,interest,score\n"
        for lead in leads:
            csv += f'"{lead[0] or ""}","{lead[1] or ""}","{lead[2] or ""}","{lead[3] or ""}","{lead[4] or ""}",{lead[5]}\n'
        await update.message.reply_document(document=csv.encode(), filename=f"leads_{user_id}.csv")
    finally:
        cursor.close()
        conn.close()

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle group messages"""
    if update.message.chat.type not in ['group', 'supergroup']:
        return
    
    text = update.message.text
    if not text:
        return
    
    print(f"[DEBUG] Message: {text[:50]}")
    
    lead_info = extract_lead_info(text)
    if lead_info:
        print(f"[DEBUG] Lead found: {lead_info}")
        score = score_lead(lead_info)
        print(f"[DEBUG] Score: {score}")
        if score >= 3:
            user_id = update.effective_user.id
            store_lead(user_id, lead_info, score)

def main():
    print("[INFO] Starting bot...")
    init_db()
    
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("export", export))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    print("[INFO] Bot polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
