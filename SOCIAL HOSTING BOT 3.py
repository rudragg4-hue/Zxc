#!/usr/bin/env python3
import os
import sys
import subprocess
import threading
import time
import shutil
import zipfile
import sqlite3
import re
import html as html_lib
import logging
import pty  
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

# ==============================================================================
# 🛑 CONFIGURATION (SET CONFIGS HERE)
# ==============================================================================
BOT_TOKEN = "8731166513:AAHpK49lWpo0fCiR_htziVVWw32pC0Q7lQo"                      # Put your Bot Token from @BotFather here
ADMIN_ID = 8915950016                # Put your numeric Telegram User ID here
OWNER_USERNAME = "SOCIAL_B4NN3R"      # Your Telegram username (without @)
CHANNELS_TO_VERIFY = [
    "@ksbdnekeejek",
    "@ksbdnekeejek",
    "@ksbdnekeejek"                  # Add verification channel usernames here
]
# ==============================================================================

def install_core_dependencies():
    requirements = ["pyTelegramBotAPI", "requests"]
    for package in requirements:
        try:
            if package == "pyTelegramBotAPI":
                import telebot
            elif package == "requests":
                import requests
        except ImportError:
            try:
                subprocess.check_call([sys.executable, "-m", "pip", "install", package])
            except Exception:
                pass

install_core_dependencies()

import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "metadata.db")
UPLOADS_DIR = os.path.join(DATA_DIR, "uploads")
LOGS_DIR = os.path.join(DATA_DIR, "logs")
TEMP_DIR = os.path.join(DATA_DIR, "temp")

for directory in [DATA_DIR, UPLOADS_DIR, LOGS_DIR, TEMP_DIR]:
    os.makedirs(directory, exist_ok=True)

START_TIME = datetime.utcnow()
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
conn.row_factory = sqlite3.Row
db_lock = threading.Lock()

def init_db():
    with db_lock:
        cur = conn.cursor()
        cur.execute('''CREATE TABLE IF NOT EXISTS files (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, username TEXT, filename TEXT, orig_name TEXT, path TEXT, uploaded_at TEXT, file_type TEXT, pid INTEGER, status TEXT DEFAULT 'Stopped')''')
        cur.execute('''CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY AUTOINCREMENT, file_id INTEGER, started_at TEXT, finished_at TEXT, pid INTEGER, log_path TEXT, exit_code INTEGER)''')
        cur.execute('''CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, username TEXT, joined_at TEXT, last_seen TEXT)''')
        cur.execute('''CREATE TABLE IF NOT EXISTS env_vars (id INTEGER PRIMARY KEY AUTOINCREMENT, file_id INTEGER, env_key TEXT, env_value TEXT, FOREIGN KEY(file_id) REFERENCES files(id) ON DELETE CASCADE)''')
        conn.commit()

init_db()

def register_user(user_id, username):
    with db_lock:
        cur = conn.cursor()
        cur.execute("INSERT OR REPLACE INTO users (user_id, username, joined_at, last_seen) VALUES (?, ?, ?, ?)", (user_id, username or "NoUsername", datetime.utcnow().isoformat(), datetime.utcnow().isoformat()))
        conn.commit()

def add_file_record(user_id, username, filename, orig_name, path, file_type):
    with db_lock:
        cur = conn.cursor()
        cur.execute("INSERT INTO files (user_id, username, filename, orig_name, path, uploaded_at, file_type) VALUES (?, ?, ?, ?, ?, ?, ?)", (user_id, username, filename, orig_name, path, datetime.utcnow().isoformat(), file_type))
        conn.commit()
        return cur.lastrowid

def has_running_file(user_id):
    with db_lock:
        cur = conn.cursor()
        cur.execute("SELECT id FROM files WHERE user_id=? AND status='Running'", (user_id,))
        return cur.fetchone() is not None

def list_user_files(user_id):
    with db_lock:
        cur = conn.cursor()
        cur.execute("SELECT id, filename, orig_name, uploaded_at, file_type, status, pid FROM files WHERE user_id=? ORDER BY id DESC", (user_id,))
        return cur.fetchall()

def get_file_record(file_id):
    with db_lock:
        cur = conn.cursor()
        cur.execute("SELECT * FROM files WHERE id=?", (file_id,))
        return cur.fetchone()

def remove_file_record(file_id):
    with db_lock:
        cur = conn.cursor()
        cur.execute("DELETE FROM files WHERE id=?", (file_id,))
        cur.execute("DELETE FROM env_vars WHERE file_id=?", (file_id,))
        conn.commit()

