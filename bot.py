import asyncio
import logging
import os
import sys
import aiosqlite
import aiohttp
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton,
    BufferedInputFile
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import TelegramBadRequest

# ==========================================
#        НАСТРОЙКИ (ИЗ ПЕРЕМЕННЫХ СРЕДЫ)
# ==========================================

# Ключи берутся из настроек сервера (Environment Variables)
API_TOKEN = os.getenv("BOT_TOKEN")
CMC_API_KEY = os.getenv("CMC_API_KEY")

# Списки администраторов (можно оставить в коде)
ADMIN_IDS = [
    1008747450, 
    1128228291,
]

MOD_IDS = [
    6061577974,
]

# ID групп для рассылки
TARGET_GROUPS = [
    -1003224850709,
]

# ID монеты FPI Bank
CMC_FPI_ID = "35859"

# ==========================================
#           СИСТЕМНЫЕ НАСТРОЙКИ
# ==========================================

ALL_STAFF_IDS = ADMIN_IDS + MOD_IDS
TZ_7 = timezone(timedelta(hours=7))
DB_NAME = "bot_data.db"

logging.basicConfig(level=logging.INFO, stream=sys.stdout)

# Проверка наличия ключей перед запуском
if not API_TOKEN:
    print("❌ КРИТИЧЕСКАЯ ОШИБКА: Не найден BOT_TOKEN в переменных окружения!")
    sys.exit(1)

bot = Bot(token=API_TOKEN)
dp = Dispatcher()

# --- Состояния ---
class BroadcastState(StatesGroup):
    waiting_for_content = State()
    choose_type = State()
    choose_time = State()
    waiting_for_date = State()

class AdminChatState(StatesGroup):
    active = State()

# --- БД ---
async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("CREATE TABLE IF NOT EXISTS scheduled (id INTEGER PRIMARY KEY AUTOINCREMENT, from_chat_id INTEGER, message_id INTEGER, run_time REAL, pin_mode INTEGER, status TEXT DEFAULT 'pending')")
        await db.execute("CREATE TABLE IF NOT EXISTS message_links (source_msg_id INTEGER, target_chat_id INTEGER, target_msg_id INTEGER)")
        await db.execute("CREATE TABLE IF NOT EXISTS admin_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, admin_id INTEGER, username TEXT, action TEXT, timestamp TEXT)")
        await db.commit()

async def log_action(user: types.User, action_text: str):
    try:
        now_str = datetime.now(TZ_7).strftime("%Y-%m-%d %H:%M:%S")
        username = user.username if user.username else user.first_name
        role = "ADMIN" if user.id in ADMIN_IDS else "MOD"
        log_text = f"[{role}] {action_text}"
        logging.info(f"[LOG] {username}: {log_text}")
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("INSERT INTO admin_logs (admin_id, username, action, timestamp) VALUES (?, ?, ?, ?)", (user.id, username, log_text, now_str))
            await db.commit()
    except Exception as e: logging.error(f"Log err: {e}")

async def add_scheduled_task(from_chat_id, message_id, run_time, pin_mode):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT INTO scheduled (from_chat_id, message_id, run_time, pin_mode) VALUES (?, ?, ?, ?)", (from_chat_id, message_id, run_time, pin_mode))
        await db.commit()

async def save_message_link(source_msg_id, target_chat_id, target_msg_id):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT INTO message_links (source_msg_id, target_chat_id, target_msg_id) VALUES (?, ?, ?)", (source_msg_id, target_chat_id, target_msg_id))
        await db.commit()

async def get_message_links(source_msg_id):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT target_chat_id, target_msg_id FROM message_links WHERE source_msg_id = ?", (source_msg_id,)) as cursor:
            return await cursor.fetchall()

async def delete_message_links(source_msg_id):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM message_links WHERE source_msg_id = ?", (source_msg_id,))
        await db.commit()

# --- Безопасное редактирование ---
async def safe_edit_text(message: types.Message, text: str, reply_markup=None):
    try:
        await message.edit_text(text, reply_markup=reply_markup, parse_mode="Markdown")
    except TelegramBadRequest:
        pass 
    except Exception:
        await message.answer(text, reply_markup=reply_markup, parse_mode="Markdown")

# --- КЛАВИАТУРЫ ---
def get_main_menu(user_id):
    kb = []
    if user_id in ADMIN_IDS: kb.append([KeyboardButton(text="📢 Создать рассылку")])
    kb.append([KeyboardButton(text="📈 ЦБ")])
    kb.append([KeyboardButton(text="Чат")])
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

def get_chat_exit_kb():
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="⬅️ Выйти из чата")]], resize_keyboard=True)

def get_type_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Отправить", callback_data="type_normal"), InlineKeyboardButton(text="📌 Отправить и Закрепить", callback_data="type_pin")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_all")]
    ])

def get_time_choice_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚡ Отправить СЕЙЧАС", callback_data="time_now")],
        [InlineKeyboardButton(text="📅 Выбрать дату и время", callback_data="time_custom")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_type")]
    ])

