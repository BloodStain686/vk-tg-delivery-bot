import asyncio
import aiohttp
import aiosqlite
import json
import time
import os
from dotenv import load_dotenv
from aiogram import Bot
from database import get_menu, create_order, DB_NAME

load_dotenv()                               

VK_TOKEN = os.getenv("VK_TOKEN").strip()    
VK_GROUP_ID = int(os.getenv("VK_GROUP_ID")) 
TG_TOKEN = os.getenv("TG_TOKEN")            
TG_CHAT_ID = os.getenv("TG_CHAT_ID")
OPERATOR_PHONE = os.getenv("OPERATOR_PHONE")  

bot = Bot(token=TG_TOKEN)

async def ensure_session_table():               
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "CREATE TABLE IF NOT EXISTS user_sessions ("
            "vk_user_id INTEGER PRIMARY KEY, "
            "session_data TEXT NOT NULL"
            ")"
        )
        await db.commit()

async def load_user_session(user_id):
    await ensure_session_table()                
    async with aiosqlite.connect(DB_NAME) as db:                                                
        async with db.execute("SELECT session_data FROM user_sessions WHERE vk_user_id = ?", (user_id,)) as cursor:                             
            row = await cursor.fetchone()
            if row: 
                return json.loads(row[0])
    return {'state': 'CATEGORY', 'cart': {}, 'current_cat': None}                       

async def save_user_session(user_id, session_data):                                         
    await ensure_session_table()
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT OR REPLACE INTO user_sessions (vk_user_id, session_data) VALUES (?, ?)",
            (user_id, json.dumps(session_data, ensure_ascii=False))
        )                 
        await db.commit()
                                            
async def delete_user_session(user_id):
    await ensure_session_table()                
    async with aiosqlite.connect(DB_NAME) as db:                                                
        await db.execute("DELETE FROM user_sessions WHERE vk_user_id = ?", (user_id,))          
        await db.commit()                                                               

async def send_vk_clean_msg(http_session, user_id, text, keyboard_json=None):               
    url = "https://api.vk.com/method/messages.send"                                         
    params = {
        "user_id": user_id, "message": text, "random_id": int(time.time() * 1000),              
        "access_token": VK_TOKEN, "v": "5.131"
    }                                           
    if keyboard_json: 
        params["keyboard"] = keyboard_json
    try:                                            
        async with http_session.get(url, params=params) as resp:                                    
            res = await resp.json()
            if "error" in res: 
                print(f"🛑 [ОШИБКА VK API]: {res['error']['error_msg']}")    
    except Exception as e: 
        print(f"🛑 [ОШИБКА СЕТИ]: {e}")
                                            
def make_vk_keyboard(buttons_matrix, is_inline=True):                                       
    keyboard = {"one_time": False, "inline": is_inline, "buttons": []}                      
    for row in buttons_matrix:                      
        row_buttons = []                            
        for label, payload in row:
            row_buttons.append({
                "action": {
                    "type": "text", 
                    "payload": json.dumps(payload, ensure_ascii=False), 
                    "label": label
                }, 
                "color": "primary"
            })
        keyboard["buttons"].append(row_buttons)
    return json.dumps(keyboard, ensure_ascii=False)

def get_vk_static_keyboard():
    return json.dumps({
        "one_time": False, "inline": False, "buttons": [[
            {"action": {"type": "text", "label": "🍕 Показать меню", "payload": "{\"act\":\"menu_root\"}"}, "color": "primary"},
            {"action": {"type": "text", "label": "📊 Статус заказа", "payload": "{\"act\":\"status_check\"}"}, "color": "primary"}          
        ]]
    }, ensure_ascii=False)                  

async def get_user_info(http_session, user_id):
    url = "https://api.vk.com/method/users.get"
    params = {"user_ids": user_id, "access_token": VK_TOKEN, "v": "5.131"}
    try:                                            
        async with http_session.get(url, params=params) as resp:                                    
            res = await resp.json()
            return f"{res['response'][0].get('first_name', 'Имя')} {res['response'][0].get('last_name', 'Фамилия')}"
    except: 
        return "Клиент ВК"
                                            
