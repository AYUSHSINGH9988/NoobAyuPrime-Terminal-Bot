import os
import re
import sys
import asyncio
import traceback
import signal
import time
import shutil
from datetime import datetime, timezone
from pathlib import Path
from pyrogram import Client, filters, idle
from pyrogram.types import Message, InputMediaPhoto
from aiohttp import web
import motor.motor_asyncio

# ══════════════════════════════════════════════════════════════
#   ⚡ ADVANCED TERMINAL BOT  — Ayuprime 
# ══════════════════════════════════════════════════════════════

API_ID           = int(os.environ.get("API_ID", "33675350"))
API_HASH         = os.environ.get("API_HASH", "2f97c845b067a750c9f36fec497acf97")
BOT_TOKEN        = os.environ.get("BOT_TOKEN", "8644839571:AAGTIo0Bfxc8y-BommwhJhbVUp1uTP66kAI")
OWNER_ID         = int(os.environ.get("OWNER_ID", "8493596199"))
MONGO_URI        = os.environ.get("MONGO_URI", "")
MAX_UPTIME_HRS   = int(os.environ.get("MAX_UPTIME_HRS", "40"))
RCLONE_REMOTE    = os.environ.get("RCLONE_REMOTE", "mega").strip()
RCLONE_CONF_DATA = os.environ.get("RCLONE_CONF_DATA", "")
REMOTE_VAULT     = f"{RCLONE_REMOTE}:Ayuprime_Vault"

SCRIPTS_DIR = Path("./scripts")
LOGS_DIR    = Path("./logs")
for d in [SCRIPTS_DIR, LOGS_DIR, Path("./venvs")]:
    d.mkdir(exist_ok=True)

running_processes:  dict = {}
live_terminals:     dict = {}   # user_id -> proc  (for /sh stdin piping)
foreground_process: dict = {}   # user_id -> {"proc", "msg", "m", "script"}  (/run interactive)
user_cwd:           dict = {}
authorized_users:   set  = set()   # user_ids allowed by owner via /auth
last_bot_msg:       dict = {}      # uid → last bot reply (auto-cleaned on next cmd)
BOT_START_TIME = time.time()

# ══════════════════════════════════════════════════════════════════════════════
# 🔥  PHOENIX SYSTEM — MongoDB persistence, auto-backup, smart restart alerts
# ══════════════════════════════════════════════════════════════════════════════

_mongo_client = None
_db           = None

def _get_db():
    return _db

async def _init_mongo():
    global _mongo_client, _db
    if not MONGO_URI:
        print("⚠️  MONGO_URI not set — Phoenix persistence disabled.")
        return False
    try:
        _mongo_client = motor.motor_asyncio.AsyncIOMotorClient(
            MONGO_URI, serverSelectionTimeoutMS=8000
        )
        await _mongo_client.admin.command("ping")
        _db = _mongo_client["terminal_bot"]
        print("✅ MongoDB connected.")
        return True
    except Exception as e:
        print(f"❌ MongoDB connection failed: {e}")
        return False

async def _setup_rclone():
    if not Path("./rclone").exists():
        print("⚙️  rclone not found — downloading...")
        out = await _shell(
            "curl -O https://downloads.rclone.org/rclone-current-linux-amd64.zip && "
            "unzip -j rclone-current-linux-amd64.zip '*/rclone' -d . && "
            "chmod +x ./rclone && "
            "rm rclone-current-linux-amd64.zip",
            timeout=120,
        )
        print(f"⚙️  rclone install output: {out}")
    else:
        print("✅ rclone binary found.")
    if RCLONE_CONF_DATA:
        Path("rclone.conf").write_text(RCLONE_CONF_DATA, encoding="utf-8")
        print("✅ rclone.conf written from RCLONE_CONF_DATA.")
    else:
        print("⚠️  RCLONE_CONF_DATA not set — rclone.conf not written.")

async def _record_boot():
    db = _get_db()
    if db is None:
        return
    try:
        now  = datetime.now(timezone.utc)
        last = await db.state.find_one({"_id": "boot"})
        if last and last.get("booted_at"):
            last_boot = last["booted_at"]
            if last_boot.tzinfo is None:
                last_boot = last_boot.replace(tzinfo=timezone.utc)
            gap_sec = int((now - last_boot).total_seconds())
            await db.sessions.insert_one({
                "booted_at":    last_boot,
                "restarted_at": now,
                "gap_sec":      gap_sec,
                "gap_human":    _uptime(gap_sec),
            })
            print(f"📊 Last session gap: {_uptime(gap_sec)}")
        await db.state.update_one(
            {"_id": "boot"},
            {"$set": {"booted_at": now, "max_uptime_hrs": MAX_UPTIME_HRS}},
            upsert=True,
        )
    except Exception as e:
        print(f"⚠️  _record_boot error: {e}")

async def _uptime_alert_task():
    await asyncio.sleep(300)
    alerted_hours: set = set()
    while True:
        try:
            elapsed_hrs = (time.time() - BOT_START_TIME) / 3600
            remaining   = MAX_UPTIME_HRS - elapsed_hrs
            if 0 < remaining <= 4:
                bucket = int(remaining)
                if bucket not in alerted_hours:
                    alerted_hours.add(bucket)
                    urgency = "🔴" if bucket <= 1 else ("🟠" if bucket == 2 else "🟡")
                    msg = (
                        f"```\n{urgency} UPTIME ALERT\n"
                        f"──────────────────────────\n"
                        f"Running        : {elapsed_hrs:.1f}h\n"
                        f"Max limit      : {MAX_UPTIME_HRS}h\n"
                        f"Until sleep    : ~{remaining:.1f}h\n"
                        f"──────────────────────────\n"
                        f"Restart HuggingFace Space soon!\n"
                        f"Use /restart when ready.\n```"
                    )
                    await app.send_message(OWNER_ID, msg)
            elif remaining <= 0 and 0 not in alerted_hours:
                alerted_hours.add(0)
                await app.send_message(
                    OWNER_ID,
                    "```\n🔴 UPTIME LIMIT REACHED\nBot may sleep any moment. Restart now!\n```"
                )
        except Exception as e:
            print(f"⚠️  Uptime alert error: {e}")
        await asyncio.sleep(3600)

async def _auto_restart_task():
    RESTART_INTERVAL = 10 * 3600
    WARN_BEFORE      = 5 * 60
    await asyncio.sleep(RESTART_INTERVAL - WARN_BEFORE)
    try:
        await app.send_message(
            OWNER_ID,
            "```\n[AUTO-RESTART]  Scheduled restart in 5 minutes.\n"
            f"Bot has been running for ~10h.\n"
            "Saving state and restarting cleanly...\n```"
        )
    except Exception as e:
        print(f"⚠️  Auto-restart warning failed: {e}")
    await asyncio.sleep(WARN_BEFORE)
    try:
        await app.send_message(OWNER_ID, "```\n[AUTO-RESTART]  Restarting now...\n```")
    except Exception:
        pass
    await asyncio.sleep(1)
    print("🔄 Auto-restart triggered after 10h uptime")
    os.execv(sys.executable, [sys.executable] + sys.argv)

MODULES_FILE = "backupmodules.txt"

def _read_modules_local() -> list:
    try:
        if Path(MODULES_FILE).exists():
            lines = Path(MODULES_FILE).read_text(encoding="utf-8").splitlines()
            return sorted(set(l.strip() for l in lines if l.strip() and not l.startswith("#")))
    except Exception as e:
        print(f"⚠️  _read_modules_local: {e}")
    return []

def _write_modules_local(mods: list):
    try:
        Path(MODULES_FILE).write_text(
            "# Auto-managed by Terminal Bot\n" + "\n".join(sorted(set(mods))) + "\n",
            encoding="utf-8",
        )
    except Exception as e:
        print(f"⚠️  _write_modules_local: {e}")

async def _sync_modules_to_vault():
    if not Path(MODULES_FILE).exists():
        return
    out = await _shell(
        f'./rclone copy "{MODULES_FILE}" "{REMOTE_VAULT}" --config rclone.conf', timeout=60
    )
    if "error" in out.lower():
        print(f"⚠️  Module vault sync failed: {out[:200]}")
    else:
        print(f"☁️  backupmodules.txt synced → {REMOTE_VAULT}")

async def _sync_modules_from_vault():
    out = await _shell(
        f'./rclone copy "{REMOTE_VAULT}/{MODULES_FILE}" "." --config rclone.conf', timeout=60
    )
    if "error" in out.lower():
        print(f"⚠️  Module vault download failed: {out[:200]}")
    else:
        print(f"📥  backupmodules.txt downloaded from {REMOTE_VAULT}")

async def _track_pip_install(cmd: str):
    if "pip install" not in cmd and "pip3 install" not in cmd:
        return
    try:
        parts, packages, skip_next = cmd.split(), [], False
        for token in parts:
            if skip_next:
                skip_next = False
                continue
            if token in ("pip", "pip3", "install", "-m", "python", "python3"):
                continue
            if token.startswith("--"):
                continue
            if token == "-r":
                skip_next = True
                continue
            if token.startswith("-"):
                continue
            if token.endswith(".txt") or token.endswith(".py"):
                continue
            pkg = re.split(r"[>=<!]", token)[0].strip()
            if pkg:
                packages.append(pkg.lower())
        if packages:
            merged = sorted(set(_read_modules_local() + packages))
            _write_modules_local(merged)
            await _sync_modules_to_vault()
            print(f"📦 Tracked + synced: {packages}")
    except Exception as e:
        print(f"⚠️  _track_pip_install: {e}")