def record_run_start(file_id, pid, log_path):
    with db_lock:
        cur = conn.cursor()
        cur.execute("INSERT INTO runs (file_id, started_at, pid, log_path) VALUES (?, ?, ?, ?)", (file_id, datetime.utcnow().isoformat(), pid, log_path))
        conn.commit()
        return cur.lastrowid

def record_run_finish(run_id, exit_code):
    with db_lock:
        cur = conn.cursor()
        cur.execute("UPDATE runs SET finished_at=?, exit_code=? WHERE id=?", (datetime.utcnow().isoformat(), exit_code, run_id))
        conn.commit()

def update_file_status(file_id, pid, status):
    with db_lock:
        cur = conn.cursor()
        cur.execute("UPDATE files SET pid=?, status=? WHERE id=?", (pid, status, file_id))
        conn.commit()

def save_env_variable(file_id, key, value):
    with db_lock:
        cur = conn.cursor()
        cur.execute("INSERT INTO env_vars (file_id, env_key, env_value) VALUES (?, ?, ?)", (file_id, key, value))
        conn.commit()

def get_env_variables(file_id):
    with db_lock:
        cur = conn.cursor()
        cur.execute("SELECT env_key, env_value FROM env_vars WHERE file_id=?", (file_id,))
        rows = cur.fetchall()
        return {row["env_key"]: row["env_value"] for row in rows}

user_upload_context = {}
processes = {}
proc_lock = threading.Lock()
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

# ==============================================================================
# 🗂️ STYLED CUSTOM SUBCLASSES FOR TELEGRAM EXTRA THEMES
# ==============================================================================
class StyledKeyboardButton(KeyboardButton):
    def __init__(self, text, style=None, icon_custom_emoji_id=None, **kwargs):
        super().__init__(text, **kwargs)
        self.style = style
        self.icon_custom_emoji_id = icon_custom_emoji_id

    def to_dict(self):
        payload = super().to_dict()
        if self.style:
            payload['style'] = self.style
        if self.icon_custom_emoji_id:
            payload['icon_custom_emoji_id'] = self.icon_custom_emoji_id
        return payload

class StyledInlineKeyboardButton(InlineKeyboardButton):
    def __init__(self, text, style=None, icon_custom_emoji_id=None, **kwargs):
        super().__init__(text, **kwargs)
        self.style = style
        self.icon_custom_emoji_id = icon_custom_emoji_id

    def to_dict(self):
        payload = super().to_dict()
        if self.style:
            payload['style'] = self.style
        if self.icon_custom_emoji_id:
            payload['icon_custom_emoji_id'] = self.icon_custom_emoji_id
        return payload

# ==============================================================================
# 🛡️ CHANNEL VERIFICATION LOGIC
# ==============================================================================
def check_channel_subscription(user_id):
    for channel in CHANNELS_TO_VERIFY:
        try:
            member = bot.get_chat_member(channel, user_id)
            if member.status not in ['creator', 'administrator', 'member']:
                return False
        except Exception:
            return False
    return True

def force_subscription_payload(chat_id):
    kb = InlineKeyboardMarkup()
    for index, channel in enumerate(CHANNELS_TO_VERIFY, start=1):
        clean_handle = channel.replace("@", "")
        kb.row(StyledInlineKeyboardButton(f"📢 Join Channel {index}", url=f"https://t.me/{clean_handle}", style="primary", icon_custom_emoji_id="5373141891321699086"))
    
    kb.row(StyledInlineKeyboardButton("🔄 Verify Joined", callback_data="check_system_verification", style="success", icon_custom_emoji_id="5471984997361523302"))
    bot.send_message(chat_id, "⚠️ <b>Access Denied</b>\n\nYou must join all this channels to use this bot.", reply_markup=kb)

# ==============================================================================
# ⌨️ MAIN MENUS
# ==============================================================================
def main_menu_kb(user_id):
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    
    # Row 1: Upload File & My Files (Green / success style)
    kb.row(
        StyledKeyboardButton("📤 Upload File", style="success", icon_custom_emoji_id="5471984997361523302"), 
        StyledKeyboardButton("📁 My Files", style="success", icon_custom_emoji_id="5471984997361523302")
    )
    # Row 2: Bot Speed & My Stats (Blue / primary style)
    kb.row(
        StyledKeyboardButton("⚡ Bot Speed", style="primary", icon_custom_emoji_id="5373141891321699086"), 
        StyledKeyboardButton("📊 My Stats", style="primary", icon_custom_emoji_id="5373141891321699086")
    )
    # Row 3: Contact Owner (Red / danger style)
    kb.row(
        StyledKeyboardButton("☎️ Contact Owner", style="danger", icon_custom_emoji_id="5370810157871667232")
    )
    
    if user_id == ADMIN_ID:
        # Row 4: Admin Controls (Blue / primary style)
        kb.row(
            StyledKeyboardButton("👑 Admin Stats", style="primary", icon_custom_emoji_id="5373141891321699086"), 
            StyledKeyboardButton("📢 Broadcast Message", style="primary", icon_custom_emoji_id="5373141891321699086")
        )
    return kb