async def process_vk_flow(http_session, user_id, text, payload_data):
        # Проверяем, не включен ли на кухне Стоп-заказ
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT is_active FROM system_settings WHERE id = 1") as settings_cursor:
            settings_row = await settings_cursor.fetchone()
    kitchen_active = settings_row[0] if settings_row else 1

    if not kitchen_active and text.lower() not in ["📊 статус заказа", "статус"]:
        await send_vk_clean_msg(http_session, user_id, "🛑 К сожалению, наша кухня сейчас временно закрыта или принимает заказы только по телефону. Приходите позже! ❤️", get_vk_static_keyboard())
        return

    all_products = await get_menu()
    session = await load_user_session(user_id)                                              
    state = session['state']                                                                
    print(f"📥 [ВХОДЯЩЕЕ]: ID={user_id} | Стейт={state}")                                                                               
    
    if text.lower() in ["привет", "меню", "начать", "старт", "🍕 показать меню", "показать меню"] or (payload_data and payload_data.get("act") == "menu_root"):                         
        session['state'] = 'CATEGORY'
        session['current_cat'] = None               
        current_cart = session['cart']
        cart_msg = ""                       

        categories = sorted(list(set([p['category'] for p in all_products])))
        
        menu_buttons = []
        for i in range(0, len(categories), 2):
            row = []                                    
            for cat in categories[i:i+2]:
                icon = "🍕" if "пицца" in cat.lower() else "🍣" if "ролл" in cat.lower() else "🥤" if "напит" in cat.lower() else "📦"
                row.append((f"{icon} {cat}", {"act": "set_cat", "cat": cat}))
            menu_buttons.append(row)                
        
        if current_cart and sum(current_cart.values()) > 0:                                         
            prod_prices = {p['id']: p['price'] for p in all_products}
            total = sum(prod_prices.get(int(p_id), 0) * qty for p_id, qty in current_cart.items())
            cart_msg = f"\n\n🛒 В вашей корзине товаров на сумму: {total}₽"
            menu_buttons.append([                           
                ("🛒 Моя корзина", {"act": "cart_view"}),
                ("❌ Сбросить корзину", {"act": "clear_cart"})
            ])
                                                    
        kb = make_vk_keyboard(menu_buttons, is_inline=True)
        await send_vk_clean_msg(http_session, user_id, f"😋 Что хотите заказать? Выберите категорию меню ниже:{cart_msg}", kb)
        await save_user_session(user_id, session)
        return

    if text == "❌ Сбросить корзину" or (payload_data and payload_data.get("act") == "clear_cart"):
        await delete_user_session(user_id)
        await send_vk_clean_msg(http_session, user_id, "🗑 Корзина полностью очищена! Нажмите «🍕 Показать меню», чтобы собрать новый заказ.", get_vk_static_keyboard())
        return

    if payload_data and payload_data.get("act") == "cart_view":
        session['state'] = 'CART_REVIEW'            
        state = 'CART_REVIEW'

    if text in ["🚨 Статус / Отмена", "📊 Статус заказа", "статус"] or (payload_data and payload_data.get("act") == "status_check"):        
        query = (
            "SELECT o.id, o.status, GROUP_CONCAT(p.name || ' x' || oi.quantity, ', ') "
            "FROM orders o "
            "JOIN order_items oi ON o.id = oi.order_id "
            "JOIN products p ON oi.product_id = p.id "
            "WHERE o.vk_user_id = ? AND o.status IN ('Новый', 'Готовится', 'В пути') "
            "GROUP BY o.id"
        )
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute(query, (user_id,)) as cursor:                                         
                row = await cursor.fetchone()                                                                                               
        if not row:
            await send_vk_clean_msg(http_session, user_id, "⏳ У вас нет активных заказов в работе в текущий момент.")
            return                                                                              
        order_id, status, cart_data = row
        
        # ДИНАМИЧЕСКИЙ РАСЧЕТ ОЧЕРЕДИ КУХНИ ДЛЯ КЛИЕНТА
        queue_text = ""
        if status in ['Новый', 'Готовится']:
            async with aiosqlite.connect(DB_NAME) as db:
                async with db.execute("SELECT COUNT(*) FROM orders WHERE status IN ('Новый', 'Готовится') AND id <= ?", (order_id,)) as c_cursor:
                    q_row = await c_cursor.fetchone()
                    queue_num = q_row[0] if q_row else 1
            queue_text = f" в очереди кухни №{queue_num}"

        msg = (
            f"⏳ Ваш заказ{queue_text} находится в работе!\n\n"
            f"📊 Статус: {status}\n"
            f"🍣 Состав: {cart_data}\n\n"
            f"☎️ Телефон оператора: {OPERATOR_PHONE}\n\n"
            f"⚠️ Полная отмена чека доступна по инлайн-кнопке ниже:"
        )
        kb = make_vk_keyboard([[("❌ Отменить этот заказ", {"act": "cancel_order", "id": order_id})]], is_inline=True)
        await send_vk_clean_msg(http_session, user_id, msg, kb)                                 
        return
                                                
    if payload_data and payload_data.get("act") == "cancel_order":
        order_id = payload_data["id"]               
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute("SELECT status FROM orders WHERE id = ?", (order_id,)) as cursor:
                row = await cursor.fetchone()                                                       
        if not row or row[0] not in ['Новый', 'Готовится']:                                         
            await send_vk_clean_msg(http_session, user_id, f"🛑 Отмена невозможна. Чек уже у курьера. Наберите оператора: {OPERATOR_PHONE}")                                                
            return
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("UPDATE orders SET status = 'Отменен' WHERE id = ?", (order_id,))
            await db.commit()                                                                   
        await send_vk_clean_msg(http_session, user_id, "🛑 Заказ успешно отменен.", get_vk_static_keyboard())                  
        
        tg_alert = (
            f"🚨 <b>ОТМЕНА ЗАКАЗА КЛИЕНТОМ!</b>\n"
            f"────────────────────────\n"
            f"🛑 Заказ <b>#{order_id}</b> отменен пользователем ВК.\n"
            f"⚠️ <b>ПОВАРАМ: ОСТАНОВИТЕ ПРИГОТОВЛЕНИЕ!</b>"
        )
        await bot.send_message(TG_CHAT_ID, tg_alert, parse_mode="HTML")                                               
        return
                                                
    if state == 'CART_REVIEW':                      
        if payload_data and payload_data.get("act") == "cart_plus":                                 
            p_id = str(payload_data["id"])              
            session['cart'][p_id] = session['cart'].get(p_id, 0) + 1                            
        elif payload_data and payload_data.get("act") == "cart_minus":
            p_id = str(payload_data["id"])              
            if p_id in session['cart']:                     
                session['cart'][p_id] -= 1
                if session['cart'][p_id] <= 0: 
                    del session['cart'][p_id]                        
        elif payload_data and payload_data.get("act") == "start_checkout":                          
            if not session['cart'] or sum(session['cart'].values()) == 0:
                session['state'] = 'CATEGORY'                                                           
                await send_vk_clean_msg(http_session, user_id, "⚠️ Корзина пуста. Возвращаю в меню.")                                                
                await save_user_session(user_id, session)                                               
                return                                  
            session['state'] = 'DELIVERY_TYPE'                                                      
            kb = make_vk_keyboard([[("🚗 Курьер", {"type": "delivery"}), ("🏪 Самовывоз", {"type": "pickup"})]], is_inline=True)                
            await send_vk_clean_msg(http_session, user_id, "🛵 Выберите способ получения заказа:", kb)                                          
            await save_user_session(user_id, session)
            return                                                                              
        
        cart = session['cart']
        if not cart or sum(cart.values()) == 0:                                                     
            session['state'] = 'CATEGORY'
            await send_vk_clean_msg(http_session, user_id, "🛒 Ваша корзина опустела. Нажмите «🍕 Показать меню» для выбора.", get_vk_static_keyboard())                                    
            await save_user_session(user_id, session)
            return                                                                              
        
        prod_dict = {p['id']: (p['name'], p['price']) for p in all_products}                    
        msg_text = "🛒 Редактирование вашей корзины:\n\n"
        buttons = []                                
        total = 0                                   
        for p_id, qty in list(cart.items()):
            if int(p_id) in prod_dict:                      
                name, price = prod_dict[int(p_id)]
                item_total = price * qty                    
                total += item_total                         
                msg_text += f"▪️ {name} — {qty} шт. x {price}₽ ({item_total}₽)\n"                        
                short_name = name[:12]                      
                buttons.append([
                    (f"➖ {short_name}", {"act": "cart_minus", "id": p_id}),                                
                    (f"➕ {short_name}", {"act": "cart_plus", "id": p_id})                              
                ])                          
        msg_text += f"\n💰 Итого сумма: {total}₽\n\nИзмените объём кнопками на панели:"         
        buttons.append([("🚀 Оформить заказ", {"act": "start_checkout"}), ("🔙 Назад в меню", {"act": "menu_root"})])               
        kb = make_vk_keyboard(buttons, is_inline=False)                                         
        await send_vk_clean_msg(http_session, user_id, msg_text, kb)                            
        await save_user_session(user_id, session)
        return                                                                              

    if state == 'CATEGORY':
        if payload_data and payload_data.get("act") == "set_cat":                                   
            session['state'] = 'ITEMS'
            session['current_cat'] = payload_data["cat"]                                            
            state = 'ITEMS'
        else: 
            return                                                                        
            
    if state == 'ITEMS':
        current_cat = session['current_cat']        
        if payload_data and payload_data.get("act") == "add":
            p_id = str(payload_data["id"])              
            session['cart'][p_id] = session['cart'].get(p_id, 0) + 1
                                                    
        cat_products = [p for p in all_products if p['category'] == current_cat]
        cart = session['cart']                      
        msg_text = f"📂 Категория: {current_cat}\n───────────────────\n\n"
        buttons = []                                
        current_row = []
                                                    
        for index, item in enumerate(cat_products, start=1):
            p_id, name, price, desc = item['id'], item['name'], item['price'], item.get('description', '')
            qty = cart.get(str(p_id), 0)                
            msg_text += f"{index}. {name} — {price}₽\n"
            if desc: 
                msg_text += f"📋 {desc}\n"                                                     
            if qty > 0: 
                msg_text += f"✅ В корзине: {qty} шт.\n"                                    
            msg_text += "───────────────────\n"
                                                        
            btn_label = f"➕ {name}" if qty == 0 else f"✅ {name} ({qty} шт)"
            current_row.append((btn_label, {"act": "add", "id": p_id}))                 
            if len(current_row) == 2:                       
                buttons.append(current_row)                 
                current_row = []
                                                    
        if current_row: 
            buttons.append(current_row)

        msg_text += "\nНажимайте кнопки на нижней панели для сборки:"
        nav_row = [("🔙 К категориям", {"act": "menu_root"})]
        if sum(cart.values()) > 0:                      
            nav_row.append(("🛒 Моя корзина", {"act": "cart_view"}))                            
        buttons.append(nav_row)
                                                    
        kb = make_vk_keyboard(buttons, is_inline=False)                                         
        await send_vk_clean_msg(http_session, user_id, msg_text, kb)
        await save_user_session(user_id, session)                                               
        return
                                                
    if state == 'DELIVERY_TYPE':
        if payload_data and "type" in payload_data:
            session['delivery_type'] = payload_data["type"]
            if payload_data["type"] == 'delivery':
                session['state'] = 'ADDRESS'                
                await send_vk_clean_msg(http_session, user_id, "📍 Введите адрес доставки целиком (Улица, дом, квартира):")
            else:                                           
                session['address'] = 'Самовывоз'                                                        
                session['state'] = 'PERSONS'
                kb = make_vk_keyboard([[("1", {"v": "1"}), ("2", {"v": "2"}), ("3", {"v": "3"}), ("4", {"v": "4"})]], is_inline=True)
                await send_vk_clean_msg(http_session, user_id, "👥 Укажите количество персон (выберите кнопку):", kb)                           
            await save_user_session(user_id, session)
        return                              

    if state == 'ADDRESS':                        
        if text:
            session['address'] = text
            session['state'] = 'MEET_TYPE'
            kb = make_vk_keyboard([[("🚪 До двери", {"v": "door"}), ("🏃 Выйду сам", {"v": "outside"})]], is_inline=True)                       
            await send_vk_clean_msg(http_session, user_id, "🏃 Как передать вам заказ?", kb)
            await save_user_session(user_id, session)
        return                              

    if state == 'MEET_TYPE':                      
        if payload_data and "v" in payload_data:
            if payload_data["v"] == "outside":                                                          
                session['address'] += " [ВЫЙДЕТ САМ]"                                               
            session['state'] = 'PERSONS'
            kb = make_vk_keyboard([[("1", {"v": "1"}), ("2", {"v": "2"}), ("3", {"v": "3"}), ("4", {"v": "4"})]], is_inline=True)
            await send_vk_clean_msg(http_session, user_id, "👥 Укажите количество персон (выберите кнопку):", kb)                               
            await save_user_session(user_id, session)
        return
                                                
    if state == 'PERSONS':
        val = payload_data["v"] if payload_data and "v" in payload_data else (text if text.isdigit() else None)                             
        if val:
            session['persons'] = int(val)
            session['state'] = 'TIME'                   
            kb = make_vk_keyboard([[("🕒 Как можно скорее", {"v": "asap"})]], is_inline=True)
            await send_vk_clean_msg(http_session, user_id, "⏰ Когда доставить заказ? Нажмите кнопку или укажите время текстом:", kb)                                                       
            await save_user_session(user_id, session)                                           
        return
                                                
    if state == 'TIME':
        val = "Как можно скорее" if payload_data and payload_data.get("v") == "asap" else text                                              
        if val:
            session['time_spec'] = val
            session['state'] = 'PHONE'                  
            await send_vk_clean_msg(http_session, user_id, "📞 Введите номер телефона для связи:")
            await save_user_session(user_id, session)
        return                              

    if state == 'PHONE':                          
        if text and len(text) >= 10:
            session['phone'] = text
            session['state'] = 'PAYMENT'
            if session['delivery_type'] == 'delivery':
                kb = make_vk_keyboard([[("💵 Наличными", {"pay": "Наличными курьеру"}), ("💳 По карте курьеру", {"pay": "По карте курьеру"})]], is_inline=True)
                await send_vk_clean_msg(http_session, user_id, "💳 Выберите способ оплаты:", kb)
            else:
                kb = make_vk_keyboard([[("💵 При получении", {"pay": "На кассе (Наличные/Карта)"})]], is_inline=True)
                await send_vk_clean_msg(http_session, user_id, "💳 Подтвердите оплату при получении:", kb)
            await save_user_session(user_id, session)
        return

    if state == 'PAYMENT':
        if payload_data and "pay" in payload_data:
            session['payment_method'] = payload_data["pay"]
            session['state'] = 'CONFIRM'
            
            prod_dict = {p['id']: (p['name'], p['price']) for p in all_products}
            invoice = "📋 ПРОВЕРКА ВАШЕГО ЗАКАЗА:\n\n"
            total = 0
            for p_id, qty in session['cart'].items():
                if int(p_id) in prod_dict:
                    name, price = prod_dict[int(p_id)]
                    invoice += f"▪️ {name} x{qty} — {price * qty}₽\n"
                    total += price * qty
            
            invoice += f"\n💰 Итого к оплате: {total}₽\n"
            invoice += f"📍 Адрес: {session['address']}\n"
            invoice += f"📞 Телефон: {session['phone']}\n"
            invoice += f"💳 Способ оплаты: {session['payment_method']}\n\n"
            invoice += "Все верно? Отправляем заказ на кухню?"
            
            kb = make_vk_keyboard([[("✅ Да, отправить заказ", {"act": "submit_order"}), ("❌ Отмена", {"act": "clear_cart"})]], is_inline=True)
            await send_vk_clean_msg(http_session, user_id, invoice, kb)
            await save_user_session(user_id, session)
        return

    if state == 'CONFIRM':
        if payload_data and payload_data.get("act") == "submit_order":
            cart_tuples = [(int(p_id), qty) for p_id, qty in session['cart'].items()]
            order_id = await create_order(
                vk_user_id=user_id,
                delivery_type=session['delivery_type'],
                address=session['address'],
                phone=session['phone'],
                payment_method=session['payment_method'],
                cart=cart_tuples
            )
            
            user_name = await get_user_info(http_session, user_id)
            prod_dict = {p['id']: (p['name'], p['price']) for p in all_products}
            items_text = ""
            total = 0
            for p_id, qty in session['cart'].items():
                if int(p_id) in prod_dict:
                    name, price = prod_dict[int(p_id)]
                    items_text += f"▪️ {name} x{qty} — {price * qty}₽\n"
                    total += price * qty
            
            tg_msg = (
                f"🆕 <b>НОВЫЙ ЗАКАЗ #{order_id} (из ВК)</b>\n"
                f"👤 <b>Клиент:</b> {user_name} (ID: {user_id})\n"
                f"📞 <b>Телефон:</b> <code>{session['phone']}</code>\n"
                f"📍 <b>Адрес:</b> {session['address']}\n"
                f"💳 <b>Оплата:</b> {session['payment_method']}\n"
                f"────────────────────────\n"
                f"🍣 <b>Состав заказа:</b>\n{items_text}"
                f"────────────────────────\n"
                f"💰 <b>ИТОГО: {total}₽</b>"
            )
            
            from aiogram.types import InlineKeyboardMarkup as TGInlineKB, InlineKeyboardButton as TGInlineBtn
            tg_kb = TGInlineKB(inline_keyboard=[[TGInlineBtn(text=f"👨‍🍳 Принять заказ #{order_id}", callback_data=f"orderaccept_{order_id}")]])
            
            try: 
                await bot.send_message(TG_CHAT_ID, tg_msg, parse_mode="HTML", reply_markup=tg_kb)
            except Exception as e: 
                print(f"🛑 [ОШИБКА ОТПРАВКИ В ТГ]: {e}")
                
            # РАСЧЕТ ОЧЕРЕДИ ПЕРЕД ОЧИСТКОЙ СЕССИИ КЛИЕНТА
            async with aiosqlite.connect(DB_NAME) as db:
                async with db.execute("SELECT COUNT(*) FROM orders WHERE status IN ('Новый', 'Готовится') AND id <= ?", (order_id,)) as c_cursor:
                    q_row = await c_cursor.fetchone()
                    queue_num = q_row[0] if q_row else 1
                    
            await delete_user_session(user_id)
            await send_vk_clean_msg(http_session, user_id, f"🎉 Заказ успешно принят! Вы встали в очередь кухни под №{queue_num}. Вы можете отслеживать статус по кнопке меню.", get_vk_static_keyboard())
        return