async def _backup_file(local_path: str, status_msg=None, base_text: str = ""):
    db    = _get_db()
    fpath = Path(local_path)
    if not fpath.exists() or not fpath.is_file():
        return

    file_size = fpath.stat().st_size
    THROTTLE  = 1.5
    last_edit = [0.0]

    async def _rclone_progress():
        cmd = (
            f'./rclone copy "{fpath}" "{REMOTE_VAULT}/{fpath.parent}"'
            ' --config rclone.conf --stats 1s --stats-one-line'
        )
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
            )
        except Exception as e:
            return f"spawn error: {e}"

        buf        = []
        start_time = time.time()

        while True:
            if time.time() - start_time > 180:
                try:
                    proc.kill()
                except Exception:
                    pass
                break
            try:
                chunk = await asyncio.wait_for(proc.stdout.read(512), timeout=0.5)
            except asyncio.TimeoutError:
                chunk = b""
            if chunk:
                text = chunk.decode(errors="replace")
                buf.append(text)
                pct = None
                for line in text.splitlines():
                    m_pct = re.search(r',\s*(\d+)%', line)
                    if m_pct:
                        pct = int(m_pct.group(1))
                    if pct is None and file_size > 0:
                        m_bytes = re.search(r'Transferred:\s+([\d.]+)\s*(\w+)', line)
                        if m_bytes:
                            val, unit = float(m_bytes.group(1)), m_bytes.group(2).upper()
                            mult = {"B": 1, "KIB": 1024, "MIB": 1048576, "GIB": 1073741824}
                            transferred = int(val * mult.get(unit, 1))
                            pct = min(99, int(transferred * 100 / file_size))
                if pct is not None and status_msg and base_text:
                    now = time.time()
                    if now - last_edit[0] >= THROTTLE:
                        last_edit[0] = now
                        try:
                            await status_msg.edit_text(
                                base_text[:-3] + f"\nBackup  :  ☁️ Uploading {pct}%\n```"
                            )
                        except Exception:
                            pass
            if not chunk and proc.stdout.at_eof():
                break
        await proc.wait()
        return "".join(buf)

    try:
        out = await _rclone_progress()
        if out and ("error" in out.lower() or "failed" in out.lower()):
            raise RuntimeError(out[:300])
        if db is not None:
            try:
                await db.backups.update_one(
                    {"file_name": fpath.name, "original_folder_path": str(fpath.parent)},
                    {"$set": {
                        "file_name":            fpath.name,
                        "original_folder_path": str(fpath.parent),
                        "full_path":            str(fpath),
                        "backed_up_at":         datetime.now(timezone.utc),
                        "size_bytes":           file_size,
                    }},
                    upsert=True,
                )
            except Exception as e:
                print(f"⚠️  MongoDB backup metadata error: {e}")
        print(f"☁️  Backed up to Rclone Vault: {fpath.name}")
        if status_msg and base_text:
            try:
                await status_msg.edit_text(
                    base_text[:-3] + "\nBackup  :  ✅ Complete (Rclone Vault)\n```"
                )
            except Exception:
                pass
    except Exception as e:
        print(f"⚠️  _backup_file error ({fpath.name}): {e}")
        if status_msg and base_text:
            try:
                await status_msg.edit_text(
                    base_text[:-3] + "\nBackup  :  ❌ Failed\n```"
                )
            except Exception:
                pass

async def _phoenix_restore():
    db = _get_db()
    if db is None:
        return
    modules_restored = 0
    files_restored   = 0
    errors           = []

    try:
        await _sync_modules_from_vault()
        mods = _read_modules_local()
        if mods:
            print(f"📦 Restoring {len(mods)} modules: {mods}")
            env = os.environ.copy()
            env["PYTHONUNBUFFERED"] = "1"
            proc = await asyncio.create_subprocess_shell(
                f"{sys.executable} -m pip install {' '.join(mods)} -q",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
            )
            await asyncio.wait_for(proc.communicate(), timeout=300)
            modules_restored = len(mods)
            print(f"✅ {modules_restored} modules restored")
        else:
            print("ℹ️  backupmodules.txt empty or not in vault")
    except Exception as e:
        errors.append(f"Modules: {e}")
        print(f"⚠️  Module restore error: {e}")

    try:
        print(f"📥 Syncing vault: {REMOTE_VAULT}/ → ./")
        sync_out = await _shell(
            f'./rclone copy "{REMOTE_VAULT}/" "./" --config rclone.conf', timeout=300
        )
        print(f"🗂 Rclone restore output: {sync_out}")
        if "error" in sync_out.lower() or "failed" in sync_out.lower():
            errors.append(f"Rclone restore: {sync_out[:300]}")
        else:
            transferred = [l for l in sync_out.splitlines() if l.strip()]
            files_restored = (
                len(transferred) if transferred and sync_out != "✅ Done (no output)" else 0
            )
    except Exception as e:
        errors.append(f"File restore: {e}")
        print(f"⚠️  Rclone restore error: {e}")

    try:
        err_text = (
            ("\n⚠️ Errors:\n" + "\n".join(f"  • {e}" for e in errors[:5])) if errors else ""
        )
        msg = (
            f"```\n[PHOENIX BOOT — ENVIRONMENT RESTORED]\n"
            f"──────────────────────────────────────\n"
            f"Modules restored : {modules_restored}\n"
            f"Files synced     : {files_restored}\n"
            f"Max uptime limit : {MAX_UPTIME_HRS}h\n"
            f"Remote vault     : {REMOTE_VAULT}\n"
            f"──────────────────────────────────────\n"
            f"Bot is ready. All systems go.{err_text}\n```"
        )
        await app.send_message(OWNER_ID, msg)
    except Exception as e:
        print(f"⚠️  Phoenix ready message error: {e}")

async def _cmd_backup_status(m: Message):
    db = _get_db()
    if db is None:
        return await m.reply_text("❌ MongoDB not configured. Set `MONGO_URI` env var.")
    mods_list   = _read_modules_local()
    files_count = await db.backups.count_documents({})
    elapsed     = _uptime(int(time.time() - BOT_START_TIME))
    remaining   = MAX_UPTIME_HRS - (time.time() - BOT_START_TIME) / 3600
    msg = (
        f"```\n[PHOENIX STATUS]\n"
        f"─────────────────────────────────\n"
        f"Uptime          : {elapsed}\n"
        f"Max limit       : {MAX_UPTIME_HRS}h  (remaining: {remaining:.1f}h)\n"
        f"Backed up files : {files_count}\n"
        f"Tracked modules : {len(mods_list)}\n"
        f"Modules         : {', '.join(mods_list[:15]) or 'none'}\n"
        f"Remote vault    : {REMOTE_VAULT}\n"
        f"─────────────────────────────────\n"
        f"Commands:\n"
        f"  /backup files    — list backed up files\n"
        f"  /backup modules  — list tracked modules\n"
        f"  /backup now      — force backup scripts/ dir\n"
        f"  /backuplist      — list files in Rclone Vault\n"
        f"  /vaultdelete <path> — delete a file from Vault\n"
        f"  /setmaxuptime N  — change uptime alert threshold\n```"
    )
    await m.reply_text(msg)

async def _cmd_setmaxuptime(m: Message):
    global MAX_UPTIME_HRS
    if len(m.command) < 2:
        return await m.reply_text("Usage: `/setmaxuptime <hours>`")
    try:
        hrs = int(m.command[1])
        MAX_UPTIME_HRS = hrs
        db = _get_db()
        if db:
            await db.state.update_one(
                {"_id": "boot"}, {"$set": {"max_uptime_hrs": hrs}}, upsert=True
            )
        await m.reply_text(f"```\n[SET]  Max uptime limit → {hrs}h\n```")
    except ValueError:
        await m.reply_text("❌ Must be an integer. Example: `/setmaxuptime 36`")


app = Client(
    "AdvTerminalBot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    in_memory=True,
)

# ── Web server ────────────────────────────────────────────────────────────────
async def _health(request):
    procs = {
        tag: {"cmd": v["cmd"], "uptime_sec": int(time.time() - v["started"])}
        for tag, v in running_processes.items()
    }
    fg_active = {
        str(uid): {"script": v.get("script", "?"), "pid": v["proc"].pid}
        for uid, v in foreground_process.items()
        if v["proc"].returncode is None
    }
    return web.json_response({
        "status":          "online",
        "bot":             "AdvancedTerminalBot",
        "owner":           OWNER_ID,
        "running_scripts": procs,
        "foreground":      fg_active,
        "uptime_sec":      int(time.time() - BOT_START_TIME),
    })

async def _metrics(request):
    return web.Response(
        text=await _shell("df -h / && free -h && uptime"),
        content_type="text/plain",
    )

async def start_web_server():
    srv = web.Application()
    for path in ("/", "/health"):
        srv.router.add_get(path, _health)
    srv.router.add_get("/metrics", _metrics)
    runner = web.AppRunner(srv)
    await runner.setup()
    port = int(os.environ.get("PORT", 7860))
    await web.TCPSite(runner, "0.0.0.0", port).start()
    print(f"✅ Web server on port {port}")

# ── Auth ──────────────────────────────────────────────────────────────────────
async def _is_owner(_, __, m: Message):
    return m.from_user and m.from_user.id == OWNER_ID

owner_filter = filters.create(_is_owner)

# ── Authorized-user filter (owner + /auth'd users) ────────────────────────────
async def _is_allowed(_, __, m: Message):
    if not m.from_user:
        return False
    return m.from_user.id == OWNER_ID or m.from_user.id in authorized_users

allowed_filter = filters.create(_is_allowed)

# ── Auth persistence (MongoDB) ────────────────────────────────────────────────
async def _load_auth_users():
    db = _get_db()
    if db is None:
        return
    try:
        doc = await db.auth.find_one({"_id": "authorized"})
        if doc and "users" in doc:
            authorized_users.update(int(u) for u in doc["users"])
            print(f"✅ Loaded {len(authorized_users)} authorized user(s) from DB")
    except Exception as e:
        print(f"⚠️  _load_auth_users: {e}")

