import json
import os
import aiosqlite

DB_NAME = "delivery.db"

async def init_db():
    """Создание всех таблиц системы и первичное наполнение данными из JSON"""
    async with aiosqlite.connect(DB_NAME) as db:
        # 1. Таблица товаров
        await db.execute("""
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                price INTEGER NOT NULL,
                category TEXT NOT NULL,
                is_available INTEGER DEFAULT 1
            )
        """)
        
        # 2. Таблица заказов
        await db.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vk_user_id INTEGER NOT NULL,
                delivery_type TEXT NOT NULL,
                address TEXT,
                phone TEXT NOT NULL,
                payment_method TEXT NOT NULL,
                status TEXT DEFAULT 'Новый',
                courier_id INTEGER DEFAULT NULL
            )
        """)
        
        # 3. Таблица позиций внутри заказа
        await db.execute("""
            CREATE TABLE IF NOT EXISTS order_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id INTEGER NOT NULL,
                product_id INTEGER NOT NULL,
                quantity INTEGER NOT NULL,
                FOREIGN KEY (order_id) REFERENCES orders(id),
                FOREIGN KEY (product_id) REFERENCES products(id)
            )
        """)
        
        # 4. Таблица настроек кухни
        await db.execute("""
            CREATE TABLE IF NOT EXISTS system_settings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                is_active INTEGER DEFAULT 1
            )
        """)
        
        # 5. Таблица курьеров
        await db.execute("""
            CREATE TABLE IF NOT EXISTS couriers (
                tg_id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                is_active INTEGER DEFAULT 1
            )
        """)
        await db.commit()

        # Инициализация дефолтной настройки кухни
        async with db.execute("SELECT COUNT(*) FROM system_settings") as cursor:
            row = await cursor.fetchone()
            if row[0] == 0:
                await db.execute("INSERT INTO system_settings (id, is_active) VALUES (1, 1)")
                await db.commit()

        # АВТОМАГИЧЕСКАЯ ЗАГРУЗКА МЕНЮ ИЗ JSON
        async with db.execute("SELECT COUNT(*) FROM products") as cursor:
            row = await cursor.fetchone()
            if row[0] == 0:
                if os.path.exists("menu.json"):
                    with open("menu.json", "r", encoding="utf-8") as f:
                        menu_data = json.load(f)
                    
                    products_to_insert = []
                    for category, items in menu_data.items():
                        for item in items:
                            products_to_insert.append((item["name"], item["price"], category))
                    
                    await db.executemany(
                        "INSERT INTO products (name, price, category) VALUES (?, ?, ?)", 
                        products_to_insert
                    )
                    await db.commit()
                    print("[БАЗА ДАННЫХ]: Меню успешно импортировано из menu.json")

        # АВТОМАГИЧЕСКАЯ ЗАГРУЗКА КУРЬЕРОВ ИЗ JSON
        async with db.execute("SELECT COUNT(*) FROM couriers") as cursor:
            row = await cursor.fetchone()
            if row[0] == 0:
                if os.path.exists("couriers.json"):
                    with open("couriers.json", "r", encoding="utf-8") as f:
                        couriers_data = json.load(f)
                    
                    couriers_to_insert = []
                    for c in couriers_data:
                        couriers_to_insert.append((c["tg_id"], c["name"]))
                    
                    await db.executemany(
                        "INSERT OR IGNORE INTO couriers (tg_id, name, is_active) VALUES (?, ?, 1)", 
                        couriers_to_insert
                    )
                    await db.commit()
                    print("[БАЗА ДАННЫХ]: Стартовые курьеры успешно импортированы из couriers.json")
                else:
                    print("⚠️ [ПРЕДУПРЕЖДЕНИЕ]: Файл couriers.json не найден. База курьеров пуста.")

async def get_menu():
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT id, name, price, category FROM products WHERE is_available = 1") as cursor:
            rows = await cursor.fetchall()
            return [{"id": r[0], "name": r[1], "price": r[2], "category": r[3], "description": ""} for r in rows]

async def create_order(vk_user_id, delivery_type, address, phone, payment_method, cart):
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            "INSERT INTO orders (vk_user_id, delivery_type, address, phone, payment_method, status) VALUES (?, ?, ?, ?, ?, 'Новый')",
            (vk_user_id, delivery_type, address, phone, payment_method)
        )
        order_id = cursor.lastrowid
        for p_id, qty in cart:
            await db.execute("INSERT INTO order_items (order_id, product_id, quantity) VALUES (?, ?, ?)", (order_id, p_id, qty))
        await db.commit()
        return order_id

async def update_order_status(order_id: int, status: str):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE orders SET status = ? WHERE id = ?", (status, order_id))
        await db.commit()

async def assign_courier(order_id: int, courier_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE orders SET courier_id = ? WHERE id = ?", (courier_id, order_id))
        await db.commit()
