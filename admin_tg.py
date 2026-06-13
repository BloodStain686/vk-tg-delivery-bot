import asyncio
import os
import time
import urllib.parse
import aiohttp
import aiosqlite
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from database import init_db, update_order_status, assign_courier, DB_NAME

load_dotenv()
TG_TOKEN = os.getenv("TG_TOKEN")
TG_CHAT_ID = os.getenv("TG_CHAT_ID")
TG_COURIER_CHAT_ID = os.getenv("TG_COURIER_CHAT_ID")
CITY = os.getenv("CITY", "Печора")

bot = Bot(token=TG_TOKEN)
dp = Dispatcher()

# Глобальный трекер для удаления старого дашборда заказов
last_dashboard_id = {}

def get_admin_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🎛 Управление кухней"), KeyboardButton(text="📋 Стоп-лист товаров")],
            [KeyboardButton(text="📋 Список активных заказов")]
        ],
        resize_keyboard=True
    )

@dp.message(F.text == "/start")
async def cmd_start(message: Message):
    await message.answer("🚀 Боевая панель White Label запущена и готова к работе.", reply_markup=get_admin_keyboard())

async def init_couriers():
    pass

# --- БЛОК 1: ИНТЕРАКТИВНЫЙ ДАШБОРД АКТИВНЫХ ЗАКАЗОВ (БЕЗ ФЛУДА) ---

async def render_dashboard_kb():
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT id, status, address FROM orders WHERE status IN ('Новый', 'Готовится', 'В пути') ORDER BY id ASC") as cursor:
            rows = await cursor.fetchall()
    if not rows:
        return None
    
    buttons = []
    for o_id, status, addr in rows:
        emoji = "🆕" if status == "Новый" else "👨‍🍳" if status == "Готовится" else "🛵"
        clean_addr = addr.replace(" [ВЫЙДЕТ САМ]", "").split(",")[0][:15]
        buttons.append([InlineKeyboardButton(text=f"{emoji} Чек #{o_id} [{clean_addr}]", callback_data=f"manage_{o_id}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

@dp.message(F.text == "📋 Список активных заказов")
async def show_active_orders(message: Message):
    chat_id = message.chat.id
    try: await message.delete()  # Удаляем текстовую команду оператора
    except: pass
    
    if chat_id in last_dashboard_id:
        try: await bot.delete_message(chat_id, last_dashboard_id[chat_id])  # Трём старую панель
        except: pass

    kb = await render_dashboard_kb()
    if not kb:
        msg = await message.answer("📋 <b>Активных чеков нет. Кухня отдыхает!</b>", parse_mode="HTML")
        last_dashboard_id[chat_id] = msg.message_id
        return

    msg = await message.answer("🗂 <b>КУХОННЫЙ ТРЕКЕР ЗАКАЗОВ:</b>\nНажмите на нужный чек для управления:", parse_mode="HTML", reply_markup=kb)
    last_dashboard_id[chat_id] = msg.message_id

@dp.callback_query(F.data == "back_to_dash")
async def handle_back_to_dash(call):
    kb = await render_dashboard_kb()
    if not kb:
        await call.message.edit_text("📋 <b>Активных чеков нет. Кухня отдыхает!</b>", parse_mode="HTML")
        return
    await call.message.edit_text("🗂 <b>КУХОННЫЙ ТРЕКЕР ЗАКАЗОВ:</b>\nНажмите на нужный чек для управления:", parse_mode="HTML", reply_markup=kb)

@dp.callback_query(F.data.startswith("manage_"))
async def handle_manage_order(call):
    order_id = int(call.data.split("_")[1])
    query = (
        "SELECT o.status, o.address, o.phone, o.payment_method, GROUP_CONCAT(p.name || ' x' || oi.quantity, ', ') "
        "FROM orders o "
        "LEFT JOIN order_items oi ON o.id = oi.order_id "
        "LEFT JOIN products p ON oi.product_id = p.id "
        "WHERE o.id = ? GROUP BY o.id"
    )
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(query, (order_id,)) as cursor:
            row = await cursor.fetchone()

    if not row:
        await call.answer("Заказ не найден!", show_alert=True)
        return

    status, addr, phone, pay_method, items = row
    msg_text = (
        f"📄 <b>УПРАВЛЕНИЕ ЗАКАЗОМ #{order_id}</b>\n"
        f"────────────────────────\n"
        f"📊 <b>Статус:</b> {status}\n"
        f"📞 <b>Телефон:</b> <code>{phone}</code>\n"
        f"📍 <b>Адрес:</b> {addr}\n"
        f"💳 <b>Оплата:</b> {pay_method}\n"
        f"🍣 <b>Состав:</b> {items}\n"
        f"────────────────────────"
    )
    buttons = []
    if status == "Новый":
        buttons.append([InlineKeyboardButton(text="👨‍🍳 Принять в работу", callback_data=f"orderaccept_{order_id}")])
    elif status == "Готовится":
        buttons.append([InlineKeyboardButton(text="🟢 Заказ готов (Выбрать курьера)", callback_data=f"orderready_{order_id}")])
    buttons.append([InlineKeyboardButton(text="🔙 Назад к списку", callback_data="back_to_dash")])
    
    await call.message.edit_text(msg_text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

# --- БЛОК 2: РАБОТА КУХНИ И СТОП-ЛИСТ ТОВАРОВ ---

@dp.message(F.text == "🎛 Управление кухней")
async def manage_kitchen(message: Message):
    try: await message.delete()
    except: pass
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT is_active FROM system_settings WHERE id = 1") as cursor:
            row = await cursor.fetchone()
    status = row[0] if row else 1
    status_str = "🟢 РАБОТАЕТ (Заказы принимаются)" if status == 1 else "🔴 ЗАКРЫТА (Стоп-заказ)"
    btn_text = "🛑 Включить СТОП-ЗАКАЗ" if status == 1 else "🟢 Открыть кухню"
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=btn_text, callback_data=f"tikitchen_{status}")]])
    await message.answer(f"🎛 <b>УПРАВЛЕНИЕ КУХНЕЙ:</b>\n\nТекущий статус: <b>{status_str}</b>", parse_mode="HTML", reply_markup=kb)