async def _save_auth_users():
    db = _get_db()
    if db is None:
        return
    try:
        await db.auth.update_one(
            {"_id": "authorized"},
            {"$set": {"users": list(authorized_users)}},
            upsert=True,
        )
    except Exception as e:
        print(f"⚠️  _save_auth_users: {e}")

# ── Core helpers ──────────────────────────────────────────────────────────────
async def _shell(cmd: str, timeout: int = 60) -> str:
    """Run command, return full output (non-streaming)."""
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        result = (out.decode(errors="replace") + err.decode(errors="replace")).strip()
        return result or "✅ Done (no output)"
    except asyncio.TimeoutError:
        return f"⏱ Timeout after {timeout}s"
    except Exception:
        return traceback.format_exc()

async def _shell_live(cmd: str, status_msg, header: str, timeout: int = 300):
    """
    Live terminal for /sh — streams stdout+stderr into one Telegram message
    every 1.5 s. Registers in live_terminals so handle_terminal_input can pipe
    stdin from the owner's chat.
    """
    INTERVAL  = 1.5
    MAX_CHARS = 3500
    ANSI_RE   = re.compile(
        r"\x1b\[[0-9;]*[mABCDEFGHJKSTfnsuhl]"
        r"|\x1b\[[\?][0-9;]*[hl]"
        r"|\x1b=|\x1b>|\r"
    )
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"

    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
        live_terminals[OWNER_ID] = proc
    except Exception:
        await status_msg.edit_text(f"{header}\n```\n{traceback.format_exc()}\n```")
        return

    buf        = []
    last_edit  = time.time()
    last_text  = ""
    start_time = time.time()

    async def _flush(final=False):
        nonlocal last_edit, last_text
        output = "".join(buf)
        output = ANSI_RE.sub("", output)
        if len(output) > MAX_CHARS:
            output = "…(truncated)\n" + output[-MAX_CHARS:]
        suffix   = "" if final else "\n▌"
        new_text = f"{header}\n```\n{output}{suffix}\n```"
        if new_text != last_text:
            try:
                await status_msg.edit_text(new_text)
                last_text = new_text
            except Exception:
                pass
        last_edit = time.time()

    while True:
        if time.time() - start_time > timeout:
            buf.append(f"\n⏱ Timeout after {timeout}s — process killed.")
            try:
                proc.kill()
            except Exception:
                pass
            break
        try:
            chunk = await asyncio.wait_for(proc.stdout.read(512), timeout=0.3)
        except asyncio.TimeoutError:
            chunk = b""
        if chunk:
            buf.append(chunk.decode(errors="replace"))
        if time.time() - last_edit >= INTERVAL:
            await _flush()
        if not chunk and proc.stdout.at_eof():
            break
        if not chunk and proc.returncode is not None:
            try:
                remaining = await asyncio.wait_for(proc.stdout.read(), timeout=1)
                if remaining:
                    buf.append(remaining.decode(errors="replace"))
            except Exception:
                pass
            break

    live_terminals.pop(OWNER_ID, None)
    await proc.wait()
    await _flush(final=True)

    full_output = ANSI_RE.sub("", "".join(buf)).strip()
    if len(full_output) > MAX_CHARS:
        path = f"/tmp/out_{int(time.time())}.txt"
        with open(path, "w", encoding="utf-8") as f:
            f.write(full_output)
        await status_msg.reply_document(path, caption=f"{header}\n_(full output — file)_")
        os.remove(path)

async def _reply(m: Message, status_msg, header: str, output: str):
    body = f"{header}\n```\n{output}\n```"
    if len(body) > 4000:
        path = f"/tmp/out_{int(time.time())}.txt"
        with open(path, "w", encoding="utf-8") as f:
            f.write(output)
        await status_msg.delete()
        await m.reply_document(path, caption=f"{header}\n_(output too long)_")
        os.remove(path)
    else:
        await status_msg.edit_text(body)

async def _t_reply(m: Message, text: str, **kw) -> Message:
    """Send reply + track for auto-cleanup on next command."""
    sent = await m.reply_text(text, **kw)
    last_bot_msg[m.from_user.id] = sent
    return sent

async def _ls_fmt(path_str: str) -> str:
    """Return formatted ls output string for a given path."""
    raw = await _shell(f"ls -lah '{path_str}'")
    lines_out = [f"📂  {path_str}", "─" * 32]
    folders, files = [], []
    for line in raw.splitlines():
        parts = line.split(None, 8)
        if len(parts) < 9 or line.startswith("total"):
            continue
        perms, _, _, _, size, mon, day, yr_time, name = parts
        if name in (".", ".."):
            continue
        size_fmt = size
        try:
            sz = int(size)
            if sz >= 1_073_741_824:   size_fmt = f"{sz/1_073_741_824:.1f}GB"
            elif sz >= 1_048_576:     size_fmt = f"{sz/1_048_576:.1f}MB"
            elif sz >= 1024:          size_fmt = f"{sz/1024:.1f}KB"
            else:                     size_fmt = f"{sz}B"
        except ValueError:
            pass
        ts = f"{mon} {day} {yr_time}"
        if perms.startswith("d"):
            folders.append(f"📁  {name:<22} {ts}")
        else:
            files.append(f"📄  {name:<18} {size_fmt:>6}  {ts}")
    lines_out += folders + files
    if len(lines_out) == 2:
        lines_out.append("(empty)")
    return "\n".join(lines_out)

def _uptime(sec: int) -> str:
    h, rem = divmod(sec, 3600)
    m, s   = divmod(rem, 60)
    return f"{h}h {m}m {s}s" if h else f"{m}m {s}s"

def _find_req(base_path: str) -> "Path | None":
    for p in [Path(base_path), Path(base_path).parent]:
        req = p / "requirements.txt" if p.is_dir() else p
        if req.name == "requirements.txt" and req.exists():
            return req
    return None


# ══════════════════════════════════════════════════════════════════════════════
# 🎯  INTERACTIVE FOREGROUND RUNNER  (_run_interactive)
#
#  What it does:
#   ▸ Runs a .py script as a live interactive session (foreground only, 1 at a time)
#   ▸ Streams all output into ONE Telegram message, edited every 1.5 s
#   ▸ Parses bare \r (carriage return) — rewrites the current line in-place
#     so yt-dlp / tqdm progress bars work correctly instead of spamming lines
#   ▸ Accepts stdin from handle_terminal_input (any text the owner sends)
#   ▸ Magic Interceptor — if the script prints a trigger line, the bot
#     removes it from the visible output and fires the matching Pyrogram action:
#       TG_UPLOAD: path/to/file.ext  → reply_document  (with live % progress)
#       TG_PHOTO:  path/to/img.jpg   → reply_photo
#       TG_ALBUM:  a.jpg,b.jpg,c.png → reply_media_group (InputMediaPhoto list)
# ══════════════════════════════════════════════════════════════════════════════

