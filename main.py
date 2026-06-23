from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from api.ichancy import IchancyAPI
import asyncio

BOT_TOKEN = "your_token"
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
api = IchancyAPI("your_email", "your_password")

class RegisterStates(StatesGroup):
    email = State()
    password = State()
    username = State()

class DepositStates(StatesGroup):
    player_id = State()
    amount = State()

# /start
@dp.message(Command("start"))
async def start(message: types.Message):
    kb = types.ReplyKeyboardMarkup(keyboard=[
        [types.KeyboardButton(text="📝 تسجيل لاعب جديد")],
        [types.KeyboardButton(text="💰 إيداع"), types.KeyboardButton(text="💸 سحب")],
        [types.KeyboardButton(text="💳 عرض الرصيد")]
    ], resize_keyboard=True)
    await message.answer("مرحباً! اختر العملية:", reply_markup=kb)

# تسجيل لاعب
@dp.message(F.text == "📝 تسجيل لاعب جديد")
async def register_start(message: types.Message, state: FSMContext):
    await state.set_state(RegisterStates.email)
    await message.answer("أدخل الإيميل:")

@dp.message(RegisterStates.email)
async def register_email(message: types.Message, state: FSMContext):
    await state.update_data(email=message.text)
    await state.set_state(RegisterStates.password)
    await message.answer("أدخل كلمة المرور:")

@dp.message(RegisterStates.password)
async def register_password(message: types.Message, state: FSMContext):
    await state.update_data(password=message.text)
    await state.set_state(RegisterStates.username)
    await message.answer("أدخل اسم المستخدم:")

@dp.message(RegisterStates.username)
async def register_done(message: types.Message, state: FSMContext):
    data = await state.get_data()
    result = api.register_player(data["email"], data["password"], message.text)
    await state.clear()
    await message.answer(f"✅ تم التسجيل:\n{result}")

if __name__ == "__main__":
    asyncio.run(dp.start_polling(bot))