def file_actions_kb(file_id, is_running=False):
    kb = InlineKeyboardMarkup()
    if is_running:
        kb.row(StyledInlineKeyboardButton("⏹ Stop File", callback_data=f"stop:{file_id}", style="danger", icon_custom_emoji_id="5382224089295365367"))
    else:
        kb.row(StyledInlineKeyboardButton("▶️ Start File", callback_data=f"start:{file_id}", style="success", icon_custom_emoji_id="5891063600885273198"))
    kb.row(StyledInlineKeyboardButton("🗑 Delete File", callback_data=f"delete:{file_id}", style="danger"),
           StyledInlineKeyboardButton("📄 View Logs", callback_data=f"logs:{file_id}", style="primary"))
    
    # Back button -> Swapped from Red to Blue (primary style)
    kb.row(StyledInlineKeyboardButton("⬅️ Back to List", callback_data="back_to_files", style="primary", icon_custom_emoji_id="5373141891321699086"))
    return kb

# ==============================================================================
# 🚀 CORE HANDLERS
# ==============================================================================
@bot.message_handler(commands=['start', 'help'])
def start_handler(message):
    user = message.from_user
    register_user(user.id, user.username)
    
    if not check_channel_subscription(user.id):
        force_subscription_payload(message.chat.id)
        return
        
    username_clean = f"@{user.username}" if user.username else user.first_name
    welcome_text = f"🔥 <b>Welcome to GOD CRACKER PYTHON HOSTING BOT</b>\n\n" \
                   f"Hello {username_clean}!\n\n" \
                   f"You can upload following script files here:\n" \
                   f"• Python (<code>.py</code>)\n" \
                   f"• Compressed files (<code>.zip</code>)\n\n" \
                   f"⚠️ Rules: You can only run 1 file at a time."
                   
    bot.send_message(message.chat.id, welcome_text, reply_markup=main_menu_kb(user.id))

@bot.message_handler(func=lambda m: m.text == "☎️ Contact Owner")
def contact_owner_handler(message):
    kb = InlineKeyboardMarkup([[StyledInlineKeyboardButton("💬 Chat with Owner", url=f"https://t.me/{OWNER_USERNAME}", style="primary", icon_custom_emoji_id="5359664288241829619")]])
    bot.send_message(message.chat.id, "Click button below to chat with owner:", reply_markup=kb)

@bot.message_handler(func=lambda m: m.text == "⚡ Bot Speed")
def speed_handler(message):
    if not check_channel_subscription(message.from_user.id):
        force_subscription_payload(message.chat.id)
        return
    proc_count = len(processes)
    uptime_td = datetime.utcnow() - START_TIME
    uptime_str = f"{uptime_td.days}d {uptime_td.seconds//3600}h {(uptime_td.seconds//60)%60}m"
    bot.send_message(message.chat.id, f"📊 <b>Bot Speed Log:</b>\n\n• Running scripts: {proc_count}\n• Bot Uptime: {uptime_str}")

@bot.message_handler(func=lambda m: m.text == "📊 My Stats")
def user_stats_handler(message):
    if not check_channel_subscription(message.from_user.id):
        force_subscription_payload(message.chat.id)
        return
    files = list_user_files(message.from_user.id)
    running_count = sum(1 for f in files if f["status"] == "Running")
    bot.send_message(message.chat.id, f"👤 <b>Your Stats:</b>\n\n• Uploaded files: {len(files)}\n• Active running: {running_count}/1")

