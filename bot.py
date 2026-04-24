import os
import sys
import asyncio
import traceback
import subprocess
import signal
import json
import time
import shutil
from pathlib import Path
from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiohttp import web

# ╔══════════════════════════════════════════╗
# ║     🔥 ADVANCED TERMINAL BOT v2.0 🔥     ║
# ║         By Ayuprime | HuggingFace        ║
# ╚══════════════════════════════════════════╝

API_ID    = int(os.environ.get("API_ID", "33675350"))
API_HASH  = os.environ.get("API_HASH", "2f97c845b067a750c9f36fec497acf97")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8704517873:AAG89ly6CKrXTVeOj3OKNqHYq4mvZyHFBsM")
OWNER_ID  = int(os.environ.get("OWNER_ID", "8493596199"))

# ──── Directory Setup ────────────────────────────────────────────────────────
SCRIPTS_DIR  = Path("./scripts")   # Uploaded bot scripts go here
LOGS_DIR     = Path("./logs")      # Per-process logs
VENVS_DIR    = Path("./venvs")     # Optional per-bot venvs (future)
for d in [SCRIPTS_DIR, LOGS_DIR, VENVS_DIR]:
    d.mkdir(exist_ok=True)

# ──── Process Registry ───────────────────────────────────────────────────────
# { "tag": { "process": asyncio.subprocess, "cmd": str, "started": float, "log": str } }
running_processes: dict = {}

app = Client(
    "AdvTerminalBot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    in_memory=True,  # HuggingFace pe .session file write nahi hoti, RAM use karo
)

# ════════════════════════════════════════════════════════════════════════════
# 🌐 HEALTH CHECK WEB SERVER  (HuggingFace requires port 7860)
# ════════════════════════════════════════════════════════════════════════════
async def health_check(request):
    procs = {tag: {"cmd": v["cmd"], "uptime_sec": int(time.time() - v["started"])}
             for tag, v in running_processes.items()}
    return web.json_response({
        "status": "online",
        "bot": "AdvancedTerminalBot v2.0",
        "owner": OWNER_ID,
        "running_scripts": procs,
        "uptime_sec": int(time.time() - BOT_START_TIME),
    })

async def metrics(request):
    result = await _run_shell("df -h / && free -h && uptime")
    return web.Response(text=result, content_type="text/plain")

async def health_check(request):
    procs = {tag: {"cmd": v["cmd"], "uptime_sec": int(time.time() - v["started"])}
             for tag, v in running_processes.items()}
    return web.json_response({
        "status": "online",
        "bot": "AdvancedTerminalBot v2.0",
        "owner": OWNER_ID,
        "running_scripts": procs,
        "uptime_sec": int(time.time() - BOT_START_TIME),
    })

async def metrics(request):
    result = await _run_shell("df -h / && free -h && uptime")
    return web.Response(text=result, content_type="text/plain")