async def _run_interactive(m: Message, script: str, args: str = "", timeout: int = 3600):
    uid       = m.from_user.id
    INTERVAL  = 1.5          # Telegram edit throttle (seconds)
    MAX_CHARS = 3500          # max visible chars in the code-block
    ANSI_RE   = re.compile(
        r"\x1b\[[0-9;]*[mABCDEFGHJKSTfnsuhl]"
        r"|\x1b\[[\?][0-9;]*[hl]"
        r"|\x1b=|\x1b>"
    )
    TRIGGER_PREFIXES = ("TG_UPLOAD:", "TG_PHOTO:", "TG_ALBUM:")

    # ── Guard: only one foreground process at a time ──────────────────────────
    existing = foreground_process.get(uid)
    if existing and existing["proc"].returncode is None:
        return await m.reply_text(
            "```\n"
            "⚠️  A foreground process is already running.\n"
            f"Script  :  {existing.get('script', '?')}\n"
            f"PID     :  {existing['proc'].pid}\n"
            "──────────────────────────────────────\n"
            "Use /exit to kill it, then /run again.\n"
            "```"
        )

    if not os.path.exists(script):
        return await m.reply_text(f"❌ File not found: `{script}`")

    header = f"▶ /run {script}"
    stat   = await m.reply_text(f"```\n{header}\n\n▌\n```")
    last_bot_msg[uid] = stat

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"

    cmd = f"{sys.executable} -u {script} {args}".strip()
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
    except Exception:
        return await stat.edit_text(
            f"```\n{header}\n\n{traceback.format_exc()[:2000]}\n```"
        )

    foreground_process[uid] = {
        "proc": proc, "msg": stat, "m": m, "script": script
    }

    # ── Output buffer: list of lines ──────────────────────────────────────────
    # lines_buf[-1] is always the "current line being written to".
    # A bare \r resets it to "" (progress-bar overwrite behaviour).
    lines_buf    = [""]
    upload_label = {"text": ""}   # shown at the bottom of the terminal block
    last_edit    = [time.time()]
    last_text    = [""]
    pending_trig = []             # trigger strings ready to dispatch

    def _render() -> str:
        raw     = "\n".join(lines_buf)
        cleaned = ANSI_RE.sub("", raw)
        if upload_label["text"]:
            cleaned += f"\n{upload_label['text']}"
        if len(cleaned) > MAX_CHARS:
            cleaned = "…(truncated)\n" + cleaned[-MAX_CHARS:]
        return cleaned

    async def _flush(final: bool = False):
        output   = _render()
        cursor   = "" if final else "\n▌"
        new_text = f"```\n{header}\n\n{output}{cursor}\n```"
        if new_text != last_text[0]:
            try:
                await stat.edit_text(new_text)
                last_text[0] = new_text
            except Exception:
                pass
        last_edit[0] = time.time()

    def _ingest(raw: bytes):
        """
        Parse raw bytes into lines_buf, handling all three newline flavours:
          \\r\\n  (Windows CRLF) — treat as \\n
          \\r     (bare CR)      — go to start of current line (progress bar)
          \\n     (LF)           — new line; completed line checked for triggers
        """
        text = raw.decode(errors="replace")
        i = 0
        while i < len(text):
            ch = text[i]
            if ch == "\r" and i + 1 < len(text) and text[i + 1] == "\n":
                # CRLF — treat as single LF
                _finish_line()
                i += 2
            elif ch == "\r":
                # Bare CR — overwrite current line (progress bar style)
                lines_buf[-1] = ""
                i += 1
            elif ch == "\n":
                _finish_line()
                i += 1
            else:
                lines_buf[-1] += ch
                i += 1

    def _finish_line():
        """Called when \\n (or CRLF) is encountered. Checks for trigger strings."""
        completed = lines_buf[-1]
        if any(completed.strip().startswith(p) for p in TRIGGER_PREFIXES):
            # Queue for async dispatch; erase from visible output
            pending_trig.append(completed.strip())
            lines_buf[-1] = ""
        else:
            lines_buf.append("")

    # ── Magic Interceptor — Pyrogram bridge actions ───────────────────────────
    async def _dispatch_trigger(line: str):
        """Execute the Pyrogram action matching a trigger string."""

        # ── TG_UPLOAD: path/to/file.ext ───────────────────────────────────────
        if line.startswith("TG_UPLOAD:"):
            fname = line[len("TG_UPLOAD:"):].strip()
            bname = os.path.basename(fname)
            if not os.path.exists(fname):
                upload_label["text"] = f"⚠️ TG_UPLOAD: file not found → {fname}"
                await _flush()
                await asyncio.sleep(3)
                upload_label["text"] = ""
                return

            last_prog = [0.0]

            async def _prog(current, total):
                pct = int(current * 100 / total) if total else 0
                now = time.time()
                if now - last_prog[0] >= INTERVAL:
                    last_prog[0] = now
                    kb_now   = current // 1024
                    kb_total = total   // 1024
                    upload_label["text"] = (
                        f"📤 Uploading {bname}  {pct}%  ({kb_now}KB / {kb_total}KB)"
                    )
                    await _flush()

            upload_label["text"] = f"📤 Starting upload: {bname}"
            await _flush()
            try:
                await m.reply_document(fname, progress=_prog)
                upload_label["text"] = f"✅ Uploaded: {bname}"
            except Exception as e:
                upload_label["text"] = f"❌ Upload failed ({bname}): {e}"
            await _flush()
            await asyncio.sleep(3)
            upload_label["text"] = ""

        # ── TG_PHOTO: path/to/image.jpg ───────────────────────────────────────
        elif line.startswith("TG_PHOTO:"):
            fname = line[len("TG_PHOTO:"):].strip()
            bname = os.path.basename(fname)
            upload_label["text"] = f"🖼 Sending photo: {bname}"
            await _flush()
            try:
                await m.reply_photo(fname)
                upload_label["text"] = f"✅ Photo sent: {bname}"
            except Exception as e:
                upload_label["text"] = f"❌ Photo failed ({bname}): {e}"
            await _flush()
            await asyncio.sleep(3)
            upload_label["text"] = ""

        # ── TG_ALBUM: a.jpg,b.jpg,c.png ──────────────────────────────────────
        elif line.startswith("TG_ALBUM:"):
            raw_names = line[len("TG_ALBUM:"):].strip()
            fnames    = [f.strip() for f in raw_names.split(",") if f.strip()]
            valid     = [f for f in fnames if os.path.exists(f)]
            upload_label["text"] = (
                f"🖼 Sending album: {len(valid)}/{len(fnames)} photos"
            )
            await _flush()
            try:
                if not valid:
                    raise FileNotFoundError("No valid files in album list")
                media = [InputMediaPhoto(f) for f in valid]
                await m.reply_media_group(media=media)
                upload_label["text"] = f"✅ Album sent: {len(valid)} photos"
            except Exception as e:
                upload_label["text"] = f"❌ Album failed: {e}"
            await _flush()
            await asyncio.sleep(3)
            upload_label["text"] = ""

    # ── Main output-reading loop ──────────────────────────────────────────────
    start_time = time.time()

    while True:
        # Hard timeout guard
        if time.time() - start_time > timeout:
            lines_buf.append(f"\n⏱ Timeout after {timeout}s — process killed.")
            try:
                proc.kill()
            except Exception:
                pass
            break

        try:
            chunk = await asyncio.wait_for(proc.stdout.read(512), timeout=0.3)
        except asyncio.TimeoutError:
            chunk = b""

        if chunk:
            _ingest(chunk)
            # Fire any completed trigger lines as background tasks
            for trig in pending_trig:
                asyncio.create_task(_dispatch_trigger(trig))
            pending_trig.clear()

        if time.time() - last_edit[0] >= INTERVAL:
            await _flush()

        if not chunk and proc.stdout.at_eof():
            break
        if not chunk and proc.returncode is not None:
            # Drain remaining bytes
            try:
                tail = await asyncio.wait_for(proc.stdout.read(), timeout=1)
                if tail:
                    _ingest(tail)
            except Exception:
                pass
            break

    # ── Cleanup ───────────────────────────────────────────────────────────────
    foreground_process.pop(uid, None)
    await proc.wait()
    rc = proc.returncode

    exit_line = "\n[✅ Done — exit 0]" if rc == 0 else f"\n[💥 Exit code {rc}]"
    lines_buf.append(exit_line)
    await _flush(final=True)

    # If the full transcript is very long, also attach it as a file
    full_output = ANSI_RE.sub("", "\n".join(lines_buf)).strip()
    if len(full_output) > MAX_CHARS:
        path = f"/tmp/fg_{int(time.time())}.txt"
        with open(path, "w", encoding="utf-8") as f:
            f.write(full_output)
        await stat.reply_document(path, caption=f"📄 Full output: `{script}`")
        os.remove(path)


# ══════════════════════════════════════════════════════════════════════════════
#   COMMAND HANDLERS
# ══════════════════════════════════════════════════════════════════════════════

# ── /start ────────────────────────────────────────────────────────────────────
@app.on_message(filters.command("start"))
async def cmd_start(_, m: Message):
    await m.reply_text(
        "```\n"
        "┌──────────────────────────────────────────┐\n"
        "│   ⚡  TERMINAL BOT  —  Ayuprime  v6      │\n"
        "└──────────────────────────────────────────┘\n"
        "\n"
        "SHELL\n"
        "  /sh  <cmd>               run any command (live)\n"
        "\n"
        "INTERACTIVE FOREGROUND  (/run)\n"
        "  /run  <file.py> [args]   run script — live output, interactive\n"
        "                             → type anything to send as stdin\n"
        "                             → script can trigger magic bridges:\n"
        "                               TG_UPLOAD: file.ext\n"
        "                               TG_PHOTO:  image.jpg\n"
        "                               TG_ALBUM:  a.jpg,b.jpg\n"
        "  /exit                    kill the active foreground script\n"
        "\n"
        "BACKGROUND SCRIPTS  (/deploy)\n"
        "  /deploy <tag> <file>     start script silently in background\n"
        "                           (auto pip install if req.txt found)\n"
        "  /stop  <tag>             kill a background script\n"
        "  /stopall                 kill all background scripts\n"
        "  /ps                      list running scripts\n"
        "  /log   <tag> [lines]     tail output log\n"
        "  /runreq [path]           pip install requirements.txt\n"
        "\n"
        "FILE MANAGER\n"
        "  /ls   [path]             list directory (pretty)\n"
        "  /cd   <path>             set upload directory\n"
        "  /mkdir <path>            create directory\n"
        "  /rm    <path>            delete file/folder\n"
        "  /upload <path>           send file to you\n"
        "  /mv    <src> <dst>       move/rename file\n"
        "  /cp    <src> <dst>       copy file\n"
        "  send any file  →  saves to current dir\n"
        "\n"
        "FILE TOOLS\n"
        "  /nano  <file> <text>    append line to file\n"
        "  /write <file> <text>    overwrite file with text\n"
        "  /clear <file>           wipe file contents\n"
        "  /cat   <file>           read file inline\n"
        "  /find  <name>           search files by name\n"
        "  /grep  <pattern> <path> search text in files\n"
        "  /py    <code>           run Python snippet\n"
        "  /zip   <folder>         zip & send\n"
        "  /unzip <file>           unzip archive\n"
        "\n"
        "SYSTEM\n"
        "  /sys  /ping  /env  /setenv KEY VAL\n"
        "  /restart  /kill <pid>  /top\n"
        "\n"
        "ACCESS CONTROL  (owner only)\n"
        "  /auth     <user_id>   — grant bot access to a user\n"
        "  /unauth   <user_id>   — revoke access\n"
        "  /authlist             — list all authorized users\n"
        "\n"
        "PHOENIX\n"
        "  /backup              — status + restore info\n"
        "  /backup files        — list backed up files\n"
        "  /backup modules      — list modules (backupmodules.txt)\n"
        "  /backup now          — force backup scripts/ dir\n"
        "  /addmodules <pkg..>  — add to modules list + vault\n"
        "  /removemodules <pkg> — remove from modules list\n"
        "  /backuplist          — list files in Rclone Vault\n"
        "  /vaultdelete <path>  — delete a file from Rclone Vault\n"
        "  /setmaxuptime <hrs>  — set uptime alert threshold\n"
        "```"
    )

