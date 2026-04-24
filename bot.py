import os
import sys
import asyncio
import traceback
import signal
import time
from pathlib import Path
from pyrogram import Client, filters, idle
from pyrogram.types import Message
from aiohttp import web

# ╔══════════════════════════════════════════╗
# ║     🔥 ADVANCED TERMINAL BOT v2.0 🔥     ║
# ║         By Ayuprime | Koyeb              ║
# ╚══════════════════════════════════════════╝

API_ID    = int(os.environ.get("API_ID", "33675350"))
API_HASH  = os.environ.get("API_HASH", "2f97c845b067a750c9f36fec497acf97")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8751265425:AAFJ5pzO0fgU80tRSCN5DplRCwna4Euw9Lg")
OWNER_ID  = int(os.environ.get("OWNER_ID", "8493596199"))

# ──── Directory Setup ────────────────────────────────────────────────────────
SCRIPTS_DIR = Path("./scripts")
LOGS_DIR    = Path("./logs")
VENVS_DIR   = Path("./venvs")
for d in [SCRIPTS_DIR, LOGS_DIR, VENVS_DIR]:
    d.mkdir(exist_ok=True)

# ──── Process Registry ───────────────────────────────────────────────────────
running_processes: dict = {}

# BOT_START_TIME — defined BEFORE web server so health_check can use it
BOT_START_TIME = time.time()

app = Client(
    "AdvTerminalBot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    in_memory=True,
)

# ════════════════════════════════════════════════════════════════════════════
# 🌐 HEALTH CHECK WEB SERVER
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