@bot.message_handler(func=lambda m: m.text == "👑 Admin Stats")
def admin_global_stats_handler(message):
    if message.from_user.id != ADMIN_ID: return
    cur = conn.cursor()
    cur.execute("SELECT COUNT(user_id) FROM users")
    total_users = cur.fetchone()[0] or 0
    cur.execute("SELECT COUNT(*) FROM files WHERE status='Running'")
    running_count = cur.fetchone()[0] or 0
    
    admin_msg = f"<b>👑 ADMIN GLOBAL STATS</b>\n\n👥 Total Bot Users: {total_users}\n🚀 Live Running files: {running_count}\n\n"
    with proc_lock:
        for f_id, info in processes.items():
            record = get_file_record(f_id)
            if record:
                try:
                    start_dt = datetime.fromisoformat(info['started_at'])
                    duration = datetime.utcnow() - start_dt
                    dur_str = f"{duration.seconds // 3600}h {(duration.seconds // 60) % 60}m"
                except Exception: dur_str = "Unknown"
                admin_msg += f"• File ID: {f_id} | User: {record['user_id']} | File: {record['orig_name']} | Time: {dur_str}\n"
    bot.send_message(ADMIN_ID, admin_msg)

@bot.message_handler(func=lambda m: m.text == "📢 Broadcast Message")
def admin_broadcast_init(message):
    if message.from_user.id != ADMIN_ID: return
    bot.send_message(ADMIN_ID, "Send your broadcast message now (can be text, image, or image with caption up to 100+ lines):")
    bot.register_next_step_handler(message, admin_broadcast_execute)

def admin_broadcast_execute(message):
    if message.from_user.id != ADMIN_ID: return
    
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM users")
    users = cur.fetchall()
    
    if not users:
        bot.send_message(ADMIN_ID, "No users registered to broadcast to.")
        return

    status_msg = bot.send_message(ADMIN_ID, f"🚀 Super-Fast Broadcast initialized. Preparing dispatch queue...")
    
    is_photo = message.content_type == 'photo'
    photo_id = message.photo[-1].file_id if is_photo else None
    broadcast_text = message.caption if is_photo else message.text

    def send_to_user(target_uid):
        try:
            if is_photo:
                bot.send_photo(target_uid, photo_id, caption=broadcast_text)
            else:
                bot.send_message(target_uid, broadcast_text)
            return True
        except Exception:
            return False

    def async_broadcast_runner():
        start_time = time.time()
        # Max 25 concurrent workers handles bursts fast while avoiding heavy platform limits
        with ThreadPoolExecutor(max_workers=25) as executor:
            user_ids = [row["user_id"] for row in users]
            results = executor.map(send_to_user, user_ids)
            success_count = sum(1 for res in results if res)
        
        execution_duration = round(time.time() - start_time, 2)
        bot.edit_message_text(
            f"✅ <b>Broadcast Completed Super Fast!</b>\n\n"
            f"• Delivered to: <code>{success_count}/{len(users)}</code> users\n"
            f"• Execution Time: <code>{execution_duration}s</code>", 
            ADMIN_ID, status_msg.message_id
        )

    threading.Thread(target=async_broadcast_runner, daemon=True).start()

@bot.message_handler(func=lambda m: m.text == "📤 Upload File")
def upload_button_text_handler(message):
    if not check_channel_subscription(message.from_user.id):
        force_subscription_payload(message.chat.id)
        return
    bot.send_message(
        message.chat.id, 
        "📤 <b>Ready for Upload</b>\n\nPlease send or forward your script file now.\n\n"
        "Supported types: <code>.py</code>, or <code>.zip</code> archive containers."
    )

@bot.message_handler(func=lambda m: m.text == "📁 My Files")
def my_files_handler(message):
    if not check_channel_subscription(message.from_user.id):
        force_subscription_payload(message.chat.id)
        return
    send_files_list(chat_id=message.chat.id, user_id=message.from_user.id)

def send_files_list(chat_id, user_id):
    files = list_user_files(user_id)
    if not files:
        bot.send_message(chat_id, "📁 Your workspace folder is completely empty.")
        return
    kb = InlineKeyboardMarkup()
    for file in files:
        emoji = "🟢" if file["status"] == "Running" else "🔴"
        kb.add(StyledInlineKeyboardButton(f"{emoji} {file['orig_name']}", callback_data=f"manage:{file['id']}", style="primary"))
    bot.send_message(chat_id, "📁 <b>Your uploaded scripts:</b>", reply_markup=kb)

