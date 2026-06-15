import json, os, asyncio, logging, time
import threading  # مضاف للتشغيل المتوازي
from http.server import SimpleHTTPRequestHandler, HTTPServer  # مضاف للسيرفر الوهمي
from contextlib import asynccontextmanager
from groq import AsyncGroq
from telegram import Update, constants
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, CommandHandler, filters
from collections import defaultdict
from dotenv import load_dotenv

# تحميل المفاتيح من ملف خارجي (لن يتم رفعه)
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

MEMORY_FILE = "memory.json"
MAX_HISTORY = 10
REQUEST_TIMEOUT = 30

# ============ التسجيل (Logging) ============
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[logging.FileHandler("bot.log", encoding="utf-8"), logging.StreamHandler()],
)
logger = logging.getLogger(__name__)

# ============ السيرفر الوهمي لإقناع Render المجاني ============
def run_dummy_server():
    try:
        # يقرأ البورت التلقائي من Render أو يستخدم 10000 كافتراضي
        port = int(os.environ.get("PORT", 10000))
        server = HTTPServer(('0.0.0.0', port), SimpleHTTPRequestHandler)
        logger.info(f"🌍 Dummy server started on port {port}")
        server.serve_forever()
    except Exception as e:
        logger.error(f"فشل تشغيل السيرفر الوهمي: {e}")

# ============ Rate Limiting ============
class RateLimiter:
    def __init__(self, max_per_minute: int = 10):
        self.max = max_per_minute
        self.users = defaultdict(list)

    def is_allowed(self, user_id: int) -> bool:
        now = time.time()
        self.users[user_id] = [t for t in self.users[user_id] if now - t < 60]
        if len(self.users[user_id]) >= self.max: return False
        self.users[user_id].append(now)
        return True

rate_limiter = RateLimiter(max_per_minute=10)
memory_lock = asyncio.Lock()
client = AsyncGroq(api_key=GROQ_API_KEY)

# ============ الذاكرة ============
def load_memory() -> dict:
    if os.path.exists(MEMORY_FILE):
        try:
            with open(MEMORY_FILE, "r", encoding="utf-8") as f: return json.load(f)
        except: pass
    return {}

def save_memory(memory: dict):
    with open(MEMORY_FILE + ".tmp", "w", encoding="utf-8") as f: json.dump(memory, f, ensure_ascii=False, indent=2)
    os.replace(MEMORY_FILE + ".tmp", MEMORY_FILE)

user_memory = load_memory()
SYSTEM_PROMPT = "أنت مساعد ذكي ومفيد. تجاوب بالعربية بطلاقة وبأسلوب ودود ومختصر."

# ============ المهام ============
async def start(update, context):
    await update.message.reply_text("🤖 *أهلاً بك! أنا مساعدك الذكي.*\n\n💡 /clear لمسح الذاكرة.", parse_mode=constants.ParseMode.MARKDOWN)

async def clear_memory(update, context):
    user_id = str(update.effective_user.id)
    async with memory_lock:
        user_memory[user_id] = [{"role": "system", "content": SYSTEM_PROMPT}]
        save_memory(user_memory)
    await update.message.reply_text("✅ تم مسح الذاكرة بنجاح.")

@asynccontextmanager
async def typing_indicator(context, chat_id):
    task = context.application.create_task(_typing_loop(context, chat_id))
    try: yield
    finally: task.cancel()

async def _typing_loop(context, chat_id):
    while True:
        try:
            await context.bot.send_chat_action(chat_id=chat_id, action=constants.ChatAction.TYPING)
            await asyncio.sleep(4)
        except: break

async def handle_message(update, context):
    user_id = str(update.effective_user.id)
    if not rate_limiter.is_allowed(update.effective_user.id):
        await update.message.reply_text("⏳ تمهل قليلاً، انتظر دقيقة.")
        return

    async with memory_lock:
        if user_id not in user_memory: user_memory[user_id] = [{"role": "system", "content": SYSTEM_PROMPT}]
        user_memory[user_id].append({"role": "user", "content": update.message.text})
        if len(user_memory[user_id]) > MAX_HISTORY + 1: user_memory[user_id] = [user_memory[user_id][0]] + user_memory[user_id][-MAX_HISTORY:]

    try:
        async with typing_indicator(context, update.effective_chat.id):
            res = await asyncio.wait_for(client.chat.completions.create(messages=user_memory[user_id], model="llama-3.3-70b-versatile"), timeout=REQUEST_TIMEOUT)
        response = res.choices[0].message.content
        async with memory_lock:
            user_memory[user_id].append({"role": "assistant", "content": response})
            save_memory(user_memory)
        await update.message.reply_text(response)
    except Exception as e:
        logger.error(e)
        await update.message.reply_text("⚠️ حدث خطأ تقني.")

if __name__ == "__main__":
    if not BOT_TOKEN or not GROQ_API_KEY:
        print("خطأ: يرجى التأكد من وجود ملف .env وإضافة BOT_TOKEN و GROQ_API_KEY")
    else:
        # تشغيل السيرفر الوهمي كخيط فرعي آمن ومستقل قبل إطلاق البوت
        threading.Thread(target=run_dummy_server, daemon=True).start()
        
        app = ApplicationBuilder().token(BOT_TOKEN).build()
        app.add_handler(CommandHandler("start", start))
        app.add_handler(CommandHandler("clear", clear_memory))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
        print("✅ البوت يعمل الآن!")
        app.run_polling()