# ── /sh — live shell ──────────────────────────────────────────────────────────
# ── Auto-delete previous bot reply when user sends a new command ─────────────
@app.on_message(allowed_filter & filters.text, group=-1)
async def _pre_cleanup(_, m: Message):
    uid = m.from_user.id
    old = last_bot_msg.pop(uid, None)
    if old:
        try:
            await old.delete()
        except Exception:
            pass

@app.on_message(filters.command(["sh", "bash", "termux"]) & allowed_filter)
async def cmd_sh(_, m: Message):
    if len(m.command) < 2:
        return await m.reply_text("Usage: `/sh <command>`")
    cmd  = m.text.split(maxsplit=1)[1]
    uid  = m.from_user.id
    cwd  = user_cwd.get(uid, ".")
    stat = await m.reply_text(f"```\n$ {cmd[:100]}\n▌\n```")
    last_bot_msg[uid] = stat
    if "pip install" in cmd or "pip3 install" in cmd:
        asyncio.create_task(_track_pip_install(cmd))
    await _shell_live(f"cd '{cwd}' && {cmd}", stat, f"$ {cmd[:80]}")

# ── Live stdin interceptor ─────────────────────────────────────────────────────
#
#  Priority order:
#   1. foreground_process  (active /run script)
#   2. live_terminals      (active /sh session)
#
@app.on_message(filters.text & allowed_filter & ~filters.regex(r"^/"))
async def handle_terminal_input(_, m: Message):
    uid = m.from_user.id

    # ── Priority 1: foreground /run process ───────────────────────────────────
    fg = foreground_process.get(uid)
    if fg and fg["proc"].returncode is None:
        proc = fg["proc"]
        if proc.stdin:
            try:
                proc.stdin.write((m.text + "\n").encode())
                await proc.stdin.drain()
                ack = await m.reply_text("📥 Input sent to foreground process…")
                await asyncio.sleep(1.5)
                await ack.delete()
            except Exception as e:
                await m.reply_text(f"```\n⚠️ Stdin error: {e}\n```")
        return   # don't fall through to /sh handler

    # ── Priority 2: /sh live terminal ─────────────────────────────────────────
    proc = live_terminals.get(uid)
    if proc and proc.stdin:
        try:
            proc.stdin.write((m.text + "\n").encode())
            await proc.stdin.drain()
            ack = await m.reply_text("📥 Fed to terminal…")
            await asyncio.sleep(1.5)
            await ack.delete()
        except Exception as e:
            await m.reply_text(f"```\n⚠️ Stdin error: {e}\n```")

# ── /run — NEW interactive foreground script runner ───────────────────────────
@app.on_message(filters.command("run") & allowed_filter)
async def cmd_run(_, m: Message):
    parts = m.text.split(maxsplit=2)
    if len(parts) < 2:
        return await m.reply_text(
            "**Usage:** `/run <script.py> [args]`\n\n"
            "Runs the script as a live interactive session.\n"
            "Output streams into this message in real-time.\n"
            "Type any text → it becomes stdin for the script.\n"
            "Use `/exit` to kill it.\n\n"
            "**Magic bridges** (print from your script):\n"
            "`TG_UPLOAD: file.ext`   → uploads the file (with live % progress)\n"
            "`TG_PHOTO:  image.jpg`  → sends as photo\n"
            "`TG_ALBUM:  a.jpg,b.jpg`→ sends as photo album"
        )
    script = parts[1]
    args   = parts[2] if len(parts) > 2 else ""
    # Resolve relative path against user CWD
    cwd_r  = user_cwd.get(m.from_user.id, str(SCRIPTS_DIR))
    if not os.path.isabs(script):
        resolved = Path(cwd_r) / script
        if resolved.exists():
            script = str(resolved)
    await _run_interactive(m, script, args)

# ── /exit — kill active foreground process ────────────────────────────────────
@app.on_message(filters.command("exit") & allowed_filter)
async def cmd_exit(_, m: Message):
    uid = m.from_user.id
    fg  = foreground_process.get(uid)
    if not fg or fg["proc"].returncode is not None:
        return await m.reply_text(
            "```\nℹ️  No active foreground process.\nUse /run <script.py> to start one.\n```"
        )
    proc = fg["proc"]
    pid  = proc.pid
    try:
        proc.kill()
    except Exception:
        try:
            proc.terminate()
        except Exception:
            pass
    foreground_process.pop(uid, None)

    # Update the live terminal message to show killed state
    stat_msg = fg.get("msg")
    if stat_msg:
        try:
            await stat_msg.edit_text(
                f"```\n▶ /run {fg.get('script', '')}\n\n"
                f"🛑 [KILLED by /exit]  PID {pid}\n```"
            )
        except Exception:
            pass

    await m.reply_text(
        f"```\n"
        f"(\\__/)\n"
        f"(-•ᴗ•-)  🛑\n"
        f"  killed.\n\n"
        f"[EXIT]  PID {pid} terminated by /exit\n"
        f"```"
    )

# ── /deploy — background script runner (was /run) ─────────────────────────────
@app.on_message(filters.command("deploy") & allowed_filter)
async def cmd_deploy(_, m: Message):
    parts = m.text.split(maxsplit=3)
    if len(parts) < 3:
        return await m.reply_text("Usage: `/deploy <tag> <script_path> [args]`")
    tag, script = parts[1], parts[2]
    extra = parts[3] if len(parts) > 3 else ""
    # Resolve relative path against user CWD
    cwd_r = user_cwd.get(m.from_user.id, str(SCRIPTS_DIR))
    if not os.path.isabs(script):
        resolved = Path(cwd_r) / script
        if resolved.exists():
            script = str(resolved)

    if tag in running_processes:
        return await m.reply_text(
            f"⚠️ `{tag}` is already running.\nUse `/stop {tag}` first."
        )
    if not os.path.exists(script):
        return await m.reply_text(f"❌ File not found: `{script}`")

    # Auto pip install if requirements.txt found nearby
    req = Path(script).parent / "requirements.txt"
    if req.exists():
        stat = await m.reply_text(
            f"```\n[AUTO-INSTALL]  Found requirements.txt\n$ pip install -r {req}\n▌\n```"
        )
        await _shell_live(
            f"{sys.executable} -m pip install -r '{req}'",
            stat,
            f"$ pip install -r {req}",
            timeout=300,
        )

    log_path = str(LOGS_DIR / f"{tag}.log")
    cmd      = f"{sys.executable} -u {script} {extra}".strip()
    log_fh   = open(log_path, "w", buffering=1, encoding="utf-8")
    proc     = await asyncio.create_subprocess_shell(
        cmd, stdout=log_fh, stderr=log_fh, preexec_fn=os.setsid
    )
    running_processes[tag] = {
        "process": proc, "cmd": cmd,
        "started": time.time(), "log": log_path, "log_fh": log_fh,
    }
    asyncio.create_task(_watch(tag, m))
    await m.reply_text(
        "```\n"
        "(\\__/)\n"
        "(>•ᴗ•)>  🖥️\n"
        "  deployed.\n\n"
        f"[DEPLOY]  {tag}\n"
        f"PID     :  {proc.pid}\n"
        f"Script  :  {script}\n"
        f"Log     :  /log {tag}\n"
        "```"
    )

async def _watch(tag: str, m: Message):
    """Notify when a background script exits."""
    entry = running_processes.get(tag)
    if not entry:
        return
    rc = await entry["process"].wait()
    if tag not in running_processes:
        return
    entry["log_fh"].close()
    running_processes.pop(tag, None)
    if rc == 0:
        banner = "(\\__/)\n(>•ᴗ•)> ✨\n  success!"
        status = "✅ exited cleanly"
    else:
        banner = "(\\__/)\n(>x_x)> 💀\n  failed!"
        status = f"💥 crashed (code {rc})"
    try:
        await m.reply_text(
            f"```\n{banner}\n\n"
            f"[EXITED]  {tag}\n"
            f"Status  :  {status}\n"
            f"Log     :  /log {tag}\n```"
        )
    except Exception:
        pass

# ── /stop ─────────────────────────────────────────────────────────────────────
@app.on_message(filters.command("stop") & allowed_filter)
async def cmd_stop(_, m: Message):
    if len(m.command) < 2:
        return await m.reply_text("Usage: `/stop <tag>`")
    tag   = m.command[1]
    entry = running_processes.get(tag)
    if not entry:
        return await m.reply_text(
            f"❌ No running process named `{tag}`.\nUse `/ps` to see active scripts."
        )
    pid = entry["process"].pid
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except Exception:
        try:
            entry["process"].terminate()
        except Exception:
            pass
    await asyncio.sleep(0.5)
    entry["log_fh"].close()
    running_processes.pop(tag, None)
    await m.reply_text(
        f"```\n(\\__/)\n(-•ᴗ•-)  💤\n  stopped.\n\n"
        f"[STOPPED]  {tag}\nPID {pid} terminated.\n```"
    )

# ── /stopall ──────────────────────────────────────────────────────────────────
@app.on_message(filters.command("stopall") & allowed_filter)
async def cmd_stopall(_, m: Message):
    if not running_processes:
        return await m.reply_text("ℹ️ No background scripts running.")
    lines = []
    for tag in list(running_processes.keys()):
        entry = running_processes.pop(tag)
        pid   = entry["process"].pid
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except Exception:
            try:
                entry["process"].terminate()
            except Exception:
                pass
        entry["log_fh"].close()
        lines.append(f"  • {tag}  (PID {pid})")
    await m.reply_text("```\n[STOPPED ALL]\n" + "\n".join(lines) + "\n```")

