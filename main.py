from aiohttp import web
import asyncio
import logging
import sqlite3
import re
import html
import aiohttp
import json

# ==============================================================================
# CONFIGURAZIONE BOT
# ==============================================================================
TELEGRAM_BOT_TOKEN = "8637062218:AAFibtCbxXLMpKqAkz_X-0BstHZji_kdKwY"
TELEGRAM_CHAT_ID = ""  # Rilevamento automatico chat attive

VINTED_DOMAIN = "it"     # Dominio Vinted
CHECK_INTERVAL = 60      # Intervallo scansione in secondi
DB_FILE = "vinted_sentinel.db"

# Stato globale
IS_MONITORING = True
USER_STATES = {}

# ==============================================================================
# LOGGING & DATABASE
# ==============================================================================
logging.basicConfig(
    format="%(asctime)s - [%(levelname)s] - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("VintedSentinel")

def init_db():
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("CREATE TABLE IF NOT EXISTS seen_items (item_id TEXT PRIMARY KEY)")
        cursor.execute("CREATE TABLE IF NOT EXISTS target_users (user_id INTEGER PRIMARY KEY, name TEXT)")
        cursor.execute("CREATE TABLE IF NOT EXISTS telegram_subscribers (chat_id INTEGER PRIMARY KEY)")
        conn.commit()

def add_subscriber(chat_id: int):
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT OR IGNORE INTO telegram_subscribers (chat_id) VALUES (?)", (chat_id,))
        conn.commit()

def get_subscribers() -> list[int]:
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT chat_id FROM telegram_subscribers")
        rows = cursor.fetchall()
        return [row[0] for row in rows] if rows else []

def get_target_users():
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT user_id, name FROM target_users")
        return [{"user_id": row[0], "name": row[1]} for row in cursor.fetchall()]

def add_target_user(user_id: int, name: str):
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO target_users (user_id, name) VALUES (?, ?)", (user_id, name))
        conn.commit()

def remove_target_user(user_id: int) -> bool:
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM target_users WHERE user_id = ?", (user_id,))
        deleted = cursor.rowcount > 0
        conn.commit()
        return deleted

def is_item_seen(item_id: str) -> bool:
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM seen_items WHERE item_id = ?", (str(item_id),))
        return cursor.fetchone() is not None

def save_seen_item(item_id: str):
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT OR IGNORE INTO seen_items (item_id) VALUES (?)", (str(item_id),))
        conn.commit()

def extract_user_id(text: str) -> int | None:
    match = re.search(r"(?:member|users)/(\d+)", text)
    if match:
        return int(match.group(1))
    numbers = re.findall(r"\b\d{6,}\b", text)
    if numbers:
        return int(numbers[0])
    return None

# ==============================================================================
# CLASSE VINTED SENTINEL
# ==============================================================================
class VintedSentinel:
    def __init__(self, domain="it"):
        self.domain = domain
        self.base_url = f"https://www.vinted.{domain}"
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
            "Referer": f"{self.base_url}/",
            "Connection": "keep-alive"
        }

    async def refresh_cookies(self, session: aiohttp.ClientSession, user_id: int = None):
        target_url = f"{self.base_url}/member/{user_id}" if user_id else self.base_url
        headers = self.headers.copy()
        headers["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8"
        try:
            async with session.get(target_url, headers=headers, timeout=10) as resp:
                if resp.status == 200:
                    logger.info("Cookie Vinted aggiornati correttamente.")
        except Exception as e:
            logger.error(f"Errore caricamento cookie: {e}")

    async def fetch_user_info(self, session: aiohttp.ClientSession, user_id: int) -> str:
        url = f"{self.base_url}/api/v2/users/{user_id}"
        try:
            async with session.get(url, headers=self.headers, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    user_data = data.get("user", {})
                    return user_data.get("login") or user_data.get("username") or f"Profilo_{user_id}"
        except Exception:
            pass
        return f"Profilo_{user_id}"

    async def fetch_item_details(self, session: aiohttp.ClientSession, item_id: str, item_url: str) -> tuple[str, list[str]]:
        """Estrae descrizione dettagliata e tutte le foto dell'articolo."""
        description = ""
        photos = []

        # 1. Tentativo API Standard
        api_url = f"{self.base_url}/api/v2/items/{item_id}"
        try:
            async with session.get(api_url, headers=self.headers, timeout=5) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    item_data = data.get("item", {})
                    description = item_data.get("description", "")
                    
                    # Estrae tutte le foto dall'API se presenti
                    api_photos = item_data.get("photos", [])
                    for p in api_photos:
                        p_url = p.get("url") or p.get("full_size_url")
                        if p_url:
                            photos.append(p_url)
        except Exception:
            pass

        # 2. Se mancano dettagli o foto, usa lo Scraper HTML
        scrape_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "it-IT,it;q=0.9",
            "Upgrade-Insecure-Requests": "1"
        }
        try:
            async with session.get(item_url, headers=scrape_headers, timeout=8) as resp:
                if resp.status == 200:
                    html_text = await resp.text()
                    
                    # Estrazione descrizione via Regex se vuota
                    if not description or str(description).strip() in ("", "None"):
                        match = re.search(r'"description"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"', html_text)
                        if match:
                            raw = match.group(1)
                            raw = raw.replace('\\n', '\n').replace('\\r', '').replace('\\"', '"').replace('\\/', '/')
                            raw = re.sub(r'<[^>]+>', '', raw)
                            clean = html.unescape(raw).strip()
                            if clean and clean.lower() not in ("null", "none"):
                                description = clean

                    # Estrazione di tutte le foto dalla pagina HTML se l'API ne ha trovate poche o nessuna
                    if not photos:
                        matches = re.findall(r'"full_size_url"\s*:\s*"([^"]+)"', html_text)
                        for m in matches:
                            fixed_url = m.replace('\\/', '/')
                            if fixed_url not in photos:
                                photos.append(fixed_url)
        except Exception as e:
            logger.error(f"Errore scraper HTML per dettagli articolo {item_id}: {e}")

        return description, photos

    async def fetch_user_items(self, session: aiohttp.ClientSession, user_id: int):
        endpoints = [
            f"{self.base_url}/api/v2/users/{user_id}/items?page=1&per_page=20&order=newest_first",
            f"{self.base_url}/api/v2/catalog/items?user_ids[]={user_id}&per_page=20&order=newest_first"
        ]

        for url in endpoints:
            try:
                async with session.get(url, headers=self.headers, timeout=10) as resp:
                    if resp.status in (401, 403):
                        await self.refresh_cookies(session, user_id)
                        continue
                    elif resp.status == 200:
                        data = await resp.json()
                        items = data.get("items", [])
                        
                        valid_items = []
                        for item in items:
                            item_owner = str(item.get("user_id") or item.get("user", {}).get("id") or "")
                            if not item_owner or item_owner == str(user_id):
                                valid_items.append(item)
                        
                        if valid_items:
                            return valid_items
                    elif resp.status == 429:
                        return []
            except Exception as e:
                logger.error(f"Errore recupero articoli utente {user_id}: {e}")
        return []

    async def send_telegram_message(self, session: aiohttp.ClientSession, chat_id: int, text: str, reply_markup: dict = None):
        telegram_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        try:
            await session.post(telegram_url, json=payload)
        except Exception as e:
            logger.error(f"Errore invio messaggio generico: {e}")

    async def send_single_notification(self, session: aiohttp.ClientSession, chat_id: int, item: dict, user_name: str, prefix_msg: str = ""):
        item_id = str(item.get("id"))
        raw_title = item.get("title") or "Senza Titolo"
        title = html.escape(str(raw_title))
        url = item.get("url") or f"{self.base_url}/items/{item_id}"

        # Recupera descrizione e tutte le foto aggiornate
        raw_desc, photos = await self.fetch_item_details(session, item_id, url)
        
        # Fallback alle foto di base dell'oggetto se lo scraper non ne ha trovate altre
        if not photos:
            base_photos = item.get("photos", [])
            for p in base_photos:
                p_url = p.get("url")
                if p_url:
                    photos.append(p_url)

        desc_str = str(raw_desc).strip() if raw_desc else ""
        if desc_str and desc_str.lower() != "none":
            clean_desc = html.escape(desc_str)
            clean_desc = re.sub(r'\n{3,}', '\n\n', clean_desc)
            if len(clean_desc) > 700:
                clean_desc = clean_desc[:700] + "..."
            desc_block = f"\n\n{clean_desc}\n\n"
        else:
            desc_block = "\n\n"

        raw_price = item.get("price")
        if isinstance(raw_price, dict):
            price = raw_price.get("amount", "N/A")
            currency = raw_price.get("currency_code", "EUR")
        else:
            price = raw_price or "N/A"
            currency = item.get("currency", "EUR")
        
        safe_user_name = html.escape(str(user_name or "Utente"))

        # Costruzione del testo finale
        header = prefix_msg if prefix_msg else ""
        caption = (
            f"{header}"
            f"👤 Profilo: {safe_user_name}\n"
            f"📌 {title}\n"
            f"💰 Prezzo: {price} {currency}"
            f"{desc_block}"
            f"🔗 <a href='{url}'>Apri su Vinted</a>"
        )

        telegram_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
        
        try:
            if photos:
                media_group = []
                unique_photos = list(dict.fromkeys(photos))[:10]
                
                for idx, p_url in enumerate(unique_photos):
                    media_item = {
                        "type": "photo",
                        "media": p_url
                    }
                    if idx == len(unique_photos) - 1:
                        media_item["caption"] = caption
                        media_item["parse_mode"] = "HTML"
                    
                    media_group.append(media_item)

                endpoint = f"{telegram_url}/sendMediaGroup"
                payload = {
                    "chat_id": chat_id,
                    "media": json.dumps(media_group)
                }
                async with session.post(endpoint, data=payload) as resp:
                    if resp.status != 200:
                        fallback_endpoint = f"{telegram_url}/sendPhoto"
                        fallback_payload = {"chat_id": chat_id, "photo": unique_photos[0], "caption": caption, "parse_mode": "HTML"}
                        async with session.post(fallback_endpoint, data=fallback_payload) as f_resp:
                            pass
            else:
                endpoint = f"{telegram_url}/sendMessage"
                payload = {"chat_id": chat_id, "text": caption, "parse_mode": "HTML"}
                async with session.post(endpoint, data=payload) as resp:
                    pass
        except Exception as e:
            logger.error(f"Errore invio notifica album foto: {e}")

    async def broadcast_notification(self, session: aiohttp.ClientSession, item: dict, user_name: str):
        subscribers = get_subscribers()
        for chat_id in subscribers:
            await self.send_single_notification(session, chat_id, item, user_name, prefix_msg="🚨 Nuovo articolo pubblicato!\n\n")

# ==============================================================================
# PROVA IMMEDIATA ALL'AGGIUNTA
# ==============================================================================
async def process_new_user_and_test(session: aiohttp.ClientSession, sentinel: VintedSentinel, chat_id: int, user_id: int):
    await sentinel.refresh_cookies(session, user_id)
    real_username = await sentinel.fetch_user_info(session, user_id)
    add_target_user(user_id, real_username)

    await sentinel.send_telegram_message(
        session, chat_id, 
        f"⏳ Profilo {real_username} (ID: {user_id}) salvato!\nRecupero gli articoli reali in corso...", 
        get_main_keyboard()
    )

    items = await sentinel.fetch_user_items(session, user_id)

    if not items:
        await sentinel.send_telegram_message(
            session, chat_id, 
            f"⚠️ Profilo {real_username} aggiunto, ma non sono stati trovati articoli pubblici.", 
            get_main_keyboard()
        )
        return

    test_items = items[:3]
    await sentinel.send_telegram_message(
        session, chat_id, 
        f"✅ Test Riuscito! Ecco gli ultimi {len(test_items)} articoli reali di {real_username}:\nDa adesso riceverai notifiche SOLO quando pubblicherà NUOVI articoli.", 
        get_main_keyboard()
    )

    for item in test_items:
        item_id = str(item.get("id"))
        save_seen_item(item_id)
        await sentinel.send_single_notification(
            session, chat_id, item, real_username, 
            prefix_msg=""
        )
        await asyncio.sleep(1.5)

    for item in items[3:]:
        save_seen_item(str(item.get("id")))

# ==============================================================================
# MENU E TASTIERA TELEGRAM
# ==============================================================================
def get_main_keyboard():
    return {
        "keyboard": [
            [{"text": "▶️ Avvia Bot"}, {"text": "⏸️ Ferma Bot"}],
            [{"text": "➕ Aggiungi"}, {"text": "📋 Lista"}, {"text": "🗑️ Rimuovi"}],
            [{"text": "ℹ️ Info / Stato"}]
        ],
        "resize_keyboard": True
    }

async def handle_telegram_updates(session: aiohttp.ClientSession, sentinel: VintedSentinel):
    global IS_MONITORING, USER_STATES
    offset = 0
    telegram_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"

    while True:
        try:
            async with session.get(f"{telegram_url}?offset={offset}&timeout=10") as resp:
                if resp.status == 200:
                    data = await resp.json()
                    for update in data.get("result", []):
                        offset = update["update_id"] + 1
                        message = update.get("message", {})
                        chat = message.get("chat", {})
                        chat_id = chat.get("id")
                        text = message.get("text", "").strip()

                        if not chat_id or not text:
                            continue

                        add_subscriber(chat_id)
                        current_state = USER_STATES.get(chat_id)

                        if text.startswith("/start") or text == "Menu":
                            USER_STATES[chat_id] = None
                            msg = "🤖 Vinted Sentinel Bot\n\nInvia un link di un utente o seleziona un'opzione:"
                            await sentinel.send_telegram_message(session, chat_id, msg, get_main_keyboard())

                        elif text.startswith("/add") or text == "➕ Aggiungi":
                            extracted = extract_user_id(text)
                            if extracted:
                                USER_STATES[chat_id] = None
                                await process_new_user_and_test(session, sentinel, chat_id, extracted)
                            else:
                                USER_STATES[chat_id] = "WAITING_ADD"
                                msg = "➕ Invia il link del profilo Vinted da monitorare.\n\nEsempio:\nhttps://www.vinted.it/member/178949847"
                                await sentinel.send_telegram_message(session, chat_id, msg, get_main_keyboard())

                        elif text.startswith("/remove") or text == "🗑️ Rimuovi":
                            extracted = extract_user_id(text)
                            if extracted:
                                if remove_target_user(extracted):
                                    USER_STATES[chat_id] = None
                                    msg = f"🗑️ Profilo {extracted} rimosso!"
                                else:
                                    msg = f"❌ Profilo {extracted} non trovato nella lista."
                                await sentinel.send_telegram_message(session, chat_id, msg, get_main_keyboard())
                            else:
                                users = get_target_users()
                                if not users:
                                    msg = "🗑️ Nessun profilo presente da rimuovere."
                                    await sentinel.send_telegram_message(session, chat_id, msg, get_main_keyboard())
                                else:
                                    USER_STATES[chat_id] = "WAITING_REMOVE"
                                    list_txt = "\n".join([f"• {u['user_id']} ({u['name']})" for u in users])
                                    msg = f"🗑️ Invia il link o ID da rimuovere:\n\n{list_txt}"
                                    await sentinel.send_telegram_message(session, chat_id, msg, get_main_keyboard())

                        elif text.startswith("/list") or text == "📋 Lista":
                            USER_STATES[chat_id] = None
                            users = get_target_users()
                            if not users:
                                msg = "📋 Nessun profilo monitorato."
                            else:
                                list_txt = "\n".join([f"• {u['name']} (ID: {u['user_id']})" for u in users])
                                msg = f"📋 Profili In Monitoraggio ({len(users)}):\n\n{list_txt}"
                            await sentinel.send_telegram_message(session, chat_id, msg, get_main_keyboard())

                        elif text in ("▶️ Avvia Bot", "/run"):
                            IS_MONITORING = True
                            USER_STATES[chat_id] = None
                            await sentinel.send_telegram_message(session, chat_id, "▶️ Monitoraggio avviato!", get_main_keyboard())

                        elif text in ("⏸️ Ferma Bot", "/stop"):
                            IS_MONITORING = False
                            USER_STATES[chat_id] = None
                            await sentinel.send_telegram_message(session, chat_id, "⏸️ Monitoraggio in pausa.", get_main_keyboard())

                        elif text in ("ℹ️ Info / Stato", "/status"):
                            USER_STATES[chat_id] = None
                            status_str = "🟢 Attivo" if IS_MONITORING else "🔴 In Pausa"
                            users = get_target_users()
                            msg = f"ℹ️ Stato Bot: {status_str}\n👥 Profili: {len(users)}\n⏱️ Intervallo: {CHECK_INTERVAL}s"
                            await sentinel.send_telegram_message(session, chat_id, msg, get_main_keyboard())

                        else:
                            extracted = extract_user_id(text)
                            if current_state == "WAITING_ADD" or (extracted and "vinted" in text.lower()):
                                if extracted:
                                    USER_STATES[chat_id] = None
                                    await process_new_user_and_test(session, sentinel, chat_id, extracted)
                                else:
                                    await sentinel.send_telegram_message(session, chat_id, "❌ Link non valido. Riprova.", get_main_keyboard())
                            elif current_state == "WAITING_REMOVE":
                                if extracted and remove_target_user(extracted):
                                    USER_STATES[chat_id] = None
                                    await sentinel.send_telegram_message(session, chat_id, f"🗑️ Profilo {extracted} rimosso!", get_main_keyboard())
                                else:
                                    await sentinel.send_telegram_message(session, chat_id, "❌ Profilo non trovato nella lista.", get_main_keyboard())
        except Exception as e:
            pass
        asyncio.sleep(2)

# ==============================================================================
# ROUTINE AUTOMATICA DI MONITORAGGIO
# ==============================================================================
async def monitoring_loop(session: aiohttp.ClientSession, sentinel: VintedSentinel):
    first_run = True
    while True:
        if IS_MONITORING:
            targets = get_target_users()
            if targets:
                for target in targets:
                    user_name = target["name"]
                    user_id = target["user_id"]
                    
                    items = await sentinel.fetch_user_items(session, user_id)
                    for item in items:
                        item_id = str(item.get("id"))
                        if not is_item_seen(item_id):
                            save_seen_item(item_id)
                            if not first_run:
                                await sentinel.broadcast_notification(session, item, user_name)
                                await asyncio.sleep(1.5)
                    await asyncio.sleep(2)
                first_run = False
        await asyncio.sleep(CHECK_INTERVAL)

# ==============================================================================
# SERVER WEB PER RENDER (Mantiene il bot attivo 24/7)
# ==============================================================================
async def handle_ping(request):
    return web.Response(text="Bot is running!")

async def web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 10000)
    await site.start()

async def main():
    init_db()
    sentinel = VintedSentinel(domain=VINTED_DOMAIN)
    async with aiohttp.ClientSession() as session:
        await sentinel.refresh_cookies(session)
        logger.info("🤖 Bot Sentinel Vinted avviato ed in ascolto!")
        await asyncio.gather(
            web_server(), # Mette in ascolto il server web per Render
            monitoring_loop(session, sentinel),
            handle_telegram_updates(session, sentinel)
        )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot arrestato dall'utente.")