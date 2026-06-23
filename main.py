import os
import json
import logging
import sqlite3
from contextlib import asynccontextmanager
from typing import Optional, Dict, Any

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.client.default import DefaultBotProperties
from fastapi import FastAPI, Request
import uvicorn
from playwright.async_api import async_playwright
from apscheduler.schedulers.asyncio import AsyncIOScheduler

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
AGENT_USERNAME = os.getenv("AGENT_USERNAME")
AGENT_PASSWORD = os.getenv("AGENT_PASSWORD")
PARENT_ID = os.getenv("PARENT_ID", "2751155")
BASE_URL = os.getenv("BASE_URL", "https://agents.ichancy.com")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
PORT = int(os.getenv("PORT", 10000))

DB_PATH = "bot_data.db"

# ---------- قاعدة البيانات ----------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            player_id TEXT NOT NULL,
            email TEXT,
            login TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

def db_get_user(telegram_id: int) -> Optional[dict]:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT telegram_id, player_id, email, login FROM users WHERE telegram_id=?", (telegram_id,))
    row = c.fetchone()
    conn.close()
    if row:
        return {"telegram_id": row[0], "player_id": row[1], "email": row[2], "login": row[3]}
    return None

def db_create_user(telegram_id: int, player_id: str, email: str, login: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO users (telegram_id, player_id, email, login) VALUES (?,?,?,?)",
              (telegram_id, player_id, email, login))
    conn.commit()
    conn.close()

