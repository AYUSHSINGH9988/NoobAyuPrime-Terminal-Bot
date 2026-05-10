import os
import re
import sys
import asyncio
import traceback
import signal
import time
import shutil
from pathlib import Path
from pyrogram import Client, filters, idle
from pyrogram.types import Message
from aiohttp import web

# ══════════════════════════════════════════════════════════════
#   ⚡ ADVANCED TERMINAL BOT  — Ayuprime 
# ══════════════════════════════════════════════════════════════

API_ID    = int(os.environ.get("API_ID", "33675350"))
API_HASH  = os.environ.get("API_HASH", "2f97c845b067a750c9f36fec497acf97")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8751265425:AAFJ5pzO0fgU80tRSCN5DplRCwna4Euw9Lg")
OWNER_ID  = int(os.environ.get("OWNER_ID", "8493596199"))

SCRIPTS_DIR = Path("./scripts")
LOGS_DIR    = Path("./logs")
for d in [SCRIPTS_DIR, LOGS_DIR, Path("./venvs")]:
    d.mkdir(exist_ok=True)

running_processes: dict = {}
user_cwd: dict = {}          # per-user upload directory
BOT_START_TIME = time.time()

app = Client(
    "AdvTerminalBot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    in_memory=True,
)

# ── Web server ────────────────────────────────────────────────────────────────
async def _health(request):
    procs = {tag: {"cmd": v["cmd"], "uptime_sec": int(time.time() - v["started"])}
             for tag, v in running_processes.items()}
    return web.json_response({
        "status": "online", "bot": "AdvancedTerminalBot",
        "owner": OWNER_ID, "running_scripts": procs,
        "uptime_sec": int(time.time() - BOT_START_TIME),
    })

async def _metrics(request):
    return web.Response(text=await _shell("df -h / && free -h && uptime"),
                        content_type="text/plain")

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

# ── Helpers ───────────────────────────────────────────────────────────────────
async def _shell(cmd: str, timeout: int = 60) -> str:
    """Run command, return full output (non-streaming)."""
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        result = (out.decode(errors="replace") + err.decode(errors="replace")).strip()
        return result or "✅ Done (no output)"
    except asyncio.TimeoutError:
        return f"⏱ Timeout after {timeout}s"
    except Exception:
        return traceback.format_exc()

async def _shell_live(cmd: str, status_msg, header: str, timeout: int = 300):
    """
    Live terminal: streams output into one Telegram message, updated every 1.5s.
    Fixes:
      1. ANSI codes stripped — Telegram markdown won't break on color escapes.
      2. PYTHONUNBUFFERED=1 — no buffering, output flows instantly.
      3. Manual timeout tracking — no asyncio.timeout() which caused stuck loops.
    """
    INTERVAL  = 1.5
    MAX_CHARS = 3500
    ANSI_RE   = re.compile(r"\x1b\[[0-9;]*[mABCDEFGHJKSTfnsuhl]|\x1b\[[\?][0-9;]*[hl]|\x1b=|\x1b>|\r")

    # FIX 2: Force unbuffered output so pip/python stream instantly
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"

    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,  # merge stderr into stdout
            env=env,
        )
    except Exception:
        await status_msg.edit_text(f"{header}\n```\n{traceback.format_exc()}\n```")
        return

    buf        = []
    last_edit  = time.time()
    last_text  = ""
    start_time = time.time()          # FIX 3: manual timeout tracking

    async def _flush(final=False):
        nonlocal last_edit, last_text
        output = "".join(buf)
        # FIX 1: Strip all ANSI escape codes before sending to Telegram
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

    # FIX 3: Plain while loop — manual time check, no asyncio.timeout()
    while True:
        # Timeout check
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

        # Exit when process is done and no more data
        if not chunk and proc.stdout.at_eof():
            break
        if not chunk and proc.returncode is not None:
            # Drain any last bytes
            try:
                remaining = await asyncio.wait_for(proc.stdout.read(), timeout=1)
                if remaining:
                    buf.append(remaining.decode(errors="replace"))
            except Exception:
                pass
            break

    await proc.wait()
    await _flush(final=True)

    # If full output too long, also send as a file
    full_output = ANSI_RE.sub("", "".join(buf)).strip()
    if len(full_output) > MAX_CHARS:
        path = f"/tmp/out_{int(time.time())}.txt"
        with open(path, "w", encoding="utf-8") as f:
            f.write(full_output)
        await status_msg.reply_document(path, caption=f"{header}\n_(full output — file)_")
        os.remove(path)

