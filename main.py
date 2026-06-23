import os
import json
import logging
import sqlite3
from contextlib import asynccontextmanager
from typing import Optional, Dict, Any

from dotenv import load_dotenv
import aiohttp
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.client.default import DefaultBotProperties
from fastapi import FastAPI, Request
import uvicorn
from curl_cffi import requests as curl_requests
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

# ---------- مدير الجلسة باستخدام curl_cffi ----------
class SessionManager:
    def __init__(self):
        self.session = None
        self.cookies = None

    def login(self):
        # إنشاء جلسة curl_cffi ببصمة Chrome 120
        self.session = curl_requests.Session(impersonate="chrome120")

        # 1. زيارة الصفحة الرئيسية للحصول على كوكيز Cloudflare
        logger.info("طلب الصفحة الرئيسية للحصول على كوكيز Cloudflare...")
        resp = self.session.get(BASE_URL, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        })
        logger.info(f"استجابة الصفحة الرئيسية: {resp.status_code}")

        # 2. تسجيل الدخول
        login_url = f"{BASE_URL}/global/api/User/signIn"
        login_payload = {"username": AGENT_USERNAME, "password": AGENT_PASSWORD}
        headers = {
            "Content-Type": "application/json",
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/plain, */*",
            "Referer": BASE_URL + "/login",
            "Origin": BASE_URL
        }
        logger.info("إرسال طلب تسجيل الدخول...")
        resp = self.session.post(login_url, json=login_payload, headers=headers)
        if resp.status_code == 200:
            logger.info("تم تسجيل الدخول بنجاح.")
            self.cookies = self.session.cookies.get_dict()
            return True
        else:
            logger.error(f"فشل تسجيل الدخول: {resp.status_code} - {resp.text[:300]}")
            return False

    def api_call(self, endpoint: str, data: Dict[str, Any]) -> Optional[Dict]:
        url = f"{BASE_URL}{endpoint}"
        headers = {
            "Content-Type": "application/json",
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/plain, */*",
            "Referer": BASE_URL + "/login",
            "Origin": BASE_URL
        }
        try:
            resp = self.session.post(url, json=data, headers=headers)
            if resp.status_code == 200:
                return resp.json()
            else:
                logger.error(f"API call failed: {resp.status_code} - {resp.text[:200]}")
                return None
        except Exception as e:
            logger.exception(f"Error calling API {endpoint}")
            return None

    def test_session(self) -> bool:
        test_url = f"{BASE_URL}/global/api/Statistics/getPlayersStatisticsPro"
        payload = {"start": 0, "limit": 1, "filter": {}}
        try:
            resp = self.session.post(test_url, json=payload, headers={
                "Content-Type": "application/json",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": BASE_URL + "/login"
            })
            return resp.status_code == 200
        except:
            return False

session_mgr = SessionManager()

# ---------- دوال مساعدة ----------
async def call_api(endpoint: str, data: dict) -> Optional[dict]:
    return session_mgr.api_call(endpoint, data)

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

def session_keepalive():
    logger.info("فحص صحة الجلسة...")
    if not session_mgr.test_session():
        logger.warning("فشل اختبار الجلسة، إعادة تسجيل الدخول...")
        session_mgr.login()

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    success = session_mgr.login()
    if not success:
        logger.error("فشل تسجيل الدخول الأولي. البوت قد لا يعمل.")
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