# --- API: FPI BANK ---
async def get_fpi_price():
    if not CMC_API_KEY: return None, "CMC Key Error (Not found in ENV)"
    try:
        url = 'https://pro-api.coinmarketcap.com/v1/cryptocurrency/quotes/latest'
        headers = {'X-CMC_PRO_API_KEY': CMC_API_KEY}
        params = {'id': CMC_FPI_ID, 'convert': 'USD'}
        
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, params=params) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    coin = data['data'].get(CMC_FPI_ID)
                    if not coin: return None, "Coin not found"
                    
                    usd = coin['quote']['USD']['price']
                    change = coin['quote']['USD']['percent_change_24h']
                    rub = usd * 100 
                    
                    return {'rub': f"{rub:,.6f}", 'usd': f"{usd:,.6f}", 'change': change}, None
                return None, f"CMC Error: {resp.status}"
    except Exception as e: return None, str(e)

# ==========================================
#              ЛОГИКА БОТА
# ==========================================

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    if message.from_user.id not in ALL_STAFF_IDS: return 
    await log_action(message.from_user, "Start")
    await message.answer(f"👋 Привет!", reply_markup=get_main_menu(message.from_user.id))

@dp.message(Command("del"))
async def cmd_del(message: types.Message):
    if message.from_user.id not in ADMIN_IDS or not message.reply_to_message: return
    links = await get_message_links(message.reply_to_message.message_id)
    if not links: return await message.reply("Сообщение не найдено в базе.")
    
    cnt = 0
    for chat, msg in links:
        try:
            await bot.delete_message(chat, msg)
            cnt += 1
        except: pass
    await delete_message_links(message.reply_to_message.message_id)
    await message.reply(f"🗑 Удалено из {cnt} групп.")

@dp.message(Command("logs"))
async def cmd_logs(message: types.Message):
    if message.from_user.id not in ALL_STAFF_IDS: return
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT timestamp, username, action FROM admin_logs ORDER BY id DESC LIMIT 200") as c: rows = await c.fetchall()
    text = "\n".join([f"{r[0]} | {r[1]} | {r[2]}" for r in rows]) if rows else "Пусто"
    await message.answer_document(BufferedInputFile(text.encode(), filename="logs.txt"), caption="Logs")

# --- ЧАТ ---
@dp.message(F.text == "Чат")
async def chat_enter(message: types.Message, state: FSMContext):
    if message.from_user.id not in ALL_STAFF_IDS: return
    await state.set_state(AdminChatState.active)
    await message.answer("💬 Чат активен", reply_markup=get_chat_exit_kb())

@dp.message(AdminChatState.active)
async def chat_msg(message: types.Message, state: FSMContext):
    if message.text == "⬅️ Выйти из чата":
        await state.clear()
        return await message.answer("Выход", reply_markup=get_main_menu(message.from_user.id))
    
    sender_id = message.from_user.id
    for uid in ALL_STAFF_IDS:
        if uid != sender_id:
            try:
                prefix = "👑" if sender_id in ADMIN_IDS else "👮"
                await bot.send_message(uid, f"💬 {prefix} **{message.from_user.first_name}:**", parse_mode="Markdown")
                await message.copy_to(uid)
            except: pass

# --- FPI ---
@dp.message(F.text == "📈 ЦБ")
async def fpi_proc(message: types.Message):
    if message.from_user.id not in ALL_STAFF_IDS: return
    wait = await message.answer("⏳...")
    data, err = await get_fpi_price()
    if err: return await safe_edit_text(wait, f"❌ Ошибка: {err}")
    
    trend = "🟢" if data['change'] > 0 else "🔴"
    text = (f"🏦 **FPI Bank**\n\n"
            f"🇺🇸 USD: **${data['usd']}**\n"
            f"🇷🇺 RUB: **{data['rub']} ₽** (≈)\n"
            f"{trend} 24ч: **{data['change']:.2f}%**")
    await safe_edit_text(wait, text)