async def _reply(m: Message, status_msg, header: str, output: str):
    """Static reply — edit status_msg in-place once."""
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

def _uptime(sec: int) -> str:
    h, rem = divmod(sec, 3600)
    m, s   = divmod(rem, 60)
    return f"{h}h {m}m {s}s" if h else f"{m}m {s}s"

def _find_req(base_path: str) -> Path | None:
    """Search for requirements.txt in given path or its parent directories (up to 2 levels)."""
    for p in [Path(base_path), Path(base_path).parent]:
        req = p / "requirements.txt" if p.is_dir() else p
        if req.name == "requirements.txt" and req.exists():
            return req
    return None

# ── /start ────────────────────────────────────────────────────────────────────
@app.on_message(filters.command("start"))
async def cmd_start(_, m: Message):
    await m.reply_text(
        "```\n"
        "┌──────────────────────────────────────────┐\n"
        "│   ⚡  TERMINAL BOT  —  Ayuprime  v5      │\n"
        "└──────────────────────────────────────────┘\n"
        "\n"
        "SHELL\n"
        "  /sh  <cmd>               run any command (live)\n"
        "\n"
        "SCRIPT RUNNER\n"
        "  /run   <tag> <file>      start in background\n"
        "                           (auto pip install if req found)\n"
        "  /stop  <tag>             kill a script\n"
        "  /stopall                 kill everything\n"
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
        "```"
    )

# ── /sh — live shell ──────────────────────────────────────────────────────────
@app.on_message(filters.command(["sh", "bash", "termux"]) & owner_filter)
async def cmd_sh(_, m: Message):
    if len(m.command) < 2:
        return await m.reply_text("Usage: `/sh <command>`")
    cmd  = m.text.split(maxsplit=1)[1]
    cwd  = user_cwd.get(m.from_user.id, ".")
    stat = await m.reply_text(f"```\n$ {cmd[:100]}\n▌\n```")
    await _shell_live(f"cd '{cwd}' && {cmd}", stat, f"$ {cmd[:80]}")

# ── /run — start script (with auto pip install) ───────────────────────────────
@app.on_message(filters.command("run") & owner_filter)
async def cmd_run(_, m: Message):
    parts = m.text.split(maxsplit=3)
    if len(parts) < 3:
        return await m.reply_text("Usage: `/run <tag> <script_path> [args]`")
    tag, script = parts[1], parts[2]
    extra = parts[3] if len(parts) > 3 else ""

    if tag in running_processes:
        return await m.reply_text(f"⚠️ `{tag}` is already running.\nUse `/stop {tag}` first.")
    if not os.path.exists(script):
        return await m.reply_text(f"❌ File not found: `{script}`")

    # ── Auto pip install if requirements.txt exists nearby ──
    script_dir = str(Path(script).parent)
    req = (Path(script_dir) / "requirements.txt")
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
    proc = await asyncio.create_subprocess_shell(
        cmd, stdout=log_fh, stderr=log_fh, preexec_fn=os.setsid)
    running_processes[tag] = {
        "process": proc, "cmd": cmd,
        "started": time.time(), "log": log_path, "log_fh": log_fh,
    }
    await m.reply_text(
        f"```\n[STARTED]  {tag}\nPID     :  {proc.pid}\nScript  :  {script}\nLog     :  /log {tag}\n```"
    )
    asyncio.create_task(_watch(tag, m))

