import os
import json
import logging
import sqlite3
from contextlib import asynccontextmanager
from typing import Optional

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

# ---------- إعداد السجلات ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# ---------- المتغيرات ----------
BOT_TOKEN = os.getenv("BOT_TOKEN")
AGENT_USERNAME = os.getenv("AGENT_USERNAME")
AGENT_PASSWORD = os.getenv("AGENT_PASSWORD")
PARENT_ID = os.getenv("PARENT_ID", "2751155")
BASE_URL = os.getenv("BASE_URL", "https://agents.ichancy.com")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
PORT = int(os.getenv("PORT", 10000))

# ---------- قاعدة البيانات ----------
DB_PATH = "bot_data.db"

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
            cookies TEXT NOT NULL,
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
    c.execute("""
        INSERT OR REPLACE INTO agent_session (id, cookies, last_login)
        VALUES (1, ?, CURRENT_TIMESTAMP)
    """, (cookies,))
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

# ---------- Playwright: تسجيل الدخول والحصول على الكوكيز ----------
async def playwright_login() -> Optional[str]:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        try:
            logger.info("فتح الصفحة الرئيسية...")
            await page.goto(BASE_URL, wait_until="networkidle", timeout=60000)
            await page.wait_for_timeout(5000)

            login_url = f"{BASE_URL}/global/api/User/signIn"
            login_payload = {"username": AGENT_USERNAME, "password": AGENT_PASSWORD}
            logger.info("إرسال طلب تسجيل الدخول...")
            response = await page.request.post(
                login_url,
                data=json.dumps(login_payload),
                headers={"Content-Type": "application/json"}
            )
            if response.status != 200:
                logger.error(f"فشل تسجيل الدخول: {response.status}")
                return None

            await page.wait_for_timeout(2000)
            cookies = await context.cookies()
            cookie_str = "; ".join([f"{c['name']}={c['value']}" for c in cookies])
            logger.info("تم جمع الكوكيز بنجاح")
            return cookie_str
        except Exception as e:
            logger.exception("خطأ أثناء تسجيل الدخول عبر Playwright")
            return None
        finally:
            await browser.close()

async def refresh_session():
    logger.info("بدء تجديد الجلسة...")
    new_cookies = await playwright_login()
    if new_cookies:
        db_save_session_cookies(new_cookies)
        logger.info("تم تحديث الجلسة بنجاح")
        return new_cookies
    else:
        logger.error("فشل تجديد الجلسة")
        return None

async def get_valid_session() -> Optional[str]:
    cookies = db_get_session_cookies()
    if not cookies:
        return await refresh_session()
    if not await test_session(cookies):
        logger.warning("الجلسة منتهية الصلاحية، جاري التجديد...")
        return await refresh_session()
    return cookies