@dp.callback_query(F.data.startswith("tikitchen_"))
async def handle_toggle_kitchen(call):
    curr = int(call.data.split("_")[1])
    new_status = 0 if curr == 1 else 1
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE system_settings SET is_active = ? WHERE id = 1", (new_status,))
        await db.commit()
    status_str = "🟢 РАБОТАЕТ (Заказы принимаются)" if new_status == 1 else "🔴 ЗАКРЫТА (Стоп-заказ)"
    btn_text = "🛑 Включить СТОП-ЗАКАЗ" if new_status == 1 else "🟢 Открыть кухню"
    await call.message.edit_text(f"🎛 <b>УПРАВЛЕНИЕ КУХНЕЙ:</b>\n\nТекущий статус: <b>{status_str}</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=btn_text, callback_data=f"tikitchen_{new_status}")]]))

@dp.message(F.text == "📋 Стоп-лист товаров")
async def manage_stop_list(message: Message):
    try: await message.delete()
    except: pass
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT id, name, is_available, category FROM products") as cursor:
            rows = await cursor.fetchall()
    if not rows:
        await message.answer("📋 <b>База продуктов пуста.</b>")
        return
    buttons = []
    for p_id, name, is_avail, cat in rows:
        st = "🟢" if is_avail == 1 else "🔴 СТОП"
        buttons.append([InlineKeyboardButton(text=f"{name} ({cat}) — {st}", callback_data=f"tostop_{p_id}")])
    await message.answer("📋 <b>СТОП-ЛИСТ ТОВАРОВ:</b>\nНажмите на кнопку товара, чтобы переключить его доступность в ВК:", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@dp.callback_query(F.data.startswith("tostop_"))
async def handle_toggle_stop(call):
    p_id = int(call.data.split("_")[1])
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT is_available FROM products WHERE id = ?", (p_id,)) as cursor:
            row = await cursor.fetchone()
        new_st = 0 if row[0] == 1 else 1
        await db.execute("UPDATE products SET is_available = ? WHERE id = ?", (new_st, p_id))
        await db.commit()
        async with db.execute("SELECT id, name, is_available, category FROM products") as cursor:
            rows = await cursor.fetchall()
    buttons = []
    for pr_id, name, is_avail, cat in rows:
        st = "🟢" if is_avail == 1 else "🔴 СТОП"
        buttons.append([InlineKeyboardButton(text=f"{name} ({cat}) — {st}", callback_data=f"tostop_{pr_id}")])
    await call.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

# --- БЛОК 3: ОБРАБОТЧИКИ ЦЕПОЧКИ ЗАКАЗА ---

@dp.callback_query(F.data.startswith("orderaccept_"))
async def handle_order_accept(call):
    vk_token = os.getenv("VK_TOKEN", "").strip()
    order_id = int(call.data.split("_")[1])
    await update_order_status(order_id, "Готовится")
    
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT vk_user_id FROM orders WHERE id = ?", (order_id,)) as cursor:
            row = await cursor.fetchone()
            
    if row and vk_token:
        url = "https://api.vk.com/method/messages.send"
        params = {
            "user_id": row[0],
            "message": "👨‍🍳 Отличные новости! Ваш заказ успешно принят кухней и уже готовится.",
            "random_id": int(time.time() * 1000), "access_token": vk_token, "v": "5.131"
        }
        try:
            async with aiohttp.ClientSession() as session: await session.get(url, params=params)
        except: pass
            
    next_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🟢 Заказ готов (Выбрать курьера)", callback_data=f"orderready_{order_id}")]])
    await call.message.edit_text(f"🍳 Заказ #{order_id} передан поварам.\n📊 Статус: <b>Готовится</b>", parse_mode="HTML", reply_markup=next_kb)

@dp.callback_query(F.data.startswith("orderready_"))
async def handle_order_ready(call):
    order_id = int(call.data.split("_")[1])
    await update_order_status(order_id, "Готов")
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT tg_id, name FROM couriers WHERE is_active = 1") as cursor:
            couriers = await cursor.fetchall()
            
    if not couriers:
        await call.answer(text="🚨 Ошибка: В couriers.json нет курьеров!", show_alert=True)
        return
    buttons = []
    for c_id, c_name in couriers:
        buttons.append([InlineKeyboardButton(text=f"🚗 {c_name}", callback_data=f"assign_{order_id}_{c_id}")])
    await call.message.edit_text(f"📦 Заказ #{order_id} <b>ГОТОВ К ВЫДАЧЕ</b>.\nНажмите на курьера:", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@dp.callback_query(F.data.startswith("assign_"))
async def handle_courier_assignment(call):
    vk_token = os.getenv("VK_TOKEN", "").strip()
    parts = call.data.split("_")
    order_id, courier_id = int(parts[1]), int(parts[2])
    
    await update_order_status(order_id, "В пути")
    await assign_courier(order_id, courier_id)
    
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT address, phone, vk_user_id FROM orders WHERE id = ?", (order_id,)) as cursor:
            order_row = await cursor.fetchone()
        async with db.execute("SELECT name FROM couriers WHERE tg_id = ?", (courier_id,)) as cursor:
            c_row = await cursor.fetchone()
            
    courier_name = c_row[0] if c_row else "Курьер"
    if order_row:
        addr, phone, vk_uid = order_row
        is_outside = "[ВЫЙДЕТ САМ]" in addr
        clean_addr = addr.replace("[ВЫЙДЕТ САМ]", "").strip()
        geolink = f"https://yandex.ru/maps/?text={urllib.parse.quote(f'{CITY}, {clean_addr}')}"
        
        courier_msg = f"🛵 <b>ЗАКАЗ #{order_id}</b>\n📍 <b>Адрес:</b> {addr}\n📞 <b>Тел:</b> {phone}\n🗺 <a href='{geolink}'>Открыть Карты</a>"
        c_btns = [[InlineKeyboardButton(text="🔔 Выходите!", callback_data=f"callout_{order_id}_{courier_id}")]] if is_outside else []
        c_btns.append([InlineKeyboardButton(text="✅ Доставлен!", callback_data=f"delv_{order_id}_{courier_id}")])
        
        try: await bot.send_message(TG_COURIER_CHAT_ID, courier_msg, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=c_btns))
        except: pass
            
        if vk_token:
            url = "https://api.vk.com/method/messages.send"
            params = {
                "user_id": vk_uid, "message": f"🛵 Курьер {courier_name} взял ваш заказ и выехал к вам! Ожидайте.",
                "random_id": int(time.time() * 1000), "access_token": vk_token, "v": "5.131"
            }
            try:
                async with aiohttp.ClientSession() as session: await session.get(url, params=params)
            except: pass
            
    await call.message.edit_text(f"🛵 Заказ #{order_id} передан курьеру <b>{courier_name}</b>.", parse_mode="HTML")

@dp.callback_query(F.data.startswith("callout_"))
async def handle_courier_callout(call):
    vk_token = os.getenv("VK_TOKEN", "").strip()
    parts = call.data.split("_")
    order_id, courier_id = int(parts[1]), int(parts[2])
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT vk_user_id FROM orders WHERE id = ?", (order_id,)) as cursor:
            row = await cursor.fetchone()
    if row and vk_token:
        url = "https://api.vk.com/method/messages.send"
        params = {
            "user_id": row[0], "message": "🔔 Курьер подъехал к вашему дому! Пожалуйста, выходите забирать ваш заказ.",
            "random_id": int(time.time() * 1000), "access_token": vk_token, "v": "5.131"
        }
        try:
            async with aiohttp.ClientSession() as session: await session.get(url, params=params)
        except: pass
    await call.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Доставлен!", callback_data=f"delv_{order_id}_{courier_id}")]]))

@dp.callback_query(F.data.startswith("delv_"))
async def handle_courier_delivered(call):
    vk_token = os.getenv("VK_TOKEN", "").strip()
    order_id = int(call.data.split("_")[1])
    await update_order_status(order_id, "Доставлен")
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT vk_user_id FROM orders WHERE id = ?", (order_id,)) as cursor:
            row = await cursor.fetchone()
    if row and vk_token:
        url = "https://api.vk.com/method/messages.send"
        params = {
            "user_id": row[0], "message": "🎉 Ваш заказ успешно доставлен! Приятного аппетита! ❤️",
            "random_id": int(time.time() * 1000), "access_token": vk_token, "v": "5.131"
        }
        try:
            async with aiohttp.ClientSession() as session: await session.get(url, params=params)
        except: pass
    await call.message.edit_text(f"✅ Заказ #{order_id} успешно доставлен клиенту. Работа завершена.")

async def main():
    print("🚀 [НОВАЯ ПАНЕЛЬ АДМИНА В3] Встала на боевое дежурство.")
    await init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: print("Бот остановлен.")