# ── /ps ───────────────────────────────────────────────────────────────────────
@app.on_message(filters.command("ps") & allowed_filter)
async def cmd_ps(_, m: Message):
    lines = []
    fg = foreground_process.get(m.from_user.id)
    if fg and fg["proc"].returncode is None:
        lines.append(
            f"[FOREGROUND]  {fg.get('script', '?')}  (PID {fg['proc'].pid})  — /exit to kill"
        )
        lines.append("")
    if not running_processes and not lines:
        return await m.reply_text("ℹ️ No scripts running.")
    if running_processes:
        lines.append("TAG              PID    UPTIME     COMMAND")
        lines.append("─" * 50)
        for tag, v in running_processes.items():
            up  = _uptime(int(time.time() - v["started"]))
            cmd = v["cmd"][:28] + ("…" if len(v["cmd"]) > 28 else "")
            lines.append(f"{tag:<16} {v['process'].pid:<6} {up:<10} {cmd}")
    await m.reply_text("```\n" + "\n".join(lines) + "\n```")

# ── /log ──────────────────────────────────────────────────────────────────────
@app.on_message(filters.command("log") & allowed_filter)
async def cmd_log(_, m: Message):
    if len(m.command) < 2:
        return await m.reply_text("Usage: `/log <tag> [lines=50]`")
    tag      = m.command[1]
    n        = int(m.command[2]) if len(m.command) > 2 else 50
    log_path = LOGS_DIR / f"{tag}.log"
    if not log_path.exists():
        if tag in running_processes:
            return await m.reply_text(
                f"```\n[LOG]  {tag}\nProcess running but no output yet.\n```"
            )
        return await m.reply_text(f"❌ No log found for `{tag}`.")
    stat = await m.reply_text(f"```\n[LOG]  {tag}  (last {n} lines)\nReading...\n```")
    out  = await _shell(f"tail -n {n} '{log_path}'")
    await _reply(m, stat, f"[LOG]  {tag}", out)

# ── /runreq ───────────────────────────────────────────────────────────────────
@app.on_message(filters.command("runreq") & allowed_filter)
async def cmd_runreq(_, m: Message):
    cwd = user_cwd.get(m.from_user.id, ".")
    if len(m.command) < 2:
        req = Path(cwd) / "requirements.txt"
    else:
        arg = m.command[1]
        p   = Path(arg) if Path(arg).is_absolute() else Path(cwd) / arg
        req = p if p.name == "requirements.txt" else p / "requirements.txt"
    if not req.exists():
        return await m.reply_text(
            f"❌ Not found: `{req}`\nUse `/cd <folder>` to go to the right directory first."
        )
    stat = await m.reply_text(f"```\n$ pip install -r {req}\n▌\n```")
    await _shell_live(
        f"{sys.executable} -m pip install -r '{req}'",
        stat,
        f"$ pip install -r {req}",
        timeout=300,
    )
    try:
        for line in Path(req).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                asyncio.create_task(_track_pip_install(f"pip install {line}"))
    except Exception:
        pass

# ── /cd ───────────────────────────────────────────────────────────────────────
@app.on_message(filters.command("cd") & allowed_filter)
async def cmd_cd(_, m: Message):
    uid = m.from_user.id
    if len(m.command) < 2:
        cwd = user_cwd.get(uid, str(SCRIPTS_DIR))
        return await m.reply_text(f"📂 Current dir: `{cwd}`")
    path = Path(m.text.split(maxsplit=1)[1])
    path.mkdir(parents=True, exist_ok=True)
    user_cwd[uid] = str(path)
    stat = await m.reply_text(f"```\n[CD]  → {path}\nListing...\n```")
    last_bot_msg[uid] = stat
    ls_out = await _ls_fmt(str(path))
    await _reply(m, stat, f"📂 {path}", ls_out)

# ── File receiver ─────────────────────────────────────────────────────────────
@app.on_message(filters.document & allowed_filter)
async def cmd_file(_, m: Message):
    fname = m.document.file_name
    cwd   = user_cwd.get(m.from_user.id, str(SCRIPTS_DIR))
    dest  = Path(cwd) / fname
    stat  = await m.reply_text(f"```\n[DOWNLOAD]  {fname}\nSaving to {cwd}/...\n```")
    await m.download(file_name=str(dest.absolute()))
    size      = dest.stat().st_size
    base_text = (
        f"```\n[SAVED]  {fname}\n"
        f"Path  :  {dest}\n"
        f"Size  :  {size:,} bytes\n"
        f"Run   :  /run {dest}\n```"
    )
    await stat.edit_text(base_text[:-3] + "\nBackup  :  ⏳ uploading...\n```")
    asyncio.create_task(_backup_file(str(dest), status_msg=stat, base_text=base_text))

# ── /ls ───────────────────────────────────────────────────────────────────────
@app.on_message(filters.command("ls") & allowed_filter)
async def cmd_ls(_, m: Message):
    uid  = m.from_user.id
    path = (
        m.text.split(maxsplit=1)[1] if len(m.command) > 1
        else user_cwd.get(uid, ".")
    )
    stat = await m.reply_text(f"```\n📂 {path}\nListing...\n```")
    last_bot_msg[uid] = stat
    ls_out = await _ls_fmt(path)
    await _reply(m, stat, f"📂 {path}", ls_out)

# ── /mkdir ────────────────────────────────────────────────────────────────────
@app.on_message(filters.command("mkdir") & allowed_filter)
async def cmd_mkdir(_, m: Message):
    if len(m.command) < 2:
        return await m.reply_text("Usage: `/mkdir <path>`")
    path = m.text.split(maxsplit=1)[1]
    os.makedirs(path, exist_ok=True)
    await _t_reply(m, f"```\n[CREATED]  {path}\n```")

# ── /rm ───────────────────────────────────────────────────────────────────────
@app.on_message(filters.command("rm") & allowed_filter)
async def cmd_rm(_, m: Message):
    if len(m.command) < 2:
        return await m.reply_text("Usage: `/rm <path>`")
    path = m.text.split(maxsplit=1)[1]
    stat = await m.reply_text(f"```\n$ rm -rf {path}\nDeleting...\n```")
    out  = await _shell(f"rm -rf '{path}' && echo 'Deleted.'")
    await _reply(m, stat, f"$ rm -rf {path}", out)

# ── /mv ───────────────────────────────────────────────────────────────────────
@app.on_message(filters.command("mv") & allowed_filter)
async def cmd_mv(_, m: Message):
    parts = m.text.split(maxsplit=2)
    if len(parts) < 3:
        return await m.reply_text("Usage: `/mv <source> <destination>`")
    src, dst = parts[1], parts[2]
    try:
        shutil.move(src, dst)
        await m.reply_text(f"```\n[MOVED]  {src}  →  {dst}\n```")
    except Exception as e:
        await m.reply_text(f"❌ Error: `{e}`")

# ── /cp ───────────────────────────────────────────────────────────────────────
@app.on_message(filters.command("cp") & allowed_filter)
async def cmd_cp(_, m: Message):
    parts = m.text.split(maxsplit=2)
    if len(parts) < 3:
        return await m.reply_text("Usage: `/cp <source> <destination>`")
    src, dst = parts[1], parts[2]
    try:
        if os.path.isdir(src):
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)
        await m.reply_text(f"```\n[COPIED]  {src}  →  {dst}\n```")
    except Exception as e:
        await m.reply_text(f"❌ Error: `{e}`")

# ── /upload ───────────────────────────────────────────────────────────────────
@app.on_message(filters.command("upload") & allowed_filter)
async def cmd_upload(_, m: Message):
    if len(m.command) < 2:
        return await m.reply_text("Usage: `/upload <path>`")
    path = m.text.split(maxsplit=1)[1]
    if not os.path.exists(path):
        return await m.reply_text(f"❌ Not found: `{path}`")
    stat = await m.reply_text(f"```\n[UPLOAD]  {path}\nSending...\n```")
    await m.reply_document(path)
    await stat.delete()

# ── /nano ─────────────────────────────────────────────────────────────────────
@app.on_message(filters.command("nano") & allowed_filter)
async def cmd_nano(_, m: Message):
    parts = m.text.split(maxsplit=2)
    if len(parts) < 3:
        return await m.reply_text("Usage: `/nano <filename> <text>`\nAppends text as a new line.")
    fname = parts[1]
    text  = parts[2]
    cwd   = user_cwd.get(m.from_user.id, ".")
    fpath = Path(cwd) / fname
    fpath.parent.mkdir(parents=True, exist_ok=True)
    with open(fpath, "a", encoding="utf-8") as f:
        f.write(text + "\n")
    lines = len(fpath.read_text(encoding="utf-8").splitlines())
    if RCLONE_REMOTE:
        asyncio.create_task(_backup_file(str(fpath)))
    await m.reply_text(f"```\n[NANO]  {fpath}\nAppended line {lines}: {text[:60]}\n```")

# ── /write ────────────────────────────────────────────────────────────────────
@app.on_message(filters.command("write") & allowed_filter)
async def cmd_write(_, m: Message):
    parts = m.text.split(maxsplit=2)
    if len(parts) < 3:
        return await m.reply_text("Usage: `/write <filename> <text>`\nOverwrites file with text.")
    fname = parts[1]
    text  = parts[2]
    cwd   = user_cwd.get(m.from_user.id, ".")
    fpath = Path(cwd) / fname
    fpath.parent.mkdir(parents=True, exist_ok=True)
    with open(fpath, "w", encoding="utf-8") as f:
        f.write(text + "\n")
    await m.reply_text(f"```\n[WRITE]  {fpath}\nWritten: {text[:80]}\n```")