async def start_web_server():
    server = web.Application()
    server.router.add_get("/",        health_check)
    server.router.add_get("/health",  health_check)
    server.router.add_get("/metrics", metrics)
    runner = web.AppRunner(server)
    await runner.setup()
    
    # Koyeb assigns a PORT dynamically, default to 8000
    port = int(os.environ.get("PORT", 8000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"✅ Web server started on port {port} (Koyeb Health Check)")

BOT_START_TIME = time.time()

# ════════════════════════════════════════════════════════════════════════════
# 🛡️ Auth Filter
# ════════════════════════════════════════════════════════════════════════════
async def is_owner(_, __, message: Message):
    return message.from_user and message.from_user.id == OWNER_ID

owner_filter = filters.create(is_owner)

# ════════════════════════════════════════════════════════════════════════════
# 🔧 Helper: run shell and return output string
# ════════════════════════════════════════════════════════════════════════════
async def _run_shell(cmd: str, timeout: int = 60) -> str:
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        out = (stdout.decode(errors="replace") + stderr.decode(errors="replace")).strip()
        return out or "[No output / Command ran successfully]"
    except asyncio.TimeoutError:
        return f"[TIMEOUT after {timeout}s] Command may still be running in background."
    except Exception:
        return traceback.format_exc()

async def _send_output(message: Message, msg, cmd: str, output: str):
    header = f"**OUTPUT** (`{cmd[:60]}`):\n"
    if len(output) > 3800:
        path = f"/tmp/output_{int(time.time())}.txt"
        with open(path, "w", encoding="utf-8") as f:
            f.write(output)
        await msg.delete()
        await message.reply_document(path, caption=header + "[Output too long, see file]")
        os.remove(path)
    else:
        await msg.edit_text(f"{header}```\n{output}\n```")

# ════════════════════════════════════════════════════════════════════════════
# /start
# ════════════════════════════════════════════════════════════════════════════
@app.on_message(filters.command("start"))
async def start_cmd(_, message: Message):
    text = (
        "🔥 **Advanced Terminal Bot v2.0**\n\n"
        "**Shell Commands:**\n"
        "  `/sh <cmd>` — Run any shell command\n"
        "  `/bash <cmd>` — Alias of /sh\n\n"
        "**Script/Bot Runner:**\n"
        "  `/run <tag> <file.py> [args]` — Start a script in background\n"
        "  `/runreq <tag>` — pip install requirements.txt inside scripts/<tag>/\n"
        "  `/stop <tag>` — Kill a running script\n"
        "  `/stopall` — Kill ALL running scripts\n"
        "  `/ps` — List running scripts\n"
        "  `/log <tag> [lines]` — Tail log of a running script\n\n"
        "**File Manager:**\n"
        "  Send any file → auto-saved to `./scripts/`\n"
        "  `/ls [path]` — List directory\n"
        "  `/rm <path>` — Delete file/dir\n"
        "  `/mkdir <path>` — Create directory\n"
        "  `/upload <path>` — Send a file from server to you\n\n"
        "**System:**\n"
        "  `/sys` — CPU / RAM / Disk info\n"
        "  `/ping` — Check bot latency\n"
        "  `/env` — Show env variables (safe)\n"
        "  `/setenv KEY VALUE` — Set env variable for this session\n"
    )
    await message.reply_text(text)

# ════════════════════════════════════════════════════════════════════════════
# /sh  /bash  /termux — Shell execution
# ════════════════════════════════════════════════════════════════════════════
@app.on_message(filters.command(["sh", "bash", "termux"]) & owner_filter)
async def shell_cmd(_, message: Message):
    if len(message.command) < 2:
        return await message.reply_text("❌ Usage: `/sh <command>`")
    cmd = message.text.split(maxsplit=1)[1]
    msg = await message.reply_text(f"⚙️ Running: `{cmd[:80]}`")
    output = await _run_shell(cmd)
    await _send_output(message, msg, cmd, output)

# ════════════════════════════════════════════════════════════════════════════
# /run — Start a background script (another bot's main.py, etc.)
# ════════════════════════════════════════════════════════════════════════════
@app.on_message(filters.command("run") & owner_filter)
async def run_script(_, message: Message):
    """
    Usage: /run <tag> <script_path> [args...]
    Example: /run mybot scripts/mybot/main.py
    The tag is used to stop/log the process later.
    """
    parts = message.text.split(maxsplit=3)
    if len(parts) < 3:
        return await message.reply_text(
            "❌ Usage: `/run <tag> <script_path> [args]`\n"
            "Example: `/run leechbot scripts/leechbot/main.py`"
        )
    tag       = parts[1]
    script    = parts[2]
    extra     = parts[3] if len(parts) > 3 else ""

    if tag in running_processes:
        return await message.reply_text(f"⚠️ `{tag}` already running. Use `/stop {tag}` first.")

    if not os.path.exists(script):
        return await message.reply_text(f"❌ File not found: `{script}`")

    log_path = str(LOGS_DIR / f"{tag}.log")
    cmd = f"{sys.executable} -u {script} {extra}".strip()

    log_file = open(log_path, "w", buffering=1, encoding="utf-8")
    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=log_file,
        stderr=log_file,
        preexec_fn=os.setsid,   # New process group so we can kill cleanly
    )

    running_processes[tag] = {
        "process": proc,
        "cmd":     cmd,
        "started": time.time(),
        "log":     log_path,
        "log_fh":  log_file,
    }

    await message.reply_text(
        f"🚀 **Started** `{tag}`\n"
        f"**PID:** `{proc.pid}`\n"
        f"**CMD:** `{cmd}`\n"
        f"**Log:** `{log_path}`\n\n"
        f"Use `/log {tag}` to see output."
    )

    # Background watcher — auto-remove from registry when process dies
    asyncio.create_task(_watch_process(tag, message))

async def _watch_process(tag: str, message: Message):
    entry = running_processes.get(tag)
    if not entry:
        return
    await entry["process"].wait()
    rc = entry["process"].returncode
    entry["log_fh"].close()
    running_processes.pop(tag, None)
    try:
        await message.reply_text(
            f"⚠️ Script `{tag}` **exited** (return code `{rc}`).\n"
            f"Use `/log {tag}` to see last output (log still available)."
        )
    except Exception:
        pass