async def _watch(tag: str, m: Message):
    """Notify when a background script exits."""
    entry = running_processes.get(tag)
    if not entry:
        return
    rc = await entry["process"].wait()
    entry["log_fh"].close()
    running_processes.pop(tag, None)
    try:
        await m.reply_text(f"```\n[EXITED]  {tag}  (code {rc})\nUse /log {tag} to review.\n```")
    except Exception:
        pass

# ── /stop ─────────────────────────────────────────────────────────────────────
@app.on_message(filters.command("stop") & owner_filter)
async def cmd_stop(_, m: Message):
    if len(m.command) < 2:
        return await m.reply_text("Usage: `/stop <tag>`")
    tag   = m.command[1]
    entry = running_processes.get(tag)
    if not entry:
        return await m.reply_text(
            f"❌ No running process named `{tag}`.\nUse `/ps` to see active scripts.")
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
    await m.reply_text(f"```\n[STOPPED]  {tag}\nPID {pid} terminated.\n```")

# ── /stopall ──────────────────────────────────────────────────────────────────
@app.on_message(filters.command("stopall") & owner_filter)
async def cmd_stopall(_, m: Message):
    if not running_processes:
        return await m.reply_text("ℹ️ No scripts running.")
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
@app.on_message(filters.command("ps") & owner_filter)
async def cmd_ps(_, m: Message):
    if not running_processes:
        return await m.reply_text("ℹ️ No scripts running.")
    lines = ["TAG              PID    UPTIME     COMMAND", "─" * 50]
    for tag, v in running_processes.items():
        up  = _uptime(int(time.time() - v["started"]))
        cmd = v["cmd"][:28] + ("…" if len(v["cmd"]) > 28 else "")
        lines.append(f"{tag:<16} {v['process'].pid:<6} {up:<10} {cmd}")
    await m.reply_text("```\n" + "\n".join(lines) + "\n```")

# ── /log ──────────────────────────────────────────────────────────────────────
@app.on_message(filters.command("log") & owner_filter)
async def cmd_log(_, m: Message):
    if len(m.command) < 2:
        return await m.reply_text("Usage: `/log <tag> [lines=50]`")
    tag      = m.command[1]
    n        = int(m.command[2]) if len(m.command) > 2 else 50
    log_path = LOGS_DIR / f"{tag}.log"
    if not log_path.exists():
        if tag in running_processes:
            return await m.reply_text(f"```\n[LOG]  {tag}\nProcess running but no output yet.\n```")
        return await m.reply_text(f"❌ No log found for `{tag}`.")
    stat = await m.reply_text(f"```\n[LOG]  {tag}  (last {n} lines)\nReading...\n```")
    out  = await _shell(f"tail -n {n} '{log_path}'")
    await _reply(m, stat, f"[LOG]  {tag}", out)

# ── /runreq — pip install requirements.txt (live output) ─────────────────────
@app.on_message(filters.command("runreq") & owner_filter)
async def cmd_runreq(_, m: Message):
    """
    /runreq              → installs requirements.txt from current /cd directory
    /runreq <path>       → installs from given path (file or folder)
    """
    cwd = user_cwd.get(m.from_user.id, ".")
    if len(m.command) < 2:
        req = Path(cwd) / "requirements.txt"
    else:
        arg = m.command[1]
        p   = Path(arg) if Path(arg).is_absolute() else Path(cwd) / arg
        req = p if p.name == "requirements.txt" else p / "requirements.txt"

    if not req.exists():
        return await m.reply_text(
            f"❌ Not found: `{req}`\n"
            f"Use `/cd <folder>` to go to the right directory first."
        )
    stat = await m.reply_text(f"```\n$ pip install -r {req}\n▌\n```")
    await _shell_live(
        f"{sys.executable} -m pip install -r '{req}'",
        stat,
        f"$ pip install -r {req}",
        timeout=300,
    )

# ── /cd ───────────────────────────────────────────────────────────────────────
@app.on_message(filters.command("cd") & owner_filter)
async def cmd_cd(_, m: Message):
    if len(m.command) < 2:
        cwd = user_cwd.get(m.from_user.id, str(SCRIPTS_DIR))
        return await m.reply_text(f"📂 Current upload dir: `{cwd}`")
    path = Path(m.text.split(maxsplit=1)[1])
    path.mkdir(parents=True, exist_ok=True)
    user_cwd[m.from_user.id] = str(path)
    await m.reply_text(f"```\n[CD]  Upload dir → {path}\n```")