# ── /clear ────────────────────────────────────────────────────────────────────
@app.on_message(filters.command("clear") & allowed_filter)
async def cmd_clear(_, m: Message):
    if len(m.command) < 2:
        return await m.reply_text("Usage: `/clear <filename>`")
    cwd   = user_cwd.get(m.from_user.id, ".")
    fpath = Path(cwd) / m.command[1]
    if not fpath.exists():
        return await m.reply_text(f"❌ Not found: `{fpath}`")
    open(fpath, "w").close()
    await m.reply_text(f"```\n[CLEAR]  {fpath}\nFile wiped — 0 bytes.\n```")

# ── /cat ──────────────────────────────────────────────────────────────────────
@app.on_message(filters.command("cat") & allowed_filter)
async def cmd_cat(_, m: Message):
    if len(m.command) < 2:
        return await m.reply_text("Usage: `/cat <filename>`")
    cwd   = user_cwd.get(m.from_user.id, ".")
    fpath = Path(cwd) / m.command[1]
    if not fpath.exists():
        return await m.reply_text(f"❌ Not found: `{fpath}`")
    stat = await m.reply_text(f"```\n[CAT]  {fpath}\nReading...\n```")
    out  = fpath.read_text(encoding="utf-8", errors="replace")
    if not out.strip():
        return await stat.edit_text(f"```\n[CAT]  {fpath}\n(empty file)\n```")
    await _reply(m, stat, f"[CAT]  {fpath}", out)

# ── /find ─────────────────────────────────────────────────────────────────────
@app.on_message(filters.command("find") & allowed_filter)
async def cmd_find(_, m: Message):
    parts = m.text.split(maxsplit=2)
    if len(parts) < 2:
        return await m.reply_text("Usage: `/find <name> [search_path]`")
    name  = parts[1]
    spath = parts[2] if len(parts) > 2 else user_cwd.get(m.from_user.id, ".")
    stat  = await m.reply_text(f"```\n$ find {spath} -name '*{name}*'\nSearching...\n```")
    out   = await _shell(f"find '{spath}' -name '*{name}*' 2>/dev/null | head -50")
    await _reply(m, stat, f"$ find {spath} -name '*{name}*'", out or "No results found.")

# ── /grep ─────────────────────────────────────────────────────────────────────
@app.on_message(filters.command("grep") & allowed_filter)
async def cmd_grep(_, m: Message):
    parts = m.text.split(maxsplit=3)
    if len(parts) < 3:
        return await m.reply_text(
            "Usage: `/grep <pattern> <path> [flags]`\nExample: `/grep TODO . -r`"
        )
    pattern = parts[1]
    path    = parts[2]
    flags   = parts[3] if len(parts) > 3 else "-r --include='*.py'"
    stat    = await m.reply_text(f"```\n$ grep {flags} '{pattern}' {path}\nSearching...\n```")
    out     = await _shell(f"grep {flags} '{pattern}' '{path}' 2>/dev/null | head -50")
    await _reply(m, stat, f"$ grep '{pattern}' {path}", out or "No matches found.")

# ── /py ───────────────────────────────────────────────────────────────────────
@app.on_message(filters.command("py") & allowed_filter)
async def cmd_py(_, m: Message):
    if len(m.command) < 2:
        return await m.reply_text("Usage: `/py <python code>`\nExample: `/py print(2+2)`")
    code = m.text.split(maxsplit=1)[1]
    stat = await m.reply_text(f"```\n>>> {code[:80]}\nRunning...\n```")
    tmp  = f"/tmp/_py_{int(time.time())}.py"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(code)
    out = await _shell(f"{sys.executable} '{tmp}'", timeout=30)
    os.remove(tmp)
    await _reply(m, stat, f">>> {code[:60]}", out)

# ── /zip ──────────────────────────────────────────────────────────────────────
@app.on_message(filters.command("zip") & allowed_filter)
async def cmd_zip(_, m: Message):
    if len(m.command) < 2:
        return await m.reply_text("Usage: `/zip <folder_or_file>`")
    cwd    = user_cwd.get(m.from_user.id, ".")
    target = m.command[1]
    tpath  = Path(target) if Path(target).is_absolute() else Path(cwd) / target
    if not tpath.exists():
        return await m.reply_text(f"❌ Not found: `{tpath}`")
    out_zip = f"/tmp/{tpath.name}_{int(time.time())}.zip"
    stat    = await m.reply_text(f"```\n[ZIP]  {tpath}\nCompressing...\n```")
    result  = await _shell(
        f"cd '{tpath.parent}' && zip -r '{out_zip}' '{tpath.name}'", timeout=120
    )
    if not Path(out_zip).exists():
        return await _reply(m, stat, f"[ZIP]  {tpath}", result)
    size = Path(out_zip).stat().st_size
    await stat.edit_text(f"```\n[ZIP]  {tpath}\nSize: {size:,} bytes — sending...\n```")
    await m.reply_document(out_zip, caption=f"📦 `{tpath.name}.zip`")
    await stat.delete()
    os.remove(out_zip)

# ── /unzip ────────────────────────────────────────────────────────────────────
@app.on_message(filters.command("unzip") & allowed_filter)
async def cmd_unzip(_, m: Message):
    parts = m.text.split(maxsplit=2)
    if len(parts) < 2:
        return await m.reply_text("Usage: `/unzip <file.zip> [destination]`")
    cwd   = user_cwd.get(m.from_user.id, ".")
    src   = parts[1]
    dst   = parts[2] if len(parts) > 2 else cwd
    fpath = Path(src) if Path(src).is_absolute() else Path(cwd) / src
    if not fpath.exists():
        return await m.reply_text(f"❌ Not found: `{fpath}`")
    stat = await m.reply_text(f"```\n$ unzip {fpath} -d {dst}\nExtracting...\n```")
    out  = await _shell(f"unzip -o '{fpath}' -d '{dst}'", timeout=120)
    await _reply(m, stat, f"$ unzip {fpath}", out)

# ── /sys ──────────────────────────────────────────────────────────────────────
@app.on_message(filters.command("sys") & allowed_filter)
async def cmd_sys(_, m: Message):
    stat = await m.reply_text("```\nFetching system info...\n```")
    cmd  = (
        "echo '[ CPU ]' && grep 'model name' /proc/cpuinfo | head -1 | cut -d: -f2 | xargs && "
        "echo '[ RAM ]' && free -h && echo '[ DISK ]' && df -h / && "
        "echo '[ UPTIME ]' && uptime -p"
    )
    out  = await _shell(cmd)
    await _reply(m, stat, "System Info", out)

# ── /top ──────────────────────────────────────────────────────────────────────
@app.on_message(filters.command("top") & allowed_filter)
async def cmd_top(_, m: Message):
    stat = await m.reply_text("```\nFetching top processes...\n```")
    out  = await _shell("ps aux --sort=-%cpu | head -15")
    await _reply(m, stat, "Top Processes (by CPU)", out)

# ── /kill ─────────────────────────────────────────────────────────────────────
@app.on_message(filters.command("kill") & owner_filter)
async def cmd_kill(_, m: Message):
    if len(m.command) < 2:
        return await m.reply_text("Usage: `/kill <pid>`")
    pid = m.command[1]
    out = await _shell(f"kill -9 {pid} && echo 'Killed PID {pid}'")
    await m.reply_text(f"```\n{out}\n```")

# ── /ping ─────────────────────────────────────────────────────────────────────
@app.on_message(filters.command("ping") & allowed_filter)
async def cmd_ping(_, m: Message):
    t   = time.time()
    msg = await m.reply_text("```\nPinging...\n```")
    ms  = round((time.time() - t) * 1000)
    up  = _uptime(int(time.time() - BOT_START_TIME))
    fg  = foreground_process.get(m.from_user.id)
    fg_str = (
        f"\nFG process : {fg['script']} (PID {fg['proc'].pid})"
        if fg and fg["proc"].returncode is None
        else ""
    )
    await msg.edit_text(f"```\n🏓 Pong!  {ms}ms\nUptime: {up}{fg_str}\n```")

# ── /env ──────────────────────────────────────────────────────────────────────
@app.on_message(filters.command("env") & owner_filter)
async def cmd_env(_, m: Message):
    SECRET = {"BOT_TOKEN", "API_HASH", "PASSWORD", "SECRET", "KEY", "TOKEN"}
    lines  = [
        f"{k}={v[:4]+'••••' if any(s in k.upper() for s in SECRET) else v}"
        for k, v in sorted(os.environ.items())
    ]
    stat = await m.reply_text("```\nReading env...\n```")
    await _reply(m, stat, "Environment Variables", "\n".join(lines))

# ── /setenv ───────────────────────────────────────────────────────────────────
@app.on_message(filters.command("setenv") & owner_filter)
async def cmd_setenv(_, m: Message):
    parts = m.text.split(maxsplit=2)
    if len(parts) < 3:
        return await m.reply_text("Usage: `/setenv KEY VALUE`")
    os.environ[parts[1]] = parts[2]
    await m.reply_text(f"```\n[SET]  {parts[1]} = {parts[2][:40]}\n```")

# ── /restart ──────────────────────────────────────────────────────────────────
@app.on_message(filters.command("restart") & owner_filter)
async def cmd_restart(_, m: Message):
    await m.reply_text("```\n[RESTART]  Rebooting bot...\n```")
    await asyncio.sleep(1)
    os.execv(sys.executable, [sys.executable] + sys.argv)