# ════════════════════════════════════════════════════════════════════════════
# /runreq — pip install requirements for a tag folder
# ════════════════════════════════════════════════════════════════════════════
@app.on_message(filters.command("runreq") & owner_filter)
async def run_requirements(_, message: Message):
    parts = message.command
    if len(parts) < 2:
        return await message.reply_text("❌ Usage: `/runreq <tag>`\nLooks for `scripts/<tag>/requirements.txt`")
    tag = parts[1]
    req = SCRIPTS_DIR / tag / "requirements.txt"
    if not req.exists():
        return await message.reply_text(f"❌ Not found: `{req}`")
    msg = await message.reply_text(f"📦 Installing requirements for `{tag}`...")
    output = await _run_shell(f"{sys.executable} -m pip install -r {req} --break-system-packages", timeout=300)
    await _send_output(message, msg, f"pip install -r {req}", output)

# ════════════════════════════════════════════════════════════════════════════
# /stop — Kill a running script
# ════════════════════════════════════════════════════════════════════════════
@app.on_message(filters.command("stop") & owner_filter)
async def stop_script(_, message: Message):
    parts = message.command
    if len(parts) < 2:
        return await message.reply_text("❌ Usage: `/stop <tag>`")
    tag = parts[1]
    entry = running_processes.get(tag)
    if not entry:
        return await message.reply_text(f"❌ No running process with tag `{tag}`.")
    try:
        os.killpg(os.getpgid(entry["process"].pid), signal.SIGTERM)
    except Exception:
        entry["process"].terminate()
    entry["log_fh"].close()
    running_processes.pop(tag, None)
    await message.reply_text(f"🛑 Stopped `{tag}`.")

# ════════════════════════════════════════════════════════════════════════════
# /stopall — Kill all running scripts
# ════════════════════════════════════════════════════════════════════════════
@app.on_message(filters.command("stopall") & owner_filter)
async def stop_all(_, message: Message):
    if not running_processes:
        return await message.reply_text("ℹ️ No scripts are running.")
    tags = list(running_processes.keys())
    for tag in tags:
        entry = running_processes.pop(tag)
        try:
            os.killpg(os.getpgid(entry["process"].pid), signal.SIGTERM)
        except Exception:
            entry["process"].terminate()
        entry["log_fh"].close()
    await message.reply_text(f"🛑 Stopped all: `{'`, `'.join(tags)}`")

# ════════════════════════════════════════════════════════════════════════════
# /ps — List running scripts
# ════════════════════════════════════════════════════════════════════════════
@app.on_message(filters.command("ps") & owner_filter)
async def list_processes(_, message: Message):
    if not running_processes:
        return await message.reply_text("ℹ️ No scripts running.")
    lines = ["**Running Scripts:**\n"]
    for tag, v in running_processes.items():
        uptime = int(time.time() - v["started"])
        lines.append(f"🟢 **{tag}** | PID `{v['process'].pid}` | Uptime `{uptime}s`\n   CMD: `{v['cmd'][:80]}`")
    await message.reply_text("\n".join(lines))

# ════════════════════════════════════════════════════════════════════════════
# /log — Tail log of a script
# ════════════════════════════════════════════════════════════════════════════
@app.on_message(filters.command("log") & owner_filter)
async def tail_log(_, message: Message):
    parts = message.command
    if len(parts) < 2:
        return await message.reply_text("❌ Usage: `/log <tag> [lines=50]`")
    tag   = parts[1]
    lines = int(parts[2]) if len(parts) > 2 else 50

    # Check running or old logs
    log_path = LOGS_DIR / f"{tag}.log"
    if not log_path.exists():
        return await message.reply_text(f"❌ No log for `{tag}`.")

    output = await _run_shell(f"tail -n {lines} {log_path}")
    await _send_output(message, await message.reply_text("📋 Fetching log..."), f"tail {tag}", output)

# ════════════════════════════════════════════════════════════════════════════
# 📥 File Receiver — auto-save uploaded files
# ════════════════════════════════════════════════════════════════════════════
@app.on_message(filters.document & owner_filter)
async def file_receiver(_, message: Message):
    fname = message.document.file_name
    dest  = SCRIPTS_DIR / fname
    msg   = await message.reply_text(f"📥 Downloading `{fname}`...")
    await message.download(file_name=str(dest))
    size  = dest.stat().st_size
    await msg.edit_text(
        f"✅ **Saved:** `{dest}`\n"
        f"**Size:** `{size:,} bytes`\n\n"
        f"**Quick run:** `/sh python {dest}`\n"
        f"**Background:** `/run mytag {dest}`"
    )