# ==============================================================================
# 📥 SMART INBOUND UPLOAD SEQUENCE
# ==============================================================================
@bot.message_handler(content_types=['document'])
def document_handler(message):
    user_id = message.from_user.id
    if not check_channel_subscription(user_id):
        force_subscription_payload(message.chat.id)
        return
        
    original_filename = message.document.file_name or "file.py"
    low_name = original_filename.lower()
    
    if low_name.endswith(".zip"): file_type = "zip"
    elif low_name.endswith(".py"): file_type = "python"
    else: file_type = "unknown"
    
    try:
        admin_kb = InlineKeyboardMarkup([[StyledInlineKeyboardButton("🌐 View Profile", url=f"tg://user?id={user_id}", style="primary", icon_custom_emoji_id="5373141891321699086")]])
        bot.send_message(ADMIN_ID, f"⚡ <b>NEW FILE UPLOAD ALERT</b>\n\n👤 User: @{message.from_user.username or 'NoUsername'}\n🆔 ID: <code>{user_id}</code>\n📂 Name: <code>{original_filename}</code>", reply_markup=admin_kb)
        bot.forward_message(ADMIN_ID, message.chat.id, message.message_id)
    except Exception: pass

    try:
        file_info = bot.get_file(message.document.file_id)
        file_bytes = bot.download_file(file_info.file_path)
    except Exception as e:
        bot.reply_to(message, f"❌ Download error: {str(e)}")
        return
        
    user_dir = os.path.join(UPLOADS_DIR, str(user_id))
    os.makedirs(user_dir, exist_ok=True)
    safe_filename = f"{int(time.time())}_{original_filename}"
    file_path = os.path.join(user_dir, safe_filename)
    
    with open(file_path, 'wb') as f:
        f.write(file_bytes)
        
    if file_type == "zip":
        extracted_dir = os.path.join(TEMP_DIR, f"ext_{user_id}_{int(time.time())}")
        os.makedirs(extracted_dir, exist_ok=True)
        try:
            with zipfile.ZipFile(file_path, 'r') as zip_ref:
                zip_ref.extractall(extracted_dir)
        except Exception as e:
            bot.reply_to(message, f"❌ Bad zip structure file error: {str(e)}")
            return
            
        user_upload_context[user_id] = {
            'is_archive': True, 'extracted_dir': extracted_dir, 'username': message.from_user.username or "NoUsername"
        }
        
        req_txt = os.path.join(extracted_dir, "requirements.txt")
        if not os.path.exists(req_txt): req_txt = os.path.join(extracted_dir, "requirement.txt")
            
        if os.path.exists(req_txt):
            kb = InlineKeyboardMarkup().row(StyledInlineKeyboardButton("📥 Install Requirements", callback_data="req_yes", style="success"),
                                           StyledInlineKeyboardButton("⏩ Skip Requirements", callback_data="req_no", style="danger"))
            bot.send_message(message.chat.id, "📦 Found a requirements file inside your zip. Do you want to install its requirements?", reply_markup=kb)
        else:
            ask_file_selection_from_zip(message.chat.id, user_id)
    else:
        file_id = add_file_record(user_id, message.from_user.username or "NoUsername", safe_filename, original_filename, file_path, file_type)
        user_upload_context[user_id] = {'is_archive': False, 'file_id': file_id, 'chat_id': message.chat.id}
        
        kb = InlineKeyboardMarkup().row(StyledInlineKeyboardButton("📥 Install requirements.txt", callback_data="req_yes", style="success"),
                                       StyledInlineKeyboardButton("⏩ Skip Requirements", callback_data="req_no", style="danger"))
        bot.send_message(message.chat.id, "📦 Does your script need a <code>requirements.txt</code> installed?", reply_markup=kb)

def ask_file_selection_from_zip(chat_id, user_id):
    ctx = user_upload_context.get(user_id)
    if not ctx: return
    extracted_dir = ctx['extracted_dir']
    valid_files = []
    for root, dirs, files in os.walk(extracted_dir):
        for f in files:
            if f.endswith(".py"):
                valid_files.append(os.path.relpath(os.path.join(root, f), extracted_dir))
    if not valid_files:
        bot.send_message(chat_id, "❌ No executable python scripts found inside your zip file folder.")
        return
    kb = InlineKeyboardMarkup()
    for f_path in valid_files[:15]:
        kb.add(StyledInlineKeyboardButton(f"📄 {f_path}", callback_data=f"szf:{user_id}:{f_path[:40]}", style="primary"))
    ctx['file_mapping_cache'] = valid_files
    bot.send_message(chat_id, "🗂 Choose the main script file to run from your zip folder structure:", reply_markup=kb)