# ── File receiver ─────────────────────────────────────────────────────────────
@app.on_message(filters.document & owner_filter)
async def cmd_file(_, m: Message):
    fname  = m.document.file_name
    cwd    = user_cwd.get(m.from_user.id, str(SCRIPTS_DIR))
    dest   = Path(cwd) / fname
    stat   = await m.reply_text(f"```\n[DOWNLOAD]  {fname}\nSaving to {cwd}/...\n```")
    await m.download(file_name=str(dest))
    size   = dest.stat().st_size
    await stat.edit_text(
        f"```\n[SAVED]  {fname}\nPath  :  {dest}\nSize  :  {size:,} bytes\nRun   :  /run mytag {dest}\n```"
    )

# ── /ls — mobile-friendly directory listing ───────────────────────────────────
@app.on_message(filters.command("ls") & owner_filter)
async def cmd_ls(_, m: Message):
    if len(m.command) > 1:
        path = m.text.split(maxsplit=1)[1]
    else:
        path = user_cwd.get(m.from_user.id, ".")
    stat = await m.reply_text(f"```\n📂 {path}\nListing...\n```")
    raw  = await _shell(f"ls -lah '{path}'")
    lines_out = [f"📂  {path}", "─" * 32]
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
            if sz >= 1_073_741_824:  size_fmt = f"{sz/1_073_741_824:.1f}GB"
            elif sz >= 1_048_576:    size_fmt = f"{sz/1_048_576:.1f}MB"
            elif sz >= 1024:         size_fmt = f"{sz/1024:.1f}KB"
            else:                    size_fmt = f"{sz}B"
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
    await _reply(m, stat, f"📂 {path}", "\n".join(lines_out))

# ── /mkdir ────────────────────────────────────────────────────────────────────
@app.on_message(filters.command("mkdir") & owner_filter)
async def cmd_mkdir(_, m: Message):
    if len(m.command) < 2:
        return await m.reply_text("Usage: `/mkdir <path>`")
    path = m.text.split(maxsplit=1)[1]
    os.makedirs(path, exist_ok=True)
    await m.reply_text(f"```\n[CREATED]  {path}\n```")

# ── /rm ───────────────────────────────────────────────────────────────────────
@app.on_message(filters.command("rm") & owner_filter)
async def cmd_rm(_, m: Message):
    if len(m.command) < 2:
        return await m.reply_text("Usage: `/rm <path>`")
    path = m.text.split(maxsplit=1)[1]
    stat = await m.reply_text(f"```\n$ rm -rf {path}\nDeleting...\n```")
    out  = await _shell(f"rm -rf '{path}' && echo 'Deleted.'")
    await _reply(m, stat, f"$ rm -rf {path}", out)

# ── /mv — move/rename ─────────────────────────────────────────────────────────
@app.on_message(filters.command("mv") & owner_filter)
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

# ── /cp — copy ────────────────────────────────────────────────────────────────
@app.on_message(filters.command("cp") & owner_filter)
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
@app.on_message(filters.command("upload") & owner_filter)
async def cmd_upload(_, m: Message):
    if len(m.command) < 2:
        return await m.reply_text("Usage: `/upload <path>`")
    path = m.text.split(maxsplit=1)[1]
    if not os.path.exists(path):
        return await m.reply_text(f"❌ Not found: `{path}`")
    stat = await m.reply_text(f"```\n[UPLOAD]  {path}\nSending...\n```")
    await m.reply_document(path)
    await stat.delete()

# ── /nano — append line to file ───────────────────────────────────────────────
@app.on_message(filters.command("nano") & owner_filter)
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
    await m.reply_text(f"```\n[NANO]  {fpath}\nAppended line {lines}: {text[:60]}\n```")