async def start_web_server():
    server = web.Application()
    server.router.add_get("/",        health_check)
    server.router.add_get("/health",  health_check)
    server.router.add_get("/metrics", metrics)
    runner = web.AppRunner(server)
    await runner.setup()
    port = int(os.environ.get("PORT", 8000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"✅ Web server started on port {port}")

# ════════════════════════════════════════════════════════════════════════════
# 🛡️ Auth Filter
# ════════════════════════════════════════════════════════════════════════════
async def is_owner(_, __, message: Message):
    return message.from_user and message.from_user.id == OWNER_ID

owner_filter = filters.create(is_owner)

# ════════════════════════════════════════════════════════════════════════════
# 🔧 Helpers
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
        return f"[TIMEOUT after {timeout}s]"
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
# Commands
# ════════════════════════════════════════════════════════════════════════════
@app.on_message(filters.command("start"))
async def start_cmd(_, message: Message):
    text = (
        "🔥 **Advanced Terminal Bot v2.0**\n\n"
        "**Shell:** `/sh <cmd>` | `/bash <cmd>`\n\n"
        "**Script Runner:**\n"
        "  `/run <tag> <file.py>` — Background mein start karo\n"
        "  `/runreq <tag>` — pip install requirements\n"
        "  `/stop <tag>` | `/stopall` | `/ps` | `/log <tag>`\n\n"
        "**Files:** Send any file to save • `/ls` • `/rm` • `/mkdir` • `/upload <path>`\n\n"
        "**System:** `/sys` • `/ping` • `/env` • `/setenv KEY VAL`"
    )
    await message.reply_text(text)

@app.on_message(filters.command(["sh", "bash", "termux"]) & owner_filter)
async def shell_cmd(_, message: Message):
    if len(message.command) < 2:
        return await message.reply_text("❌ Usage: `/sh <command>`")
    cmd = message.text.split(maxsplit=1)[1]
    msg = await message.reply_text(f"⚙️ Running: `{cmd[:80]}`")
    output = await _run_shell(cmd)
    await _send_output(message, msg, cmd, output)

@app.on_message(filters.command("run") & owner_filter)
async def run_script(_, message: Message):
    parts = message.text.split(maxsplit=3)
    if len(parts) < 3:
        return await message.reply_text("❌ Usage: `/run <tag> <script_path> [args]`")
    tag    = parts[1]
    script = parts[2]
    extra  = parts[3] if len(parts) > 3 else ""
    if tag in running_processes:
        return await message.reply_text(f"⚠️ `{tag}` already running. Use `/stop {tag}` first.")
    if not os.path.exists(script):
        return await message.reply_text(f"❌ File not found: `{script}`")
    log_path = str(LOGS_DIR / f"{tag}.log")
    cmd = f"{sys.executable} -u {script} {extra}".strip()
    log_file = open(log_path, "w", buffering=1, encoding="utf-8")
    proc = await asyncio.create_subprocess_shell(
        cmd, stdout=log_file, stderr=log_file, preexec_fn=os.setsid,
    )
    running_processes[tag] = {"process": proc, "cmd": cmd, "started": time.time(),
                               "log": log_path, "log_fh": log_file}
    await message.reply_text(
        f"🚀 **Started** `{tag}`\n**PID:** `{proc.pid}`\n**Log:** `/log {tag}`"
    )
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
        await message.reply_text(f"⚠️ `{tag}` exited (code `{rc}`). Use `/log {tag}` to check.")
    except Exception:
        pass

@app.on_message(filters.command("runreq") & owner_filter)
async def run_requirements(_, message: Message):
    parts = message.command
    if len(parts) < 2:
        return await message.reply_text("❌ Usage: `/runreq <tag>`")
    req = SCRIPTS_DIR / parts[1] / "requirements.txt"
    if not req.exists():
        return await message.reply_text(f"❌ Not found: `{req}`")
    msg = await message.reply_text(f"📦 Installing for `{parts[1]}`...")
    output = await _run_shell(f"{sys.executable} -m pip install -r {req}", timeout=300)
    await _send_output(message, msg, f"pip install -r {req}", output)

@app.on_message(filters.command("stop") & owner_filter)
async def stop_script(_, message: Message):
    if len(message.command) < 2:
        return await message.reply_text("❌ Usage: `/stop <tag>`")
    tag = message.command[1]
    entry = running_processes.get(tag)
    if not entry:
        return await message.reply_text(f"❌ No process `{tag}`.")
    try:
        os.killpg(os.getpgid(entry["process"].pid), signal.SIGTERM)
    except Exception:
        entry["process"].terminate()
    entry["log_fh"].close()
    running_processes.pop(tag, None)
    await message.reply_text(f"🛑 Stopped `{tag}`.")

@app.on_message(filters.command("stopall") & owner_filter)
async def stop_all(_, message: Message):
    if not running_processes:
        return await message.reply_text("ℹ️ No scripts running.")
    tags = list(running_processes.keys())
    for tag in tags:
        entry = running_processes.pop(tag)
        try:
            os.killpg(os.getpgid(entry["process"].pid), signal.SIGTERM)
        except Exception:
            entry["process"].terminate()
        entry["log_fh"].close()
    await message.reply_text(f"🛑 Stopped: `{'`, `'.join(tags)}`")

@app.on_message(filters.command("ps") & owner_filter)
async def list_processes(_, message: Message):
    if not running_processes:
        return await message.reply_text("ℹ️ No scripts running.")
    lines = ["**Running Scripts:**\n"]
    for tag, v in running_processes.items():
        uptime = int(time.time() - v["started"])
        lines.append(f"🟢 **{tag}** | PID `{v['process'].pid}` | `{uptime}s`\n  `{v['cmd'][:80]}`")
    await message.reply_text("\n".join(lines))

@app.on_message(filters.command("log") & owner_filter)
async def tail_log(_, message: Message):
    parts = message.command
    if len(parts) < 2:
        return await message.reply_text("❌ Usage: `/log <tag> [lines=50]`")
    tag   = parts[1]
    lines = int(parts[2]) if len(parts) > 2 else 50
    log_path = LOGS_DIR / f"{tag}.log"
    if not log_path.exists():
        return await message.reply_text(f"❌ No log for `{tag}`.")
    output = await _run_shell(f"tail -n {lines} {log_path}")
    await _send_output(message, await message.reply_text("📋 Fetching..."), f"tail {tag}", output)

@app.on_message(filters.document & owner_filter)
async def file_receiver(_, message: Message):
    fname = message.document.file_name
    dest  = SCRIPTS_DIR / fname
    msg   = await message.reply_text(f"📥 Downloading `{fname}`...")
    await message.download(file_name=str(dest))
    size  = dest.stat().st_size
    await msg.edit_text(
        f"✅ **Saved:** `{dest}` ({size:,} bytes)\n"
        f"Run: `/run mytag {dest}`"
    )

@app.on_message(filters.command("ls") & owner_filter)
async def ls_dir(_, message: Message):
    path = message.text.split(maxsplit=1)[1] if len(message.command) > 1 else "."
    out  = await _run_shell(f"ls -lah {path}")
    await message.reply_text(f"**📁 {path}**\n```\n{out[:3800]}\n```")

@app.on_message(filters.command("rm") & owner_filter)
async def rm_file(_, message: Message):
    if len(message.command) < 2:
        return await message.reply_text("❌ Usage: `/rm <path>`")
    path = message.text.split(maxsplit=1)[1]
    out  = await _run_shell(f"rm -rf {path} && echo 'Deleted.'")
    await message.reply_text(f"```\n{out}\n```")

@app.on_message(filters.command("mkdir") & owner_filter)
async def mkdir_cmd(_, message: Message):
    if len(message.command) < 2:
        return await message.reply_text("❌ Usage: `/mkdir <path>`")
    path = message.text.split(maxsplit=1)[1]
    os.makedirs(path, exist_ok=True)
    await message.reply_text(f"✅ Created: `{path}`")

@app.on_message(filters.command("upload") & owner_filter)
async def upload_file(_, message: Message):
    if len(message.command) < 2:
        return await message.reply_text("❌ Usage: `/upload <path>`")
    path = message.text.split(maxsplit=1)[1]
    if not os.path.exists(path):
        return await message.reply_text(f"❌ Not found: `{path}`")
    msg = await message.reply_text(f"📤 Uploading...")
    await message.reply_document(path)
    await msg.delete()

@app.on_message(filters.command("sys") & owner_filter)
async def sys_info(_, message: Message):
    cmd = "cat /proc/cpuinfo | grep 'model name' | head -1 && free -h && df -h && uptime"
    out = await _run_shell(cmd)
    await message.reply_text(f"```\n{out[:3800]}\n```")

@app.on_message(filters.command("ping") & owner_filter)
async def ping_cmd(_, message: Message):
    t   = time.time()
    msg = await message.reply_text("🏓 Pong!")
    await msg.edit_text(f"🏓 Pong! `{round((time.time()-t)*1000)}ms`")

@app.on_message(filters.command("env") & owner_filter)
async def show_env(_, message: Message):
    secret_keys = {"BOT_TOKEN", "API_HASH", "PASSWORD", "SECRET", "KEY", "TOKEN"}
    lines = []
    for k, v in sorted(os.environ.items()):
        if any(s in k.upper() for s in secret_keys):
            v = v[:4] + "****"
        lines.append(f"{k}={v}")
    await message.reply_text(f"```\n{chr(10).join(lines)[:3800]}\n```")

@app.on_message(filters.command("setenv") & owner_filter)
async def set_env(_, message: Message):
    parts = message.text.split(maxsplit=2)
    if len(parts) < 3:
        return await message.reply_text("❌ Usage: `/setenv KEY VALUE`")
    os.environ[parts[1]] = parts[2]
    await message.reply_text(f"✅ Set `{parts[1]}`.")

# ════════════════════════════════════════════════════════════════════════════
# ⚡ Main
# ════════════════════════════════════════════════════════════════════════════
async def main():
    await start_web_server()
    await app.start()
    me = await app.get_me()
    print(f"🚀 Bot ONLINE: @{me.username} | Owner: {OWNER_ID}")
    await idle()
    await app.stop()

if __name__ == "__main__":
    asyncio.run(main())