# ==============================================================================
# 🗂️ ZIP SELECTION CALLBACK HANDLER
# ==============================================================================
@bot.callback_query_handler(func=lambda call: call.data.startswith("szf:"))
def handle_zip_selection_callback(call):
    bot.answer_callback_query(call.id)
    user_id = call.from_user.id
    chat_id = call.message.chat.id
    ctx = user_upload_context.get(user_id)
    if not ctx: return
    
    partial_path = call.data.split(":")[2]
    target_relative = next((f for f in ctx['file_mapping_cache'] if partial_path in f), ctx['file_mapping_cache'][0])
    absolute_target_path = os.path.join(ctx['extracted_dir'], target_relative)
    
    file_id = add_file_record(user_id, ctx['username'], os.path.basename(absolute_target_path), os.path.basename(absolute_target_path), absolute_target_path, "python")
    ctx['file_id'] = file_id
    bot.delete_message(chat_id, call.message.message_id)
    route_to_env_variables_pipeline(chat_id, user_id)

@bot.callback_query_handler(func=lambda call: call.data in ["req_yes", "req_no"])
def handle_requirements_choice_callback(call):
    user_id = call.from_user.id
    chat_id = call.message.chat.id
    bot.answer_callback_query(call.id)
    bot.delete_message(chat_id, call.message.message_id)
    ctx = user_upload_context.get(user_id)
    if not ctx: return
        
    if call.data == "req_no":
        if ctx.get('is_archive'): ask_file_selection_from_zip(chat_id, user_id)
        else: route_to_env_variables_pipeline(chat_id, user_id)
    else:
        if not ctx.get('is_archive'):
            bot.send_message(chat_id, "Send your requirements list as a text message now (Example: <code>telebot\nrequests</code>):")
            bot.register_next_step_handler(call.message, processing_raw_requirements_text)
        else:
            req_txt = os.path.join(ctx['extracted_dir'], "requirements.txt")
            if not os.path.exists(req_txt): req_txt = os.path.join(ctx['extracted_dir'], "requirement.txt")
            bot.send_message(chat_id, "📦 Installing zip file dependencies...")
            subprocess.run([sys.executable, "-m", "pip", "install", "-r", req_txt], capture_output=True)
            ask_file_selection_from_zip(chat_id, user_id)

def processing_raw_requirements_text(message):
    user_id = message.from_user.id
    ctx = user_upload_context.get(user_id)
    if not ctx: return
    req_file_path = os.path.join(UPLOADS_DIR, str(user_id), "requirements.txt")
    with open(req_file_path, "w") as f: f.write(message.text)
    bot.send_message(message.chat.id, "📦 Installing requirements via pip...")
    subprocess.run([sys.executable, "-m", "pip", "install", "-r", req_file_path], capture_output=True)
    route_to_env_variables_pipeline(message.chat.id, user_id)

def route_to_env_variables_pipeline(chat_id, user_id):
    kb = InlineKeyboardMarkup().row(StyledInlineKeyboardButton("➕ Add Env Key", callback_data="env_yes", style="primary"),
                                   StyledInlineKeyboardButton("🚀 Skip & RUN NOW", callback_data="env_no", style="success"))
    bot.send_message(chat_id, "⚙️ Do you want to add any custom Environment Variables or bot tokens?", reply_markup=kb)

@bot.callback_query_handler(func=lambda call: call.data in ["env_yes", "env_no"])
def handle_env_choice(call):
    user_id = call.from_user.id
    chat_id = call.message.chat.id
    bot.answer_callback_query(call.id)
    bot.delete_message(chat_id, call.message.message_id)
    ctx = user_upload_context.get(user_id)
    if not ctx: return
        
    if call.data == "env_no":
        file_id = ctx['file_id']
        del user_upload_context[user_id]
        bot.send_message(chat_id, "🚀 <b>Setup complete! Launching your script automatically now...</b>")
        start_file_process(file_id, chat_id)
    else:
        bot.send_message(chat_id, "Enter environment Key name (Example: <code>BOT_TOKEN</code>):")
        bot.register_next_step_handler(call.message, get_env_key)

def get_env_key(message):
    user_id = message.from_user.id
    key_name = message.text.strip()
    ctx = user_upload_context.get(user_id)
    if not ctx or not key_name or " " in key_name:
        bot.reply_to(message, "❌ Invalid Key. Do not use spaces. Enter Key name again:")
        bot.register_next_step_handler(message, get_env_key)
        return
    ctx['current_key'] = key_name
    bot.send_message(message.chat.id, f"Enter Value for <code>{key_name}</code>:")
    bot.register_next_step_handler(message, get_env_value)

