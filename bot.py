"""
Telegram Link/File Shortener Bot with Mini App Ad-Gate
--------------------------------------------------------
Features:
  - Admin can shorten a link:            /addlink <url>
  - Admin can store a file (just forward a file to the bot, then /addfile as reply)
  - Both generate a short deep-link:     https://t.me/<bot_username>?start=<code>
  - When a user opens that link, the bot sends a button that opens a Mini App
    (webapp/index.html) where the Monetag ad is shown.
  - After the ad, the Mini App calls Telegram.WebApp.sendData(code) which
    Telegram delivers to this bot as a "web_app_data" message.
  - The bot then looks up the code and delivers the real link or file.
  - /stats (admin only) shows total links, users, and clicks.
  - /broadcast (admin only, reply to a message) sends that message to all users.

SETUP (see README.md for full step-by-step):
  1. Create a .env file (copy .env.example) and fill in:
       BOT_TOKEN, ADMIN_ID, MONGO_URI, WEBAPP_URL
  2. pip install -r requirements.txt
  3. python bot.py

NOTE: A tiny dummy HTTP server is started in a background thread purely so
Render's health check / port scan finds an open port (needed for services
deployed as "Web Service" type, and for UptimeRobot-style keep-alive pings).
It does nothing except respond 200 OK to any request. If you deploy this as
a Render "Background Worker" instead, you can safely delete that part.
"""

import os
import random
import string
import logging
import threading
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler

from pymongo import MongoClient
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    WebAppInfo,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# CONFIG (loaded from environment variables — see .env.example)
# ---------------------------------------------------------------------------
BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_ID"])          # your numeric Telegram user ID
MONGO_URI = os.environ["MONGO_URI"]             # MongoDB Atlas connection string
WEBAPP_URL = os.environ["WEBAPP_URL"]            # https URL where webapp/index.html is hosted

# ---------------------------------------------------------------------------
# DATABASE
# ---------------------------------------------------------------------------
client = MongoClient(MONGO_URI)
db = client["shortener_bot"]
links_col = db["links"]      # {code, type: 'link'|'file', value, clicks, created_at}
users_col = db["users"]      # {user_id, first_seen}


def generate_code(length: int = 6) -> str:
    chars = string.ascii_letters + string.digits
    while True:
        code = "".join(random.choices(chars, k=length))
        if not links_col.find_one({"code": code}):
            return code


def record_user(user_id: int):
    if not users_col.find_one({"user_id": user_id}):
        users_col.insert_one({"user_id": user_id, "first_seen": datetime.utcnow()})


def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


# ---------------------------------------------------------------------------
# COMMAND HANDLERS
# ---------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    record_user(user.id)

    args = context.args
    if not args:
        await update.message.reply_text(
            "Namaste! Yeh bot aapke liye shortened links/files deliver karta hai.\n"
            "Kisi bhi shared link par click karke shuru karein."
        )
        return

    code = args[0]
    entry = links_col.find_one({"code": code})
    if not entry:
        await update.message.reply_text("Yeh link invalid ya expire ho chuka hai.")
        return

    # Open the Mini App (ad-gate) with the code passed as a start_param
    webapp_full_url = f"{WEBAPP_URL}?code={code}"
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Continue ➜", web_app=WebAppInfo(url=webapp_full_url))]]
    )
    await update.message.reply_text(
        "Aage badhne ke liye niche button dabayein:",
        reply_markup=keyboard,
    )


async def addlink(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Use: /addlink <url>")
        return

    url = context.args[0]
    code = generate_code()
    links_col.insert_one(
        {"code": code, "type": "link", "value": url, "clicks": 0, "created_at": datetime.utcnow()}
    )
    bot_username = (await context.bot.get_me()).username
    await update.message.reply_text(f"Short link ready:\nhttps://t.me/{bot_username}?start={code}")


async def addfile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not update.message.reply_to_message:
        await update.message.reply_text(
            "Pehle koi file bot ko forward/send karein, phir uss file par reply karke /addfile likhein."
        )
        return

    msg = update.message.reply_to_message
    file_id = None
    if msg.document:
        file_id = msg.document.file_id
    elif msg.video:
        file_id = msg.video.file_id
    elif msg.photo:
        file_id = msg.photo[-1].file_id
    elif msg.audio:
        file_id = msg.audio.file_id

    if not file_id:
        await update.message.reply_text("Is message mein koi supported file nahi mili.")
        return

    code = generate_code()
    links_col.insert_one(
        {"code": code, "type": "file", "value": file_id, "clicks": 0, "created_at": datetime.utcnow()}
    )
    bot_username = (await context.bot.get_me()).username
    await update.message.reply_text(f"Short link ready:\nhttps://t.me/{bot_username}?start={code}")


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    total_links = links_col.count_documents({})
    total_users = users_col.count_documents({})
    total_clicks = sum(l.get("clicks", 0) for l in links_col.find({}, {"clicks": 1}))
    await update.message.reply_text(
        f"📊 Stats\nTotal links: {total_links}\nTotal users: {total_users}\nTotal clicks: {total_clicks}"
    )


async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not update.message.reply_to_message:
        await update.message.reply_text("Jis message ko broadcast karna hai, uss par reply karke /broadcast likhein.")
        return

    src = update.message.reply_to_message
    sent, failed = 0, 0
    for u in users_col.find({}, {"user_id": 1}):
        try:
            await context.bot.copy_message(
                chat_id=u["user_id"], from_chat_id=src.chat_id, message_id=src.message_id
            )
            sent += 1
        except Exception:
            failed += 1
    await update.message.reply_text(f"Broadcast done. Sent: {sent}, Failed: {failed}")


# ---------------------------------------------------------------------------
# WEB APP DATA CALLBACK  (fired after the Mini App shows the ad and the user
# taps "Get File / Get Link" — see webapp/index.html)
# ---------------------------------------------------------------------------
async def webapp_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = update.effective_message.web_app_data.data  # this is the 'code' we sent
    entry = links_col.find_one({"code": data})
    if not entry:
        await update.message.reply_text("Link expire ho chuka hai.")
        return

    links_col.update_one({"code": data}, {"$inc": {"clicks": 1}})

    if entry["type"] == "link":
        await update.message.reply_text(f"Aapka link:\n{entry['value']}")
    elif entry["type"] == "file":
        await context.bot.send_document(
            chat_id=update.effective_chat.id, document=entry["value"]
        )


# ---------------------------------------------------------------------------
# DUMMY HTTP SERVER (Render port-binding / UptimeRobot keep-alive)
# ---------------------------------------------------------------------------
class _QuietHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Bot is running.")

    def log_message(self, format, *args):
        # Suppress default per-request logging so it doesn't spam the logs
        pass


def run_dummy_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), _QuietHandler)
    logger.info(f"Dummy HTTP server listening on port {port} (for Render/UptimeRobot).")
    server.serve_forever()


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
def main():
    # Start the dummy web server in a background thread so it doesn't block
    # the bot's own polling loop.
    threading.Thread(target=run_dummy_server, daemon=True).start()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("addlink", addlink))
    app.add_handler(CommandHandler("addfile", addfile))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("broadcast", broadcast))
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, webapp_data))

    logger.info("Bot started.")
    app.run_polling()


if __name__ == "__main__":
    main()
