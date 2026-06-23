import os
import json
import logging
import sqlite3
from contextlib import asynccontextmanager
from typing import Optional, Dict, Any
from urllib.parse import urljoin

from dotenv import load_dotenv
import aiohttp
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

# ---------- مدير المتصفح (يحاكي المستخدم الحقيقي) ----------
class BrowserManager:
    def __init__(self):
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.cookies_str = ""

    async def start(self):
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(headless=True)
        self.context = await self.browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
        )
        self.page = await self.context.new_page()

        # إخفاء علامات الأتمتة بشكل موسع
        await self.page.add_init_script("""
            // إخفاء webdriver
            Object.defineProperty(navigator, 'webdriver', { get: () => false });
            // تزوير plugins
            Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] });
            // تزوير languages
            Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
            // تزوير chrome
            window.chrome = { runtime: {} };
            // تزوير permissions
            const originalQuery = window.navigator.permissions.query;
            window.navigator.permissions.query = (parameters) => (
                parameters.name === 'notifications' ?
                Promise.resolve({ state: Notification.permission }) :
                originalQuery(parameters)
            );
        """)

        # 1. زيارة صفحة تسجيل الدخول (تجاوز Cloudflare وتكوين الجلسة)
        login_page_url = urljoin(BASE_URL, "/login")
        logger.info(f"زيارة صفحة الدخول: {login_page_url}")
        await self.page.goto(login_page_url, wait_until="networkidle", timeout=60000)
        await self.page.wait_for_timeout(5000)

        # 2. ملء نموذج الدخول وتسجيل الدخول
        logger.info("ملء بيانات الدخول...")
        try:
            # نبحث عن حقول البريد وكلمة المرور (قد تختلف selectors، نستخدم أسماء شائعة)
            await self.page.fill('input[type="email"], input[name="email"], input[name="username"]', AGENT_USERNAME)
            await self.page.fill('input[type="password"], input[name="password"]', AGENT_PASSWORD)
            # التقاط صورة للنموذج إذا أردت (اختياري)
            # await self.page.screenshot(path="login_form.png")
            await self.page.click('button[type="submit"], input[type="submit"], button:has-text("Login"), button:has-text("Sign in")')
            # انتظر حتى يتم التحويل أو ظهور رسالة نجاح
            await self.page.wait_for_load_state("networkidle")
            await self.page.wait_for_timeout(3000)
            logger.info("تم تقديم نموذج الدخول.")
        except Exception as e:
            logger.error(f"خطأ أثناء ملء النموذج: {e}")
            # محاولة بديلة: إرسال طلب API مباشر كحل أخير
            await self.api_login_fallback()

        # 3. جمع الكوكيز النهائية
        cookies = await self.context.cookies()
        self.cookies_str = "; ".join([f"{c['name']}={c['value']}" for c in cookies])
        logger.info(f"تم جمع الكوكيز: {self.cookies_str[:200]}...")
        # اختبار سريع للجلسة
        if not await self.test_session():
            logger.error("فشل اختبار الجلسة بعد تسجيل الدخول!")
        else:
            logger.info("الجلسة صالحة وتم تسجيل الدخول بنجاح.")

    async def api_login_fallback(self):
        """خطة بديلة: إرسال طلب API مباشر بنفس الكوكيز الحالية"""
        payload = {"username": AGENT_USERNAME, "password": AGENT_PASSWORD}
        url = f"{BASE_URL}/global/api/User/signIn"
        headers = {
            "Content-Type": "application/json",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": urljoin(BASE_URL, "/login"),
            "Origin": BASE_URL,
            "Cookie": self.cookies_str
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, headers=headers) as resp:
                    if resp.status == 200:
                        logger.info("تم تسجيل الدخول عبر API الاحتياطي.")
                    else:
                        logger.error(f"فشل API الاحتياطي: {resp.status}")
        except Exception as e:
            logger.error(f"خطأ في API الاحتياطي: {e}")

    async def test_session(self) -> bool:
        """اختبار صلاحية الجلسة عبر API خفيف"""
        test_url = f"{BASE_URL}/global/api/Statistics/getPlayersStatisticsPro"
        payload = {"start": 0, "limit": 1, "filter": {}}
        headers = {
            "Cookie": self.cookies_str,
            "Keep-Alive": "True",
            "Content-Type": "application/json"
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(test_url, json=payload, headers=headers, timeout=15) as resp:
                    return resp.status == 200
        except:
            return False

    async def api_call(self, endpoint: str, data: Dict[str, Any]) -> Optional[Dict]:
        """استدعاء API باستخدام كوكيز الجلسة المخزنة"""
        url = f"{BASE_URL}{endpoint}"
        headers = {
            "Cookie": self.cookies_str,
            "Keep-Alive": "True",
            "Content-Type": "application/json",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": urljoin(BASE_URL, "/login"),
            "Origin": BASE_URL
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=data, headers=headers, timeout=30) as resp:
                    body = await resp.text()
                    if resp.status == 200:
                        try:
                            return json.loads(body)
                        except:
                            return {"raw": body}
                    else:
                        logger.error(f"API call failed: {resp.status} - {body[:200]}")
                        return None
        except Exception as e:
            logger.exception(f"Error calling API {endpoint}")
            return None

    async def close(self):
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()

browser_mgr = BrowserManager()

async def call_api(endpoint: str, data: dict) -> Optional[dict]:
    return await browser_mgr.api_call(endpoint, data)

# ---------- البوت (كما هو) ----------
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
    if not await browser_mgr.test_session():
        logger.warning("فشل اختبار الجلسة، إعادة تشغيل المتصفح...")
        await browser_mgr.close()
        await browser_mgr.start()

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    await browser_mgr.start()
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