async def test_session(cookies: str) -> bool:
    test_url = f"{BASE_URL}/global/api/Statistics/getPlayersStatisticsPro"
    payload = {"start": 0, "limit": 1, "filter": {}}
    headers = {
        "Cookie": cookies,
        "Keep-Alive": "True",
        "Content-Type": "application/json"
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(test_url, json=payload, headers=headers, timeout=15) as resp:
                return resp.status == 200
    except:
        return False

async def api_call(endpoint: str, data: dict) -> Optional[dict]:
    cookies = await get_valid_session()
    if not cookies:
        logger.error("لا توجد جلسة API صالحة")
        return None
    url = f"{BASE_URL}{endpoint}"
    headers = {
        "Cookie": cookies,
        "Keep-Alive": "True",
        "Content-Type": "application/json"
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=data, headers=headers, timeout=30) as resp:
                if resp.status == 200:
                    return await resp.json()
                elif resp.status in (401, 403):
                    logger.warning(f"فشل المصادقة للطلب {endpoint}. تجديد الجلسة...")
                    new_cookies = await refresh_session()
                    if new_cookies:
                        headers["Cookie"] = new_cookies
                        async with session.post(url, json=data, headers=headers, timeout=30) as retry_resp:
                            if retry_resp.status == 200:
                                return await retry_resp.json()
                            else:
                                logger.error(f"فشل إعادة المحاولة: {retry_resp.status}")
                                return None
                    else:
                        return None
                else:
                    logger.error(f"خطأ API: {resp.status} - {await resp.text()}")
                    return None
    except Exception as e:
        logger.exception(f"استثناء أثناء استدعاء {endpoint}")
        return None

# ---------- البوت ----------
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()

@dp.message(Command("start"))
async def cmd_start(message: Message):
    user = db_get_user(message.from_user.id)
    if user:
        await message.answer(
            "أهلاً بعودتك! أنت مسجل بالفعل.\n"
            "استخدم الأوامر:\n"
            "/balance - عرض الرصيد\n"
            "/deposit <المبلغ> - إيداع\n"
            "/withdraw <المبلغ> - سحب"
        )
    else:
        await message.answer(
            "مرحبًا! أنت لم تسجل لاعبًا بعد.\n"
            "أرسل /register لإنشاء لاعب جديد تلقائيًا."
        )

@dp.message(Command("register"))
async def cmd_register(message: Message):
    user = db_get_user(message.from_user.id)
    if user:
        await message.answer("لديك بالفعل لاعب مسجل. استخدم /balance لعرض رصيدك.")
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
    result = await api_call("/global/api/Player/registerPlayer", payload)
    if result and "playerId" in result:
        player_id = result["playerId"]
        db_create_user(telegram_id, player_id, email, login)
        await message.answer(
            f"✅ تم إنشاء لاعبك بنجاح!\n\n"
            f"<b>معرف اللاعب:</b> <code>{player_id}</code>\n"
            f"<b>البريد:</b> {email}\n"
            f"<b>كلمة المرور:</b> {password}\n\n"
            "يمكنك الآن استخدام:\n/balance - لمعرفة رصيدك\n/deposit <المبلغ> - إيداع\n/withdraw <المبلغ> - سحب"
        )
    else:
        await message.answer("❌ فشل إنشاء اللاعب. قد يكون الوكيل غير مصرح له أو هناك مشكلة في الخدمة.")

@dp.message(Command("balance"))
async def cmd_balance(message: Message):
    user = db_get_user(message.from_user.id)
    if not user:
        await message.answer("ليس لديك لاعب. أرسل /register أولاً.")
        return
    payload = {"playerId": user["player_id"]}
    result = await api_call("/global/api/Player/getPlayerBalanceById", payload)
    if result and "balance" in result:
        balance = result.get("balance", "غير معروف")
        await message.answer(f"💰 رصيدك الحالي: {balance} NSP")
    else:
        await message.answer("❌ تعذر جلب الرصيد. حاول لاحقًا.")

@dp.message(Command("deposit"))
async def cmd_deposit(message: Message):
    user = db_get_user(message.from_user.id)
    if not user:
        await message.answer("ليس لديك لاعب. أرسل /register أولاً.")
        return
    try:
        amount_str = message.text.split()[1]
        amount = float(amount_str)
        if amount <= 0:
            raise ValueError
    except (IndexError, ValueError):
        await message.answer("الرجاء إدخال مبلغ موجب. مثال: /deposit 500")
        return
    payload = {
        "amount": amount,
        "comment": None,
        "playerId": user["player_id"],
        "currencyCode": "NSP",
        "moneyStatus": 5
    }
    result = await api_call("/global/api/Player/depositToPlayer", payload)
    if result:
        await message.answer(f"✅ تم إيداع {amount} NSP بنجاح.\nاستخدم /balance للتحقق من رصيدك.")
    else:
        await message.answer("❌ فشلت عملية الإيداع. تأكد من المبلغ ورصيد الوكيل.")

@dp.message(Command("withdraw"))
async def cmd_withdraw(message: Message):
    user = db_get_user(message.from_user.id)
    if not user:
        await message.answer("ليس لديك لاعب. أرسل /register أولاً.")
        return
    try:
        amount_str = message.text.split()[1]
        amount = float(amount_str)
        if amount <= 0:
            raise ValueError
    except (IndexError, ValueError):
        await message.answer("الرجاء إدخال مبلغ موجب. مثال: /withdraw 200")
        return
    payload = {
        "amount": -amount,
        "comment": None,
        "playerId": user["player_id"],
        "currencyCode": "NSP",
        "moneyStatus": 5
    }
    result = await api_call("/global/api/Player/withdrawFromPlayer", payload)
    if result:
        await message.answer(f"✅ تم سحب {amount} NSP بنجاح.\nاستخدم /balance للتحقق من رصيدك.")
    else:
        await message.answer("❌ فشلت عملية السحب. تأكد من وجود رصيد كافٍ.")

# ---------- خادم FastAPI مع lifespan صحيح ----------
scheduler = AsyncIOScheduler()

async def session_keepalive():
    logger.info("فحص دوري للجلسة...")
    cookies = db_get_session_cookies()
    if not cookies:
        await refresh_session()
    else:
        if not await test_session(cookies):
            await refresh_session()

@asynccontextmanager
async def lifespan(app: FastAPI):
    # بدء التشغيل
    init_db()
    await refresh_session()
    scheduler.add_job(session_keepalive, "interval", minutes=10)
    scheduler.start()
    webhook_url = f"{WEBHOOK_URL}/webhook"
    await bot.set_webhook(webhook_url)
    logger.info(f"Webhook set to {webhook_url}")
    yield
    # إنهاء
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
