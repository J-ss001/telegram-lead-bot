import os
import re
import asyncio
import psycopg2
from datetime import datetime
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from anthropic import Anthropic

# Environment variables
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CLAUDE_API_KEY = os.environ.get("CLAUDE_API_KEY")
DATABASE_URL = os.environ.get("DATABASE_URL")

# Validate environment variables
if not TELEGRAM_BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN not set")
if not CLAUDE_API_KEY:
    raise ValueError("CLAUDE_API_KEY not set")
if not DATABASE_URL:
    raise ValueError("DATABASE_URL not set")

# Initialize Claude client
client = Anthropic()

# Database connection with retries
def get_connection(retries=3):
    for attempt in range(1, retries + 1):
        try:
            conn = psycopg2.connect(DATABASE_URL, connect_timeout=5)
            print(f"[INFO] Database connection successful (attempt {attempt})")
            return conn
        except psycopg2.OperationalError as e:
            print(f"[ERROR] DB connection attempt {attempt}/{retries} failed: {e}")
            if attempt == retries:
                raise RuntimeError(f"Could not connect to database after {retries} attempts.")
            import time
            time.sleep(1)

# Initialize database tables
def init_db():
    print("[INFO] Initialising database...")
    conn = get_connection()
    cursor = conn.cursor()
    
    try:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id BIGINT PRIMARY KEY,
                username VARCHAR(255),
                subscription_status VARCHAR(20) DEFAULT 'free',
                subscription_start_date TIMESTAMP DEFAULT NOW(),
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
        
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_leads_user ON leads(user_telegram_id)
        """)
        
        conn.commit()
        print("[INFO] Database tables verified / created.")
    except Exception as e:
        print(f"[ERROR] Database initialization failed: {e}")
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()

# Extract lead information from message
def extract_lead_info(text):
    """Extract name, email, phone, company, interest from message text"""
    lead = {
        'name': None,
        'email': None,
        'phone': None,
        'company': None,
        'interest': None
    }
    
    # Email pattern
    email_match = re.search(r'[\w\.-]+@[\w\.-]+\.\w+', text)
    if email_match:
        lead['email'] = email_match.group()
    
    # Phone pattern (handles +27, 0, etc)
    phone_match = re.search(r'(\+?\d{1,3}[-.\s]?\d{1,14})', text)
    if phone_match:
        lead['phone'] = phone_match.group()
    
    # Look for company patterns
    company_match = re.search(r'(?:company|corp|co|ltd|inc|designer|graphic|saas)[:\s]+([A-Za-z\s&]+?)(?:[,\.]|$)', text, re.IGNORECASE)
    if company_match:
        lead['company'] = company_match.group(1).strip()
    
    # Look for name patterns
    name_match = re.search(r"(?:name|i'm|i am|im|hi)[:\s]+([A-Za-z\s]+?)(?:[,\.]|$)", text, re.IGNORECASE)
    if name_match:
        lead['name'] = name_match.group(1).strip()
    
    # Look for interest/looking for patterns
    interest_match = re.search(r'(?:looking for|interested in|need)[:\s]+([A-Za-z\s&]+?)(?:[,\.]|$)', text, re.IGNORECASE)
    if interest_match:
        lead['interest'] = interest_match.group(1).strip()
    
    # Check if we extracted meaningful data
    if lead['email'] or lead['phone'] or lead['name']:
        return lead
    
    return None

# Score lead with Claude AI
def score_lead(lead_info):
    """Use Claude to score lead quality 1-10"""
    try:
        prompt = f"""Rate this lead on a scale of 1-10 based on how likely they are to be a qualified buyer.
        
Lead Information:
- Name: {lead_info.get('name', 'Unknown')}
- Email: {lead_info.get('email', 'Not provided')}
- Phone: {lead_info.get('phone', 'Not provided')}
- Company: {lead_info.get('company', 'Not provided')}
- Interest: {lead_info.get('interest', 'Not provided')}

