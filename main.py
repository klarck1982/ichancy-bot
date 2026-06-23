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

# ---------- مدير المتصفح (انتظار الكوكيز ثم fetch) ----------
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

        # إخفاء خصائص الأتمتة
        await self.page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => false });
            Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] });
            Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
            window.chrome = { runtime: {} };
        """)

        # 1. فتح صفحة الدخول
        login_page_url = urljoin(BASE_URL, "/login")
        logger.info("زيارة صفحة الدخول...")
        await self.page.goto(login_page_url, wait_until="networkidle", timeout=60000)

        # 2. الانتظار حتى نحصل على كل من cf_clearance و __cf_bm (معًا)
        logger.info("انتظار كوكيز Cloudflare (cf_clearance و __cf_bm)...")
        for i in range(20):  # 20 محاولة، كل مرة ننتظر 1.5 ثانية
            cookies = {c["name"]: c["value"] for c in await self.context.cookies()}
            if "cf_clearance" in cookies and "__cf_bm" in cookies:
                logger.info(f"تم الحصول على الكوكيز المطلوبة: cf_clearance={cookies['cf_clearance'][:30]}..., __cf_bm={cookies['__cf_bm'][:30]}...")
                break
            await self.page.wait_for_timeout(1500)
        else:
            logger.warning("لم يتم الحصول على __cf_bm، استمرار مع cf_clearance فقط")

        # 3. تسجيل الدخول عبر fetch من داخل الصفحة
        login_payload = {"username": AGENT_USERNAME, "password": AGENT_PASSWORD}
        login_url = f"{BASE_URL}/global/api/User/signIn"
        js_code = """
        async (params) => {
            const response = await fetch(params.url, {
                method: 'POST',
                credentials: 'include',  // مهم: يرسل الكوكيز مع الطلب
                headers: {
                    'Content-Type': 'application/json',
                    'X-Requested-With': 'XMLHttpRequest',
                    'Accept': 'application/json, text/plain, */*',
                    'Referer': params.loginPageUrl
                },
                body: JSON.stringify(params.data)
            });
            const text = await response.text();
            return { status: response.status, body: text };
        }
        """
        result = await self.page.evaluate(js_code, {
            "url": login_url,
            "data": login_payload,
            "loginPageUrl": login_page_url
        })
        if result["status"] == 200:
            logger.info("تسجيل الدخول ناجح.")
        else:
            logger.error(f"فشل تسجيل الدخول: {result['status']} - {result['body'][:300]}")
            # محاولة بديلة باستخدام page.request.post مع الكوكيز المستخرجة
            logger.info("محاولة بديلة باستخدام page.request.post...")
            cookies_list = await self.context.cookies()
            headers = {
                "Content-Type": "application/json",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": login_page_url,
                "Origin": BASE_URL,
                "Cookie": "; ".join([f"{c['name']}={c['value']}" for c in cookies_list])
            }
            resp = await self.page.request.post(login_url, data=json.dumps(login_payload), headers=headers)
            if resp.status == 200:
                logger.info("تسجيل الدخول نجح عبر الطريقة البديلة.")
            else:
                logger.error(f"الطريقة البديلة فشلت: {resp.status} - {await resp.text()[:300]}")

        # 4. جمع الكوكيز النهائية (يجب أن تحتوي الآن على PHPSESSID)
        cookies_list = await self.context.cookies()
        self.cookies_str = "; ".join([f"{c['name']}={c['value']}" for c in cookies_list])
        logger.info("تم تحديث الكوكيز النهائية.")

        # 5. اختبار الجلسة
        if not await self.test_session():
            logger.error("فشل اختبار الجلسة بعد تسجيل الدخول!")
        else:
            logger.info("الجلسة صالحة وجاهزة.")

    async def test_session(self) -> bool:
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
                        return json.loads(body)
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