# ── /backup ───────────────────────────────────────────────────────────────────
@app.on_message(filters.command("backup") & owner_filter)
async def cmd_backup(_, m: Message):
    sub = m.command[1].lower() if len(m.command) > 1 else ""
    db  = _get_db()
    if sub == "files":
        if db is None:
            return await m.reply_text("❌ MongoDB not configured.")
        lines = ["[BACKED UP FILES]", "─" * 40]
        async for doc in db.backups.find({}):
            lines.append(f"📄 {doc['file_name']}  →  {doc['original_folder_path']}")
        if len(lines) == 2:
            lines.append("(no files backed up yet)")
        await m.reply_text("```\n" + "\n".join(lines[:50]) + "\n```")
    elif sub == "modules":
        mods = _read_modules_local()
        text = "\n".join(f"  • {x}" for x in mods) or "  (none yet)"
        await m.reply_text(f"```\n[TRACKED MODULES]  ({len(mods)} total)\n{text}\n```")
    elif sub == "now":
        if not RCLONE_REMOTE:
            return await m.reply_text("❌ RCLONE_REMOTE not set.")
        stat  = await m.reply_text("```\n[BACKUP NOW]  Scanning scripts/ → Rclone Vault...\n```")
        count = 0
        for fpath in SCRIPTS_DIR.rglob("*"):
            if fpath.is_file():
                await _backup_file(str(fpath))
                count += 1
        await stat.edit_text(f"```\n[BACKUP NOW]  {count} file(s) sent to Rclone Vault.\n```")
    else:
        await _cmd_backup_status(m)

# ── /backuplist ───────────────────────────────────────────────────────────────
@app.on_message(filters.command("backuplist") & owner_filter)
async def cmd_backuplist(_, m: Message):
    stat = await m.reply_text(f"```\n[VAULT LIST]  {REMOTE_VAULT}\nFetching...\n```")
    out  = await _shell(f'./rclone ls "{REMOTE_VAULT}" --config rclone.conf', timeout=60)
    await _reply(m, stat, f"[VAULT LIST]  {REMOTE_VAULT}", out)

# ── /vaultdelete ──────────────────────────────────────────────────────────────
@app.on_message(filters.command("vaultdelete") & owner_filter)
async def cmd_vaultdelete(_, m: Message):
    if len(m.command) < 2:
        return await m.reply_text(
            "Usage: `/vaultdelete <remote_filepath>`\n"
            "Example: `/vaultdelete scripts/red.py`\n"
            "Tip: Use /backuplist to see exact paths."
        )
    remote_path = m.text.split(maxsplit=1)[1].strip()
    full_remote = f"{REMOTE_VAULT}/{remote_path}"
    stat = await m.reply_text(
        f"```\n[VAULT DELETE]\nTarget : {full_remote}\nDeleting...\n```"
    )
    out = await _shell(
        f'./rclone deletefile "{full_remote}" --config rclone.conf', timeout=60
    )
    db = _get_db()
    if db:
        fname = Path(remote_path).name
        await db.backups.delete_one({"file_name": fname})
    if "error" in out.lower() or "failed" in out.lower():
        await stat.edit_text(f"```\n[VAULT DELETE]  ❌ Failed\n{out[:300]}\n```")
    else:
        await stat.edit_text(
            f"```\n[VAULT DELETE]  ✅ Done\n"
            f"Removed : {full_remote}\n"
            f"MongoDB : record purged\n```"
        )

# ── /addmodules ───────────────────────────────────────────────────────────────
@app.on_message(filters.command("addmodules") & owner_filter)
async def cmd_addmodules(_, m: Message):
    if len(m.command) < 2:
        return await m.reply_text(
            "Usage: `/addmodules pkg1 pkg2 ...`\n"
            "Adds to `backupmodules.txt` and syncs to vault.\n"
            "To also install: `/sh pip install pkg1 pkg2`"
        )
    new_pkgs = [p.lower().strip() for p in m.command[1:] if p.strip()]
    existing = _read_modules_local()
    merged   = sorted(set(existing + new_pkgs))
    added    = sorted(set(merged) - set(existing))
    _write_modules_local(merged)
    stat = await m.reply_text(f"```\n[ADD MODULES]  Syncing to vault...\n▌\n```")
    await _sync_modules_to_vault()
    result = "\n".join(f"  + {p}" for p in added) or "  (all already present)"
    await stat.edit_text(
        f"```\n[ADD MODULES]\n──────────────────────────\n"
        f"{result}\n──────────────────────────\n"
        f"Total in list : {len(merged)}\n"
        f"Vault file    : {REMOTE_VAULT}/{MODULES_FILE}\n```"
    )

# ── /removemodules ────────────────────────────────────────────────────────────
@app.on_message(filters.command("removemodules") & owner_filter)
async def cmd_removemodules(_, m: Message):
    if len(m.command) < 2:
        return await m.reply_text("Usage: `/removemodules pkg1 pkg2 ...`")
    rm_pkgs   = [p.lower().strip() for p in m.command[1:] if p.strip()]
    existing  = _read_modules_local()
    merged    = sorted(set(existing) - set(rm_pkgs))
    removed   = sorted(set(existing) & set(rm_pkgs))
    not_found = sorted(set(rm_pkgs) - set(existing))
    _write_modules_local(merged)
    stat = await m.reply_text(f"```\n[REMOVE MODULES]  Syncing to vault...\n▌\n```")
    await _sync_modules_to_vault()
    lines  = [f"  - {p}" for p in removed] + [f"  ? {p}  (not found)" for p in not_found]
    result = "\n".join(lines) or "  (nothing changed)"
    await stat.edit_text(
        f"```\n[REMOVE MODULES]\n──────────────────────────\n"
        f"{result}\n──────────────────────────\n"
        f"Remaining : {len(merged)}\n```"
    )

# ── /setmaxuptime ─────────────────────────────────────────────────────────────
@app.on_message(filters.command("setmaxuptime") & owner_filter)
async def cmd_setmaxuptime(_, m: Message):
    await _cmd_setmaxuptime(m)

# ── Unauthorized users ─────────────────────────────────────────────────────────
# ── /auth — authorize a user (owner only) ────────────────────────────────────
@app.on_message(filters.command("auth") & owner_filter)
async def cmd_auth(_, m: Message):
    if len(m.command) < 2:
        return await m.reply_text(
            "Usage: `/auth <user_id>`\n"
            "Grants the user access to all bot commands.\n"
            "Example: `/auth 123456789`"
        )
    try:
        uid = int(m.command[1])
    except ValueError:
        return await m.reply_text("❌ User ID must be a number. Example: `/auth 123456789`")

    if uid == OWNER_ID:
        return await m.reply_text("ℹ️ Owner is always authorized.")

    authorized_users.add(uid)
    await _save_auth_users()

    # Try to get username/name for display
    try:
        user = await app.get_users(uid)
        label = f"@{user.username}" if user.username else user.first_name
    except Exception:
        label = str(uid)

    await m.reply_text(
        f"```\n"
        f"[AUTH]  ✅ Authorized\n"
        f"──────────────────────────\n"
        f"User   : {label}\n"
        f"ID     : {uid}\n"
        f"Total  : {len(authorized_users)} authorized user(s)\n"
        f"──────────────────────────\n"
        f"They can now use all bot commands.\n"
        f"Use /unauth {uid} to revoke.\n"
        f"```"
    )

# ── /unauth — revoke a user (owner only) ──────────────────────────────────────
@app.on_message(filters.command("unauth") & owner_filter)
async def cmd_unauth(_, m: Message):
    if len(m.command) < 2:
        return await m.reply_text(
            "Usage: `/unauth <user_id>`\n"
            "Revokes bot access from the user.\n"
            "Example: `/unauth 123456789`"
        )
    try:
        uid = int(m.command[1])
    except ValueError:
        return await m.reply_text("❌ User ID must be a number.")

    if uid == OWNER_ID:
        return await m.reply_text("❌ Cannot revoke owner access.")

    if uid not in authorized_users:
        return await m.reply_text(f"ℹ️ User `{uid}` was not authorized.")

    authorized_users.discard(uid)
    await _save_auth_users()

    try:
        user = await app.get_users(uid)
        label = f"@{user.username}" if user.username else user.first_name
    except Exception:
        label = str(uid)

    await m.reply_text(
        f"```\n"
        f"[UNAUTH]  🚫 Access Revoked\n"
        f"──────────────────────────\n"
        f"User   : {label}\n"
        f"ID     : {uid}\n"
        f"Total  : {len(authorized_users)} authorized user(s) remaining\n"
        f"```"
    )

# ── /authlist — show all authorized users (owner only) ───────────────────────
@app.on_message(filters.command("authlist") & owner_filter)
async def cmd_authlist(_, m: Message):
    if not authorized_users:
        return await m.reply_text(
            "```\n[AUTH LIST]  No authorized users.\nUse /auth <user_id> to add one.\n```"
        )
    lines = [f"[AUTH LIST]  {len(authorized_users)} user(s)", "─" * 36]
    for uid in sorted(authorized_users):
        try:
            user = await app.get_users(uid)
            label = f"@{user.username}" if user.username else user.first_name
            name  = f"  {label:<20} ({uid})"
        except Exception:
            name = f"  {uid}"
        lines.append(name)
    lines.append("─" * 36)
    lines.append("Use /unauth <id> to revoke access.")
    await m.reply_text("```\n" + "\n".join(lines) + "\n```")

# ── Unauthorized users ─────────────────────────────────────────────────────────
@app.on_message(filters.private & ~allowed_filter)
async def cmd_unauthorized(_, m: Message):
    await m.reply_text(
        "```\n[ACCESS DENIED]\nYou are not authorized to use this bot.\n```"
    )

# ── Main ──────────────────────────────────────────────────────────────────────
async def main():
    await start_web_server()
    await _init_mongo()
    await app.start()
    me = await app.get_me()
    print(f"⚡ @{me.username} is online | Owner: {OWNER_ID}")
    await _record_boot()
    await _load_auth_users()
    await _setup_rclone()
    await _phoenix_restore()
    asyncio.create_task(_uptime_alert_task())
    asyncio.create_task(_auto_restart_task())
    await idle()
    await app.stop()

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