# ── /write — overwrite file ───────────────────────────────────────────────────
@app.on_message(filters.command("write") & owner_filter)
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

# ── /clear — wipe file contents ───────────────────────────────────────────────
@app.on_message(filters.command("clear") & owner_filter)
async def cmd_clear(_, m: Message):
    if len(m.command) < 2:
        return await m.reply_text("Usage: `/clear <filename>`")
    cwd   = user_cwd.get(m.from_user.id, ".")
    fpath = Path(cwd) / m.command[1]
    if not fpath.exists():
        return await m.reply_text(f"❌ Not found: `{fpath}`")
    open(fpath, "w").close()
    await m.reply_text(f"```\n[CLEAR]  {fpath}\nFile wiped — 0 bytes.\n```")

# ── /cat — read file contents inline ─────────────────────────────────────────
@app.on_message(filters.command("cat") & owner_filter)
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

# ── /find — search files by name ─────────────────────────────────────────────
@app.on_message(filters.command("find") & owner_filter)
async def cmd_find(_, m: Message):
    parts = m.text.split(maxsplit=2)
    if len(parts) < 2:
        return await m.reply_text("Usage: `/find <name> [search_path]`")
    name  = parts[1]
    spath = parts[2] if len(parts) > 2 else user_cwd.get(m.from_user.id, ".")
    stat  = await m.reply_text(f"```\n$ find {spath} -name '*{name}*'\nSearching...\n```")
    out   = await _shell(f"find '{spath}' -name '*{name}*' 2>/dev/null | head -50")
    await _reply(m, stat, f"$ find {spath} -name '*{name}*'", out or "No results found.")

# ── /grep — search text in files ─────────────────────────────────────────────
@app.on_message(filters.command("grep") & owner_filter)
async def cmd_grep(_, m: Message):
    parts = m.text.split(maxsplit=3)
    if len(parts) < 3:
        return await m.reply_text("Usage: `/grep <pattern> <path> [flags]`\nExample: `/grep TODO . -r`")
    pattern = parts[1]
    path    = parts[2]
    flags   = parts[3] if len(parts) > 3 else "-r --include='*.py'"
    stat    = await m.reply_text(f"```\n$ grep {flags} '{pattern}' {path}\nSearching...\n```")
    out     = await _shell(f"grep {flags} '{pattern}' '{path}' 2>/dev/null | head -50")
    await _reply(m, stat, f"$ grep '{pattern}' {path}", out or "No matches found.")

# ── /py — run Python snippet inline ──────────────────────────────────────────
@app.on_message(filters.command("py") & owner_filter)
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

# ── /zip — zip a folder and send ─────────────────────────────────────────────
@app.on_message(filters.command("zip") & owner_filter)
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
    result  = await _shell(f"cd '{tpath.parent}' && zip -r '{out_zip}' '{tpath.name}'", timeout=120)
    if not Path(out_zip).exists():
        return await _reply(m, stat, f"[ZIP]  {tpath}", result)
    size = Path(out_zip).stat().st_size
    await stat.edit_text(f"```\n[ZIP]  {tpath}\nSize: {size:,} bytes — sending...\n```")
    await m.reply_document(out_zip, caption=f"📦 `{tpath.name}.zip`")
    await stat.delete()
    os.remove(out_zip)

# ── /unzip — extract archive ──────────────────────────────────────────────────
@app.on_message(filters.command("unzip") & owner_filter)
async def cmd_unzip(_, m: Message):
    parts = m.text.split(maxsplit=2)
    if len(parts) < 2:
        return await m.reply_text("Usage: `/unzip <file.zip> [destination]`")
    cwd  = user_cwd.get(m.from_user.id, ".")
    src  = parts[1]
    dst  = parts[2] if len(parts) > 2 else cwd
    fpath = Path(src) if Path(src).is_absolute() else Path(cwd) / src
    if not fpath.exists():
        return await m.reply_text(f"❌ Not found: `{fpath}`")
    stat = await m.reply_text(f"```\n$ unzip {fpath} -d {dst}\nExtracting...\n```")
    out  = await _shell(f"unzip -o '{fpath}' -d '{dst}'", timeout=120)
    await _reply(m, stat, f"$ unzip {fpath}", out)