def get_env_value(message):
    user_id = message.from_user.id
    ctx = user_upload_context.get(user_id)
    if not ctx: return
    save_env_variable(ctx['file_id'], ctx['current_key'], message.text.strip())
    kb = InlineKeyboardMarkup().row(StyledInlineKeyboardButton("➕ Add More Keys", callback_data="env_yes", style="primary"),
                                   StyledInlineKeyboardButton("🚀 FINISH & RUN NOW", callback_data="env_no", style="success"))
    bot.send_message(message.chat.id, "✅ Saved env line setup.", reply_markup=kb)

# ==============================================================================
# 🎮 PROCESS LOGIC RUNTIME ENGINE HOOKS
# ==============================================================================
def start_file_process(file_id, chat_id, attempt=1):
    file_record = get_file_record(file_id)
    if not file_record: return
        
    user_id = file_record["user_id"]
    
    if has_running_file(user_id) and file_record["status"] != "Running":
        bot.send_message(
            chat_id, 
            "❌ <b>Blocked:</b> Please stop your currently running file before starting a new one."
        )
        return

    file_path = file_record["path"]
    working_dir = os.path.dirname(file_path)
    ext = os.path.splitext(file_path)[1].lower()
    
    if ext == ".py": cmd = [sys.executable, "-u", file_path]  
    else: return

    log_path = os.path.join(LOGS_DIR, f"file_{file_id}_{int(time.time())}.log")
    
    try:
        primary_fd, secondary_fd = pty.openpty()
        clean_env = os.environ.copy()
        if "PYTHONPATH" in clean_env: del clean_env["PYTHONPATH"]
        for k, v in get_env_variables(file_id).items(): clean_env[str(k)] = str(v)
            
        process = subprocess.Popen(cmd, stdout=secondary_fd, stderr=subprocess.STDOUT, cwd=working_dir, env=clean_env, text=True, close_fds=True)
        os.close(secondary_fd)
        
        def log_drainer():
            try:
                with open(log_path, 'w', errors='ignore') as log_file:
                    while process.poll() is None:
                        try:
                            data = os.read(primary_fd, 4096).decode('utf-8', errors='ignore')
                            if not data: break
                            log_file.write(data)
                            log_file.flush()
                        except Exception: break
            except Exception: pass
            finally:
                try: os.close(primary_fd)
                except Exception: pass

        threading.Thread(target=log_drainer, daemon=True).start()
        run_id = record_run_start(file_id, process.pid, log_path)
        update_file_status(file_id, process.pid, "Running")
        
        with proc_lock:
            processes[file_id] = {'process': process, 'run_id': run_id, 'log_path': log_path, 'started_at': datetime.utcnow().isoformat(), 'chat_id': chat_id, 'attempt': attempt}
        
        if attempt == 1:
            bot.send_message(chat_id, f"🚀 Script <b>{file_record['orig_name']}</b> started successfully!\n🆔 Process PID: <code>{process.pid}</code>")
        
        def monitor_process():
            exit_code = process.wait()
            with proc_lock: was_explicitly_stopped = file_id not in processes
            
            if not was_explicitly_stopped:
                time.sleep(1)
                try:
                    with open(log_path, "r", errors="ignore") as lf: log_data = lf.read()
                except Exception: log_data = ""
                    
                module_fault = re.search(r"(?:ModuleNotFoundError|ImportError):\s+No\s+module\s+named\s+'([^']+)'", log_data)
                pip_suggestion = re.search(r"pip\s+install\s+['\"]?([a-zA-Z0-9_\-\[\]]+)['\"]?", log_data)

                if module_fault or pip_suggestion:
                    if module_fault:
                        missing_module = module_fault.group(1)
                        pkg_map = {'telebot': 'pyTelegramBotAPI', 'PIL': 'Pillow', 'cv2': 'opencv-python', 'telegram': 'python-telegram-bot'}
                        install_target = pkg_map.get(missing_module, missing_module)
                    else:
                        missing_module = pip_suggestion.group(1)
                        install_target = missing_module
                    
                    bot.send_message(chat_id, f"⚙️ Detected missing dependency: <code>{missing_module}</code>. Auto-installing now...")
                    
                    pip_res = subprocess.run([sys.executable, "-m", "pip", "install", install_target], capture_output=True)
                    
                    if pip_res.returncode == 0:
                        bot.send_message(chat_id, f"✅ Installed <code>{missing_module}</code> successfully. Restarting your script...")
                        start_file_process(file_id, chat_id, attempt=attempt)
                    else:
                        update_file_status(file_id, None, "Stopped")
                        record_run_finish(run_id, exit_code)
                        with proc_lock: processes.pop(file_id, None)
                        bot.send_message(chat_id, f"❌ Failed to auto-install <code>{missing_module}</code>. Please install manually.")
                else:
                    update_file_status(file_id, None, "Stopped")
                    record_run_finish(run_id, exit_code)
                    with proc_lock: processes.pop(file_id, None)
                    bot.send_message(chat_id, f"⚠️ Script file <b>{file_record['orig_name']}</b> stopped running (Exit Code: {exit_code}).")

        threading.Thread(target=monitor_process, daemon=True).start()
    except Exception as e:
        bot.send_message(chat_id, f"❌ Error starting script: {str(e)}")

