import os
import json
import logging
import sqlite3
from contextlib import asynccontextmanager
from typing import Optional, Dict, Any
import asyncio

from dotenv import load_dotenv
import aiohttp
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.client.default import DefaultBotProperties
from fastapi import FastAPI, Request
import uvicorn
import cloudscraper
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
    c.execute("""
        CREATE TABLE IF NOT EXISTS agent_session (
            id INTEGER PRIMARY KEY CHECK(id=1),
            cookies TEXT,
            last_login TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

def db_get_session_cookies() -> Optional[str]:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT cookies FROM agent_session WHERE id=1")
    row = c.fetchone()
    conn.close()
    if row:
        return row[0]
    return None

def db_save_session_cookies(cookies: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO agent_session (id, cookies, last_login) VALUES (1, ?, CURRENT_TIMESTAMP)", (cookies,))
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

# ---------- مدير الجلسة (cloudscraper + وضع يدوي) ----------
scraper = None  # cloudscraper session
manual_cookies = None  # cookies provided by user via /setcookie

def try_auto_login() -> Optional[str]:
    """محاولة تسجيل الدخول تلقائياً باستخدام cloudscraper"""
    global scraper
    try:
        scraper = cloudscraper.create_scraper(
            browser={
                'browser': 'chrome',
                'platform': 'windows',
                'mobile': False
            }
        )
        # 1. جلب الصفحة الرئيسية لتفعيل Cloudflare
        logger.info("جلب الصفحة الرئيسية عبر cloudscraper...")
        resp = scraper.get(BASE_URL)
        logger.info(f"استجابة الصفحة الرئيسية: {resp.status_code}")

        # 2. تسجيل الدخول
        login_url = f"{BASE_URL}/global/api/User/signIn"
        payload = {"username": AGENT_USERNAME, "password": AGENT_PASSWORD}
        headers = {
            "Content-Type": "application/json",
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/plain, */*",
            "Referer": BASE_URL + "/login",
            "Origin": BASE_URL
        }
        resp = scraper.post(login_url, json=payload, headers=headers)
        if resp.status_code == 200:
            # استخراج الكوكيز
            cookies_dict = scraper.cookies.get_dict()
            cookie_str = "; ".join([f"{k}={v}" for k, v in cookies_dict.items()])
            logger.info("تم تسجيل الدخول تلقائياً!")
            return cookie_str
        else:
            logger.error(f"فشل تلقائي: {resp.status_code} - {resp.text[:200]}")
            return None
    except Exception as e:
        logger.exception("خطأ في المحاولة التلقائية")
        return None

def api_call_via_cloudscraper(endpoint: str, data: Dict[str, Any]) -> Optional[Dict]:
    global scraper
    if not scraper:
        return None
    url = f"{BASE_URL}{endpoint}"
    headers = {
        "Content-Type": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": BASE_URL + "/login",
        "Origin": BASE_URL
    }
    try:
        resp = scraper.post(url, json=data, headers=headers)
        if resp.status_code == 200:
            return resp.json()
        else:
            logger.error(f"API call failed: {resp.status_code} - {resp.text[:200]}")
            return None
    except Exception as e:
        logger.exception(f"Error calling API {endpoint}")
        return None

def api_call_via_cookies(endpoint: str, data: Dict[str, Any]) -> Optional[Dict]:
    """استخدام الكوكيز اليدوية مع aiohttp (بشكل متزامن)"""
    cookies = db_get_session_cookies()
    if not cookies:
        return None
    url = f"{BASE_URL}{endpoint}"
    headers = {
        "Content-Type": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": BASE_URL + "/login",
        "Origin": BASE_URL,
        "Cookie": cookies
    }
    # استخدام requests عادي متزامن لأن aiohttp غير متزامن، لكننا سنلفه في async لاحقاً
    import requests
    try:
        resp = requests.post(url, json=data, headers=headers)
        if resp.status_code == 200:
            return resp.json()
        else:
            logger.error(f"API call (manual cookies) failed: {resp.status_code} - {resp.text[:200]}")
            return None
    except Exception as e:
        logger.exception(f"Error calling API {endpoint}")
        return None

async def call_api(endpoint: str, data: dict) -> Optional[dict]:
    # تفضل الطريقة اليدوية إذا كانت الكوكيز موجودة
    if db_get_session_cookies():
        # تشغيل في thread pool لأن requests متزامن
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, api_call_via_cookies, endpoint, data)
    # وإلا جرب التلقائي
    return api_call_via_cloudscraper(endpoint, data)

# ---------- البوت ----------
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()

@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer("مرحبًا! للاستخدام:\n/register - إنشاء لاعب\n/balance - عرض الرصيد\n/deposit <مبلغ>\n/withdraw <مبلغ>\n\nللمشرف: /setcookie <الكوكيز> لتحديث الجلسة")

@dp.message(Command("setcookie"))
async def cmd_setcookie(message: Message):
    # استخراج الكوكيز من الرسالة
    try:
        cookie_text = message.text.replace("/setcookie", "").strip()
        if not cookie_text:
            await message.answer("يرجى إرسال الكوكيز بعد الأمر. مثال:\n/setcookie PHPSESSID=abc; cf_clearance=xyz; ...")
            return
        db_save_session_cookies(cookie_text)
        await message.answer("✅ تم حفظ الكوكيز بنجاح. جرب /balance الآن.")
    except Exception as e:
        await message.answer("فشل حفظ الكوكيز. تأكد من الصيغة.")

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
        await message.answer("❌ فشل إنشاء اللاعب. تأكد من صلاحية الجلسة (/setcookie)")

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
        await message.answer("❌ تعذر جلب الرصيد. ربما انتهت الجلسة، استخدم /setcookie.")

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
    logger.info("فحص الجلسة...")
    if db_get_session_cookies():
        # اختبار الكوكيز اليدوية
        test = api_call_via_cookies("/global/api/Statistics/getPlayersStatisticsPro", {"start":0,"limit":1,"filter":{}})
        if test is None:
            logger.warning("الكوكيز اليدوية غير صالحة، انتظر تحديث")
    else:
        # وضع تلقائي
        test = api_call_via_cloudscraper("/global/api/Statistics/getPlayersStatisticsPro", {"start":0,"limit":1,"filter":{}})
        if test is None:
            logger.warning("فشل تلقائي للجلسة")

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # محاولة تلقائية أولى
    auto_cookies = try_auto_login()
    if auto_cookies:
        db_save_session_cookies(auto_cookies)
    else:
        logger.warning("لم تنجح المحاولة التلقائية. في انتظار /setcookie")
    scheduler.add_job(session_keepalive, "interval", minutes=10)
    scheduler.start()
    webhook_url = f"{WEBHOOK_URL}/webhook"
    await bot.set_webhook(webhook_url)
    logger.info(f"Webhook set to {webhook_url}")
    yield
    await bot.delete_webhook()
    scheduler.shutdown()

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