async def main():
    print("🚀 [VK BOT]: Асинхронный движок запущен. Слушаю сеть...")
    from database import init_db
    await init_db()
    
    async with aiohttp.ClientSession() as http_session:
        url = "https://api.vk.com/method/groups.getLongPollServer"
        params = {"group_id": VK_GROUP_ID, "access_token": VK_TOKEN, "v": "5.131"}
        
        async with http_session.get(url, params=params) as resp:
            res = await resp.json()
            if "error" in res:
                print(f"🛑 Ошибка LongPoll: {res['error']['error_msg']}")
                return
            server = res["response"]["server"]
            key = res["response"]["key"]
            ts = res["response"]["ts"]
            
        while True:
            try:
                lp_url = f"{server}?act=a_check&key={key}&ts={ts}&wait=25"
                async with http_session.get(lp_url) as resp:
                    lp_res = await resp.json()
                    if "failed" in lp_res:
                        async with http_session.get(url, params=params) as r_resp:
                            r_res = await r_resp.json()
                            key = r_res["response"]["key"]
                            ts = r_res["response"]["ts"]
                        continue
                        
                    ts = lp_res["ts"]
                    for update in lp_res.get("updates", []):
                        if update["type"] == "message_new":
                            msg = update["object"]["message"]
                            user_id = msg["from_id"]
                            text = msg["text"]
                            
                            payload_data = None
                            if "payload" in msg:
                                try: 
                                    payload_data = json.loads(msg["payload"])
                                except: 
                                    pass
                                    
                            asyncio.create_task(process_vk_flow(http_session, user_id, text, payload_data))
            except Exception as e:
                print(f"🛑 [ОШИБКА LONGPOLL]: {e}")
                await asyncio.sleep(5)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Бот остановлен.")