# ---------- مدير المتصفح الدائم ----------
class BrowserManager:
    def __init__(self):
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None

    async def start(self):
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(headless=True)
        self.context = await self.browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
        )
        self.page = await self.context.new_page()
        # حقن لإخفاء علامات الأتمتة الأساسية
        await self.page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
            Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
        """)
        # فتح الصفحة الرئيسية وتجاوز Cloudflare
        logger.info("فتح الصفحة الرئيسية...")
        await self.page.goto(BASE_URL, wait_until="networkidle", timeout=60000)
        await self.page.wait_for_timeout(8000)
        logger.info("تم تجاوز Cloudflare والمتصفح جاهز.")

    async def api_call(self, endpoint: str, data: Dict[str, Any]) -> Optional[Dict]:
        """يرسل طلب API من خلال page.request.post (الذي يشارك الكوكيز) مع ترويسات Client Hints"""
        url = f"{BASE_URL}{endpoint}"
        headers = {
            "Content-Type": "application/json",
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/plain, */*",
            "Referer": BASE_URL + "/login",
            "Origin": BASE_URL,
            "sec-ch-ua": '"Chromium";v="125", "Not.A/Brand";v="24"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "Keep-Alive": "True"
        }
        try:
            response = await self.page.request.post(url, data=json.dumps(data), headers=headers)
            body = await response.text()
            if response.status == 200:
                try:
                    return json.loads(body)
                except:
                    return {"raw": body}
            else:
                logger.error(f"API call failed: {response.status} - {body[:300]}")
                return None
        except Exception as e:
            logger.exception(f"Error calling API {endpoint}")
            return None

    async def login(self) -> bool:
        payload = {"username": AGENT_USERNAME, "password": AGENT_PASSWORD}
        result = await self.api_call("/global/api/User/signIn", payload)
        if result is not None:
            logger.info("تم تسجيل الدخول بنجاح.")
            return True
        logger.error("فشل تسجيل الدخول.")
        return False

    async def close(self):
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()

browser_mgr = BrowserManager()

# ---------- دالة استدعاء API مختصرة ----------
async def call_api(endpoint: str, data: dict) -> Optional[dict]:
    return await browser_mgr.api_call(endpoint, data)

# ---------- البوت ----------
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()

@dp.message(Command("start"))
async def cmd_start(message: Message):
    user = db_get_user(message.from_user.id)
    if user:
        await message.answer("أهلاً بعودتك!\n/balance\n/deposit <المبلغ>\n/withdraw <المبلغ>")
    else:
        await message.answer("مرحبًا! أرسل /register لإنشاء لاعب جديد.")

@dp.message(Command("register"))
async def cmd_register(message: Message):
    user = db_get_user(message.from_user.id)
    if user:
        await message.answer("لديك لاعب بالفعل.")
        return
    telegram_id = message.from_user.id
    login = f"tg{telegram_id}"
    email = f"{login}@ichancy-bot.local"
    password = "AutoGen123!"
    payload = {
        "player": {
            "email": email,
            "password": password,
            "parentId": PARENT_ID,
            "login": login,
            "countryCode": "SY"
        }
    }
    await message.answer("جاري إنشاء حسابك...")
    result = await call_api("/global/api/Player/registerPlayer", payload)
    if result and "playerId" in result:
        player_id = result["playerId"]
        db_create_user(telegram_id, player_id, email, login)
        await message.answer(
            f"✅ تم إنشاء لاعبك!\n<b>المعرف:</b> <code>{player_id}</code>\n"
            f"البريد: {email}\nكلمة المرور: {password}\n\n"
            "استخدم:\n/balance\n/deposit <المبلغ>\n/withdraw <المبلغ>"
        )
    else:
        await message.answer("❌ فشل إنشاء اللاعب.")

@dp.message(Command("balance"))
async def cmd_balance(message: Message):
    user = db_get_user(message.from_user.id)
    if not user:
        await message.answer("ليس لديك لاعب. أرسل /register أولاً.")
        return
    result = await call_api("/global/api/Player/getPlayerBalanceById", {"playerId": user["player_id"]})
    if result and "balance" in result:
        await message.answer(f"💰 رصيدك الحالي: {result['balance']} NSP")
    else:
        await message.answer("❌ تعذر جلب الرصيد.")

@dp.message(Command("deposit"))
async def cmd_deposit(message: Message):
    user = db_get_user(message.from_user.id)
    if not user:
        await message.answer("ليس لديك لاعب. أرسل /register أولاً.")
        return
    try:
        amount = float(message.text.split()[1])
        if amount <= 0: raise ValueError
    except:
        await message.answer("مثال: /deposit 500")
        return
    result = await call_api("/global/api/Player/depositToPlayer", {
        "amount": amount, "comment": None, "playerId": user["player_id"],
        "currencyCode": "NSP", "moneyStatus": 5
    })
    await message.answer("✅ تم الإيداع." if result else "❌ فشل الإيداع.")

@dp.message(Command("withdraw"))
async def cmd_withdraw(message: Message):
    user = db_get_user(message.from_user.id)
    if not user:
        await message.answer("ليس لديك لاعب. أرسل /register أولاً.")
        return
    try:
        amount = float(message.text.split()[1])
        if amount <= 0: raise ValueError
    except:
        await message.answer("مثال: /withdraw 200")
        return
    result = await call_api("/global/api/Player/withdrawFromPlayer", {
        "amount": -amount, "comment": None, "playerId": user["player_id"],
        "currencyCode": "NSP", "moneyStatus": 5
    })
    await message.answer("✅ تم السحب." if result else "❌ فشل السحب.")

# ---------- دورة الحياة ----------
scheduler = AsyncIOScheduler()

async def session_keepalive():
    logger.info("فحص صحة الجلسة...")
    test = await browser_mgr.api_call("/global/api/Statistics/getPlayersStatisticsPro", {"start":0,"limit":1,"filter":{}})
    if test is None:
        logger.warning("فشل اختبار الجلسة، إعادة تشغيل المتصفح...")
        await browser_mgr.close()
        await browser_mgr.start()
        await browser_mgr.login()

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    await browser_mgr.start()
    login_ok = await browser_mgr.login()
    if not login_ok:
        logger.error("تعذر تسجيل الدخول الأولي. سيتم إعادة المحاولة.")
    scheduler.add_job(session_keepalive, "interval", minutes=5)
    scheduler.start()
    webhook_url = f"{WEBHOOK_URL}/webhook"
    await bot.set_webhook(webhook_url)
    logger.info(f"Webhook set to {webhook_url}")
    yield
    await bot.delete_webhook()
    scheduler.shutdown()
    await browser_mgr.close()

app = FastAPI(lifespan=lifespan)

@app.post("/webhook")
async def telegram_webhook(request: Request):
    update = await request.json()
    telegram_update = types.Update(**update)
    await dp.feed_update(bot, telegram_update)
    return {"ok": True}

@app.get("/health")
async def health():
    return {"status": "ok"}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
