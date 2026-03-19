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

def get_connection():
    """Get database connection"""
    try:
        conn = psycopg2.connect(DATABASE_URL, connect_timeout=5)
        return conn
    except Exception as e:
        print(f"[ERROR] DB connection failed: {e}")
        raise

def init_db():
    """Initialize database tables"""
    print("[INFO] Initializing database...")
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id BIGINT PRIMARY KEY,
                username VARCHAR(255),
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
                score INT DEFAULT 7,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_leads_user ON leads(user_telegram_id)")
        conn.commit()
        print("[INFO] Database initialized successfully")
    except Exception as e:
        print(f"[ERROR] Database init failed: {e}")
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()

def extract_lead_info(text):
    """Extract lead data from message text"""
    lead = {
        'name': None,
        'email': None,
        'phone': None,
        'company': None,
        'interest': None
    }
    
    # Email
    email_match = re.search(r'[\w\.-]+@[\w\.-]+\.\w+', text)
    if email_match:
        lead['email'] = email_match.group()
    
    # Phone
    phone_match = re.search(r'(\+?\d{1,3}[-.\s]?\d{1,14})', text)
    if phone_match:
        lead['phone'] = phone_match.group()
    
    # Company
    company_match = re.search(r'(?:company|corp|co|ltd|inc|agency|business)[:\s]+([A-Za-z\s&]+?)(?:[,\.]|$)', text, re.IGNORECASE)
    if company_match:
        lead['company'] = company_match.group(1).strip()
    
    # Name
    name_match = re.search(r"(?:name|i'm|i am|hi)[:\s]+([A-Za-z\s]+?)(?:[,\.]|$)", text, re.IGNORECASE)
    if name_match:
        lead['name'] = name_match.group(1).strip()
    
    # Interest
    interest_match = re.search(r'(?:looking for|interested in|need)[:\s]+([A-Za-z\s&]+?)(?:[,\.]|$)', text, re.IGNORECASE)
    if interest_match:
        lead['interest'] = interest_match.group(1).strip()
    
    # Must have at least one field
    if lead['email'] or lead['phone'] or lead['name']:
        return lead
    
    return None

def store_lead(user_id, lead_info):
    """Store lead in database (without Claude scoring)"""
    conn = get_connection()
    cursor = conn.cursor()
    
    try:
        # Ensure user exists
        cursor.execute(
            "INSERT INTO users (telegram_id) VALUES (%s) ON CONFLICT (telegram_id) DO NOTHING",
            (user_id,)
        )
        
        # Store lead with default score of 7
        cursor.execute("""
            INSERT INTO leads (user_telegram_id, name, email, phone, company, interest, score)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (
            user_id,
            lead_info.get('name'),
            lead_info.get('email'),
            lead_info.get('phone'),
            lead_info.get('company'),
            lead_info.get('interest'),
            7  # Default score (temporary, will be replaced with Claude later)
        ))
        
        conn.commit()
        print(f"[INFO] ✅ Lead stored: {lead_info.get('email', 'No email')} (user: {user_id})")
        return True
    except Exception as e:
        print(f"[ERROR] Failed to store lead: {e}")
        conn.rollback()
        return False
    finally:
        cursor.close()
        conn.close()

def get_user_stats(user_id):
    """Get user's lead statistics"""
    conn = get_connection()
    cursor = conn.cursor()
    
    try:
        cursor.execute(
            "SELECT COUNT(*), AVG(score) FROM leads WHERE user_telegram_id = %s",
            (user_id,)
        )
        
        total, avg_score = cursor.fetchone()
        total = total or 0
        avg_score = round(avg_score, 1) if avg_score else 0
        
        print(f"[INFO] Stats for user {user_id}: total={total}, avg={avg_score}")
        
        return {
            'total_leads': total,
            'avg_score': avg_score
        }
    except Exception as e:
        print(f"[ERROR] Stats query failed: {e}")
        return {'total_leads': 0, 'avg_score': 0}
    finally:
        cursor.close()
        conn.close()

def export_leads_csv(user_id):
    """Export user's leads as CSV"""
    conn = get_connection()
    cursor = conn.cursor()
    
    try:
        cursor.execute(
            "SELECT name, email, phone, company, interest, score FROM leads WHERE user_telegram_id = %s ORDER BY created_at DESC",
            (user_id,)
        )
        
        leads = cursor.fetchall()
        
        if not leads:
            return None
        
        csv = "name,email,phone,company,interest,score\n"
        for lead in leads:
            csv += f'"{lead[0] or ""}","{lead[1] or ""}","{lead[2] or ""}","{lead[3] or ""}","{lead[4] or ""}",{lead[5]}\n'
        
        return csv
    except Exception as e:
        print(f"[ERROR] Export failed: {e}")
        return None
    finally:
        cursor.close()
        conn.close()

# Command handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command"""
    user_id = update.effective_user.id
    
    # Register user
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO users (telegram_id) VALUES (%s) ON CONFLICT (telegram_id) DO NOTHING",
        (user_id,)
    )
    conn.commit()
    cursor.close()
    conn.close()
    
    message = """🎉 Welcome to LeadBot!

I automatically extract leads from Telegram groups.

Commands:
/stats - View your lead statistics
/export - Download leads as CSV
/help - Show all commands

Add me to a group and I'll start extracting leads!"""
    
    await update.message.reply_text(message)
    print(f"[INFO] /start from user {user_id}")

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /stats command"""
    user_id = update.effective_user.id
    stats_data = get_user_stats(user_id)
    
    message = f"""📊 Your Lead Statistics

Total leads captured: {stats_data['total_leads']}
Average lead score: {stats_data['avg_score']} / 10

Upgrade to Pro for unlimited leads!"""
    
    await update.message.reply_text(message)
    print(f"[INFO] /stats from user {user_id}")

async def export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /export command"""
    user_id = update.effective_user.id
    csv_data = export_leads_csv(user_id)
    
    if csv_data is None:
        await update.message.reply_text("❌ You have no leads to export yet")
        return
    
    await update.message.reply_document(
        document=csv_data.encode(),
        filename=f"leads_{user_id}_{datetime.now().strftime('%Y%m%d')}.csv",
        caption="📥 Your extracted leads (CSV format)"
    )
    print(f"[INFO] /export from user {user_id}")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /help command"""
    message = """🤖 LeadBot Commands

/start - Welcome message
/stats - Show your lead statistics  
/export - Download leads as CSV
/help - Show this help message

How it works:
1. Add me to your Telegram group
2. Post leads in the group (name, email, phone, company)
3. I automatically extract and store them
4. Use /stats to see your leads
5. Use /export to download as CSV

Features:
✅ Automatic lead extraction
✅ Lead statistics
✅ CSV export
✅ Works in any group"""
    
    await update.message.reply_text(message)

async def handle_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle messages in groups - extract leads"""
    # Only process group messages
    if update.message.chat.type not in ['group', 'supergroup']:
        return
    
    text = update.message.text
    if not text:
        return
    
    user_id = update.effective_user.id
    print(f"[DEBUG] Message from {user_id}: {text[:60]}")
    
    # Extract lead
    lead_info = extract_lead_info(text)
    
    if lead_info:
        print(f"[INFO] ✅ Lead detected: {lead_info}")
        store_lead(user_id, lead_info)
    else:
        print(f"[DEBUG] No lead info extracted")

def main():
    """Main - initialize and run bot"""
    print("[INFO] Starting LeadBot...")
    
    init_db()
    
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    # Add handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("export", export))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_group_message))
    
    print("[INFO] Bot polling for updates...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