# --- РАССЫЛКА ---
@dp.message(F.text == "📢 Создать рассылку")
async def bc_enter(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS: return
    await state.set_state(BroadcastState.waiting_for_content)
    await message.answer("📤 **Отправьте пост** (текст, фото или видео):", parse_mode="Markdown")

@dp.message(BroadcastState.waiting_for_content)
async def bc_content(message: types.Message, state: FSMContext):
    await state.update_data(msg_id=message.message_id, chat_id=message.chat.id)
    await message.answer("👀 **Превью сообщения:**", parse_mode="Markdown")
    try:
        await message.copy_to(message.chat.id)
    except:
        await message.answer("Ошибка предпросмотра.")

    await message.answer("🛠 **Что делаем?**", reply_markup=get_type_kb(), parse_mode="Markdown")
    await state.set_state(BroadcastState.choose_type)

@dp.callback_query(BroadcastState.choose_type)
async def bc_type(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    if callback.data == "cancel_all":
        await state.clear()
        await safe_edit_text(callback.message, "❌ Отменено.")
        return

    is_pin = (callback.data == "type_pin")
    await state.update_data(pin_mode=is_pin)
    
    mode_text = "С закрепом 📌" if is_pin else "Обычная 🚀"
    await safe_edit_text(callback.message, f"Режим: **{mode_text}**\nКогда отправить?", reply_markup=get_time_choice_kb())
    await state.set_state(BroadcastState.choose_time)

@dp.callback_query(BroadcastState.choose_time)
async def bc_time(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    if callback.data == "back_to_type": 
        await state.set_state(BroadcastState.choose_type)
        await safe_edit_text(callback.message, "Выберите тип:", reply_markup=get_type_kb())
        return
    
    if callback.data == "time_now":
        d = await state.get_data()
        pin_mode = d.get('pin_mode', False)
        await safe_edit_text(callback.message, "⏳ **Рассылка запущена...**", reply_markup=None)
        await distribute_message(d['chat_id'], d['msg_id'], pin_mode)
        await state.clear()
        await callback.message.answer("✅ **Готово!**", parse_mode="Markdown")
    else:
        now = datetime.now(TZ_7).strftime("%d.%m.%Y %H:%M")
        await safe_edit_text(callback.message, f"✍️ Введите дату (UTC+7):\n`{now}`", reply_markup=None)
        await state.set_state(BroadcastState.waiting_for_date)

@dp.message(BroadcastState.waiting_for_date)
async def bc_date(message: types.Message, state: FSMContext):
    try:
        dt = datetime.strptime(message.text.strip(), "%d.%m.%Y %H:%M").replace(tzinfo=TZ_7)
        if dt.timestamp() < datetime.now(TZ_7).timestamp(): 
            await message.answer("⚠️ Эта дата уже прошла!")
            return
        d = await state.get_data()
        pin_mode = d.get('pin_mode', False)
        await add_scheduled_task(d['chat_id'], d['msg_id'], dt.timestamp(), 1 if pin_mode else 0)
        await message.answer(f"✅ **Запланировано:** `{dt}`", parse_mode="Markdown")
        await state.clear()
    except: 
        await message.answer("⚠️ Формат: `ДД.ММ.ГГГГ ЧЧ:ММ`")

async def distribute_message(from_chat_id, message_id, pin_mode):
    if not TARGET_GROUPS: return
    for group_id in TARGET_GROUPS:
        try:
            sent_msg = await bot.copy_message(chat_id=group_id, from_chat_id=from_chat_id, message_id=message_id)
            await save_message_link(message_id, group_id, sent_msg.message_id)
            if pin_mode:
                try: await bot.pin_chat_message(chat_id=group_id, message_id=sent_msg.message_id)
                except: pass
            await asyncio.sleep(0.1)
        except Exception as e: logging.error(f"Err {group_id}: {e}")

# 🔥 ИСПРАВЛЕННЫЙ ОБРАБОТЧИК РЕДАКТИРОВАНИЯ 🔥
@dp.edited_message(F.chat.type == "private")
async def handle_edit(message: types.Message):
    if message.from_user.id not in ADMIN_IDS: return
    
    links = await get_message_links(message.message_id)
    if not links: return
    
    logging.info(f"[EDIT] {message.from_user.first_name} редактирует {message.message_id}")
    
    success_count = 0
    
    for target_chat_id, target_msg_id in links:
        try:
            # Если это текст
            if message.text:
                await bot.edit_message_text(
                    text=message.text,
                    chat_id=target_chat_id,
                    message_id=target_msg_id,
                    entities=message.entities, # Сохраняем форматирование
                    parse_mode=None
                )
            
            # Если это фото/видео с подписью
            elif message.caption is not None:
                await bot.edit_message_caption(
                    caption=message.caption,
                    chat_id=target_chat_id,
                    message_id=target_msg_id,
                    caption_entities=message.caption_entities, # Сохраняем форматирование
                    parse_mode=None
                )
            success_count += 1
        except TelegramBadRequest as e:
            if "message is not modified" in str(e):
                pass # Это нормально, если текст не изменился
            else:
                logging.error(f"Ошибка редактирования в {target_chat_id}: {e}")
        except Exception as e:
            logging.error(f"Критическая ошибка редактирования: {e}")

    if success_count > 0:
        await message.reply(f"✅ Обновлено в {success_count} группах!", disable_notification=True)

async def scheduler_worker():
    while True:
        try:
            now = datetime.now(TZ_7).timestamp()
            async with aiosqlite.connect(DB_NAME) as db:
                async with db.execute("SELECT id, from_chat_id, message_id, pin_mode FROM scheduled WHERE run_time <= ? AND status='pending'", (now,)) as cur:
                    tasks = await cur.fetchall()
            for t in tasks:
                await distribute_message(t[1], t[2], bool(t[3]))
                async with aiosqlite.connect(DB_NAME) as db: 
                    await db.execute("UPDATE scheduled SET status='done' WHERE id=?", (t[0],))
                    await db.commit()
                try: await bot.send_message(t[1], "⏰ Отложенный пост вышел!")
                except: pass
        except Exception as e: logging.error(f"Sched err: {e}")
        await asyncio.sleep(60)

async def main():
    await init_db()
    asyncio.create_task(scheduler_worker())
    print("✅ Бот запущен!")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: print("Стоп")