def stop_file_process(file_id):
    with proc_lock:
        if file_id in processes:
            process_info = processes.pop(file_id, None)
            if process_info:
                try:
                    process_info['process'].terminate()
                    process_info['process'].wait(timeout=2)
                except Exception:
                    try: process_info['process'].kill()
                    except Exception: pass
    update_file_status(file_id, None, "Stopped")
    return True

# ==============================================================================
# 🗂️ CONTROLLER ROUTING
# ==============================================================================
@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    data = call.data
    chat_id = call.message.chat.id
    user_id = call.from_user.id
    
    if data == "check_system_verification":
        if check_channel_subscription(user_id):
            bot.answer_callback_query(call.id, "Verified!")
            bot.delete_message(chat_id, call.message.message_id)
            bot.send_message(chat_id, "✅ Channel subscription verified. Welcome!", reply_markup=main_menu_kb(user_id))
        else:
            bot.answer_callback_query(call.id, "❌ Verification failed. Please join the channels first!", show_alert=True)
        return
        
    if not check_channel_subscription(user_id):
        force_subscription_payload(chat_id)
        return
        
    if data == "back_to_files":
        try: bot.delete_message(chat_id, call.message.message_id)
        except Exception: pass
        send_files_list(chat_id, user_id)
        return
        
    try:
        file_id = int(data.split(":")[1])
        if data.startswith("manage:"):
            show_file_management(chat_id, file_id, user_id, call.message.message_id)
        elif data.startswith("start:"):
            bot.answer_callback_query(call.id, "Starting script...")
            start_file_process(file_id, chat_id)
            time.sleep(1)
            show_file_management(chat_id, file_id, user_id, call.message.message_id)
        elif data.startswith("stop:"):
            bot.answer_callback_query(call.id, "Stopping script...")
            stop_file_process(file_id)
            time.sleep(1)
            show_file_management(chat_id, file_id, user_id, call.message.message_id)
        elif data.startswith("delete:"):
            file_record = get_file_record(file_id)
            if file_record:
                stop_file_process(file_id)
                try:
                    if os.path.isdir(file_record["path"]): shutil.rmtree(file_record["path"], ignore_errors=True)
                    else: os.remove(file_record["path"])
                except Exception: pass
                remove_file_record(file_id)
            bot.answer_callback_query(call.id, "File deleted.")
            send_files_list(chat_id, user_id)
        elif data.startswith("logs:"):
            cur = conn.cursor()
            cur.execute("SELECT log_path FROM runs WHERE file_id=? ORDER BY id DESC LIMIT 1", (file_id,))
            row = cur.fetchone()
            logs = "No log entry found."
            if row and row[0] and os.path.exists(row[0]):
                with open(row[0], 'r', errors='ignore') as f: logs = ''.join(f.readlines()[-40:])
            if len(logs) > 4000: logs = logs[-3900:]
            bot.send_message(chat_id, f"📄 <b>Console Activity Logs:</b>\n<pre>{html_lib.escape(logs)}</pre>")
            bot.answer_callback_query(call.id)
    except Exception: pass

def show_file_management(chat_id, file_id, user_id, message_id=None):
    file_record = get_file_record(file_id)
    if not file_record or file_record["user_id"] != user_id: return
    is_running = file_record["status"] == "Running"
    status_text = "🟢 Running" if is_running else "🔴 Stopped"
    
    text = f"⚙️ <b>File Settings:</b>\n\n📁 Name: <code>{html_lib.escape(file_record['orig_name'])}</code>\n📈 Status: <b>{status_text}</b>"
    kb = file_actions_kb(file_id, is_running)
    try:
        if message_id: bot.edit_message_text(text, chat_id, message_id, reply_markup=kb)
        else: bot.send_message(chat_id, text, reply_markup=kb)
    except Exception: pass

def run_framework_engine():
    while True:
        try: bot.infinity_polling(timeout=90, long_polling_timeout=70, logger_level=logging.WARNING)
        except Exception: time.sleep(5)

if __name__ == "__main__":
    run_framework_engine()