# ════════════════════════════════════════════════════════════════════════════
# /ls — Directory listing
# ════════════════════════════════════════════════════════════════════════════
@app.on_message(filters.command("ls") & owner_filter)
async def ls_dir(_, message: Message):
    path = message.text.split(maxsplit=1)[1] if len(message.command) > 1 else "."
    out  = await _run_shell(f"ls -lah {path}")
    await message.reply_text(f"**📁 {path}**\n```\n{out[:3800]}\n```")

# ════════════════════════════════════════════════════════════════════════════
# /rm — Delete file
# ════════════════════════════════════════════════════════════════════════════
@app.on_message(filters.command("rm") & owner_filter)
async def rm_file(_, message: Message):
    if len(message.command) < 2:
        return await message.reply_text("❌ Usage: `/rm <path>`")
    path = message.text.split(maxsplit=1)[1]
    out  = await _run_shell(f"rm -rf {path} && echo 'Deleted: {path}'")
    await message.reply_text(f"```\n{out}\n```")

# ════════════════════════════════════════════════════════════════════════════
# /mkdir — Create directory
# ════════════════════════════════════════════════════════════════════════════
@app.on_message(filters.command("mkdir") & owner_filter)
async def mkdir_cmd(_, message: Message):
    if len(message.command) < 2:
        return await message.reply_text("❌ Usage: `/mkdir <path>`")
    path = message.text.split(maxsplit=1)[1]
    os.makedirs(path, exist_ok=True)
    await message.reply_text(f"✅ Created: `{path}`")

# ════════════════════════════════════════════════════════════════════════════
# /upload — Send a server file to Telegram
# ════════════════════════════════════════════════════════════════════════════
@app.on_message(filters.command("upload") & owner_filter)
async def upload_file(_, message: Message):
    if len(message.command) < 2:
        return await message.reply_text("❌ Usage: `/upload <path>`")
    path = message.text.split(maxsplit=1)[1]
    if not os.path.exists(path):
        return await message.reply_text(f"❌ Not found: `{path}`")
    msg = await message.reply_text(f"📤 Uploading `{path}`...")
    await message.reply_document(path)
    await msg.delete()

# ════════════════════════════════════════════════════════════════════════════
# /sys — System stats
# ════════════════════════════════════════════════════════════════════════════
@app.on_message(filters.command("sys") & owner_filter)
async def sys_info(_, message: Message):
    cmd = "echo '=== CPU ===' && cat /proc/cpuinfo | grep 'model name' | head -1 && echo '=== RAM ===' && free -h && echo '=== DISK ===' && df -h && echo '=== UPTIME ===' && uptime"
    out = await _run_shell(cmd)
    await message.reply_text(f"```\n{out[:3800]}\n```")

# ════════════════════════════════════════════════════════════════════════════
# /ping
# ════════════════════════════════════════════════════════════════════════════
@app.on_message(filters.command("ping") & owner_filter)
async def ping_cmd(_, message: Message):
    t = time.time()
    msg = await message.reply_text("🏓 Pong!")
    latency = round((time.time() - t) * 1000)
    await msg.edit_text(f"🏓 Pong! `{latency}ms`")

# ════════════════════════════════════════════════════════════════════════════
# /env — Show env vars (redacts secrets)
# ════════════════════════════════════════════════════════════════════════════
@app.on_message(filters.command("env") & owner_filter)
async def show_env(_, message: Message):
    secret_keys = {"BOT_TOKEN", "API_HASH", "PASSWORD", "SECRET", "KEY", "TOKEN"}
    lines = []
    for k, v in sorted(os.environ.items()):
        if any(s in k.upper() for s in secret_keys):
            v = v[:4] + "****"
        lines.append(f"{k}={v}")
    text = "\n".join(lines)[:3800]
    await message.reply_text(f"```\n{text}\n```")

# ════════════════════════════════════════════════════════════════════════════
# /setenv — Set environment variable (for current session)
# ════════════════════════════════════════════════════════════════════════════
@app.on_message(filters.command("setenv") & owner_filter)
async def set_env(_, message: Message):
    parts = message.text.split(maxsplit=2)
    if len(parts) < 3:
        return await message.reply_text("❌ Usage: `/setenv KEY VALUE`")
    os.environ[parts[1]] = parts[2]
    await message.reply_text(f"✅ Set `{parts[1]}` for this session.")

# ════════════════════════════════════════════════════════════════════════════
# ⚡ Main
# ════════════════════════════════════════════════════════════════════════════
async def main():
    await start_web_server()
    await app.start()
    print("🚀 Advanced Terminal Bot v2.0 is ONLINE on Koyeb!")
    await idle()  # Fix: Ye lagana zaroori hai updates receive karne ke liye
    await app.stop()

if __name__ == "__main__":
    asyncio.run(main())