Only respond with a single number from 1-10. Nothing else."""
        
        response = client.messages.create(
            model="claude-opus-4-1-20250805",
            max_tokens=5,
            messages=[
                {"role": "user", "content": prompt}
            ]
        )
        
        # Extract score from response
        score_text = response.content[0].text.strip()
        score_match = re.search(r'\d+', score_text)
        if score_match:
            score = int(score_match.group())
            score = max(1, min(10, score))  # Clamp to 1-10
            return score
        return 6  # Default score if Claude doesn't respond with number
    
    except Exception as e:
        print(f"[ERROR] Claude scoring failed: {e}")
        return 6  # Default score - more lenient

# Store lead in database
def store_lead(user_id, lead_info, score):
    """Store extracted lead in database"""
    conn = get_connection()
    cursor = conn.cursor()
    
    try:
        # Ensure user exists
        cursor.execute("""
            INSERT INTO users (telegram_id) VALUES (%s)
            ON CONFLICT (telegram_id) DO NOTHING
        """, (user_id,))
        
        # Store lead
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
            score
        ))
        
        conn.commit()
        print(f"[INFO] Lead stored for user {user_id}: {lead_info.get('email', 'No email')} (Score: {score})")
    except Exception as e:
        print(f"[ERROR] Failed to store lead: {e}")
        conn.rollback()
    finally:
        cursor.close()
        conn.close()

# Get user statistics
def get_user_stats(user_id):
    """Get lead statistics for user"""
    conn = get_connection()
    cursor = conn.cursor()
    
    try:
        cursor.execute("""
            SELECT COUNT(*) as total_leads, AVG(score) as avg_score
            FROM leads
            WHERE user_telegram_id = %s
        """, (user_id,))
        
        result = cursor.fetchone()
        total = result[0] if result[0] else 0
        avg_score = round(result[1], 1) if result[1] else 0
        
        # Count today's leads
        cursor.execute("""
            SELECT COUNT(*) FROM leads
            WHERE user_telegram_id = %s AND DATE(created_at) = CURRENT_DATE
        """, (user_id,))
        
        today_leads = cursor.fetchone()[0]
        
        return {
            'total_leads': total,
            'avg_score': avg_score,
            'today_leads': today_leads
        }
    except Exception as e:
        print(f"[ERROR] Failed to get stats: {e}")
        return {'total_leads': 0, 'avg_score': 0, 'today_leads': 0}
    finally:
        cursor.close()
        conn.close()

# Export leads as CSV
def export_leads(user_id):
    """Export user's leads as CSV"""
    conn = get_connection()
    cursor = conn.cursor()
    
    try:
        cursor.execute("""
            SELECT name, email, phone, company, interest, score
            FROM leads
            WHERE user_telegram_id = %s
            ORDER BY created_at DESC
        """, (user_id,))
        
        leads = cursor.fetchall()
        
        if not leads:
            return None
        
        # Format as CSV
        csv = "name,email,phone,company,interest,score\n"
        for lead in leads:
            csv += f'"{lead[0] or ""}","{lead[1] or ""}","{lead[2] or ""}","{lead[3] or ""}","{lead[4] or ""}",{lead[5]}\n'
        
        return csv
    except Exception as e:
        print(f"[ERROR] Failed to export leads: {e}")
        return None
    finally:
        cursor.close()
        conn.close()

# Command Handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command"""
    user_id = update.effective_user.id
    
    # Register user
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO users (telegram_id) VALUES (%s)
        ON CONFLICT (telegram_id) DO NOTHING
    """, (user_id,))
    conn.commit()
    cursor.close()
    conn.close()
    
    message = """Welcome! 🎉 I'm the Telegram Lead Bot.

I automatically extract and score leads from this group.

Commands:
/stats - Show your lead statistics
/export - Download your leads as CSV
/help - Show all commands

Just add me to a group and I'll start extracting leads!"""
    
    await update.message.reply_text(message)

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /stats command"""
    user_id = update.effective_user.id
    stats_data = get_user_stats(user_id)
    
    message = f"""📊 Your Lead Statistics

Total leads captured: {stats_data['total_leads']}
Average lead score: {stats_data['avg_score']} / 10
Today's leads: {stats_data['today_leads']}

Upgrade to Pro to get unlimited leads and features!"""
    
    await update.message.reply_text(message)

async def export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /export command"""
    user_id = update.effective_user.id
    csv_data = export_leads(user_id)
    
    if csv_data is None:
        await update.message.reply_text("❌ You have no leads to export yet")
        return
    
    # Send as file
    await update.message.reply_document(
        document=csv_data.encode(),
        filename=f"leads_{user_id}_{datetime.now().strftime('%Y%m%d')}.csv",
        caption="📥 Your extracted leads (CSV format)"
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /help command"""
    message = """🤖 Telegram Lead Bot Commands

/start - Welcome message
/stats - Show your lead statistics  
/export - Download leads as CSV
/help - Show this help message

How it works:
1. Add me to your group
2. Post leads in the group (name, email, phone, company)
3. I automatically extract and score them
4. Use /stats to see results
5. Use /export to download CSV

Features:
✅ Automatic lead extraction
✅ AI quality scoring (1-10)
✅ CSV export
✅ Lead statistics"""
    
    await update.message.reply_text(message)

async def handle_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle messages in groups - extract leads"""
    # Only process messages in groups
    if update.message.chat.type not in ['group', 'supergroup']:
        return
    
    # Get message text
    text = update.message.text
    if not text:
        return
    
    # Extract lead information
    lead_info = extract_lead_info(text)
    
    if lead_info:
        print(f"[INFO] Potential lead detected: {lead_info}")
        
        # Score the lead
        score = score_lead(lead_info)
        print(f"[INFO] Lead scored: {score}/10")
        
        # Store if score >= 3 (more lenient for testing)
        if score >= 3:
            user_id = update.effective_user.id
            store_lead(user_id, lead_info, score)
            print(f"[INFO] Lead stored (score: {score})")
        else:
            print(f"[INFO] Lead skipped (score too low: {score})")

def main():
    """Main function - initialize and run bot"""
    print("[INFO] Initialising database...")
    init_db()
    
    print("[INFO] Building Telegram application...")
    
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    # Add command handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("export", export))
    app.add_handler(CommandHandler("help", help_command))
    
    # Add message handler for group messages
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_group_message))
    
    print("[INFO] Bot started. Polling for updates...")
    
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