# ── /sys ──────────────────────────────────────────────────────────────────────
@app.on_message(filters.command("sys") & owner_filter)
async def cmd_sys(_, m: Message):
    stat = await m.reply_text("```\nFetching system info...\n```")
    cmd  = ("echo '[ CPU ]' && grep 'model name' /proc/cpuinfo | head -1 | cut -d: -f2 | xargs && "
            "echo '[ RAM ]' && free -h && echo '[ DISK ]' && df -h / && "
            "echo '[ UPTIME ]' && uptime -p")
    out  = await _shell(cmd)
    await _reply(m, stat, "System Info", out)

# ── /top — live process monitor ───────────────────────────────────────────────
@app.on_message(filters.command("top") & owner_filter)
async def cmd_top(_, m: Message):
    stat = await m.reply_text("```\nFetching top processes...\n```")
    out  = await _shell("ps aux --sort=-%cpu | head -15")
    await _reply(m, stat, "Top Processes (by CPU)", out)

# ── /kill — kill by PID ───────────────────────────────────────────────────────
@app.on_message(filters.command("kill") & owner_filter)
async def cmd_kill(_, m: Message):
    if len(m.command) < 2:
        return await m.reply_text("Usage: `/kill <pid>`")
    pid = m.command[1]
    out = await _shell(f"kill -9 {pid} && echo 'Killed PID {pid}'")
    await m.reply_text(f"```\n{out}\n```")

# ── /ping ─────────────────────────────────────────────────────────────────────
@app.on_message(filters.command("ping") & owner_filter)
async def cmd_ping(_, m: Message):
    t   = time.time()
    msg = await m.reply_text("```\nPinging...\n```")
    ms  = round((time.time() - t) * 1000)
    up  = _uptime(int(time.time() - BOT_START_TIME))
    await msg.edit_text(f"```\n🏓 Pong!  {ms}ms\nUptime: {up}\n```")

# ── /env ──────────────────────────────────────────────────────────────────────
@app.on_message(filters.command("env") & owner_filter)
async def cmd_env(_, m: Message):
    SECRET = {"BOT_TOKEN", "API_HASH", "PASSWORD", "SECRET", "KEY", "TOKEN"}
    lines  = [f"{k}={v[:4]+'••••' if any(s in k.upper() for s in SECRET) else v}"
              for k, v in sorted(os.environ.items())]
    stat   = await m.reply_text("```\nReading env...\n```")
    await _reply(m, stat, "Environment Variables", "\n".join(lines))

# ── /setenv ───────────────────────────────────────────────────────────────────
@app.on_message(filters.command("setenv") & owner_filter)
async def cmd_setenv(_, m: Message):
    parts = m.text.split(maxsplit=2)
    if len(parts) < 3:
        return await m.reply_text("Usage: `/setenv KEY VALUE`")
    os.environ[parts[1]] = parts[2]
    await m.reply_text(f"```\n[SET]  {parts[1]} = {parts[2][:40]}\n```")

# ── /restart — restart the bot process ───────────────────────────────────────
@app.on_message(filters.command("restart") & owner_filter)
async def cmd_restart(_, m: Message):
    await m.reply_text("```\n[RESTART]  Rebooting bot...\n```")
    await asyncio.sleep(1)
    os.execv(sys.executable, [sys.executable] + sys.argv)

# ── Unauthorized users ────────────────────────────────────────────────────────
@app.on_message(filters.incoming & ~owner_filter)
async def cmd_unauthorized(_, m: Message):
    await m.reply_text(
        "```\n[ACCESS DENIED]\nYou are not authorized to use this bot.\n```"
    )

# ── Main ──────────────────────────────────────────────────────────────────────
async def main():
    await start_web_server()
    await app.start()
    me = await app.get_me()
    print(f"⚡ @{me.username} is online | Owner: {OWNER_ID}")
    await idle()
    await app.stop()

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())

