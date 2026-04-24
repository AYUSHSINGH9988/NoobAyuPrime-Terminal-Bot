import os
import asyncio
import traceback
from pyrogram import Client, filters
from pyrogram.types import Message
from aiohttp import web

# ⚙️ Teri God-Mode Credentials
API_ID = 33675350
API_HASH = "2f97c845b067a750c9f36fec497acf97"
BOT_TOKEN = "8704517873:AAG89ly6CKrXTVeOj3OKNqHYq4mvZyHFBsM"

# 🛡️ Sirf Ayuprime command de sakta hai!
OWNER_ID = 8493596199

app = Client("TerminalBot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# ==========================================
# 🟢 Health Check Web Server (Port 8000)
# ==========================================
async def health_check(request):
    return web.Response(text="Ayuprime's God-Mode Bot is running! 🚀")

async def start_web_server():
    server = web.Application()
    server.add_routes([web.get('/', health_check)])
    runner = web.AppRunner(server)
    await runner.setup()
    
    # 🔥 Koyeb default port 8000 pe bind kiya
    site = web.TCPSite(runner, '0.0.0.0', 8000)
    await site.start()
    print("✅ Web Server started on port 8000 for Koyeb Health Checks")

# ==========================================
# 🛡️ Owner Verification Logic
# ==========================================
async def is_owner(_, __, message: Message):
    return message.from_user and message.from_user.id == OWNER_ID

owner_filter = filters.create(is_owner)

# ==========================================
# 🤖 Bot Commands & Terminal Logic
# ==========================================
@app.on_message(filters.command("start"))
async def start_cmd(client: Client, message: Message):
    await message.reply_text("🔥 **Ayuprime's Koyeb Terminal is Online!**\n\nUse `/sh <command>` to execute commands on the server. 🚀")

@app.on_message(filters.command(["sh", "termux"]) & owner_filter)
async def terminal_runner(client: Client, message: Message):
    if len(message.command) < 2:
        return await message.reply_text("❌ **Bhai command toh de!**\nExample: `/sh ls -la`")

    cmd = message.text.split(maxsplit=1)[1]
    msg = await message.reply_text(f"⚙️ **Executing:**\n`{cmd}`")

    try:
        # 🚀 Async command execution to prevent bot from freezing
        process = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stdout, stderr = await process.communicate()
        output = stdout.decode() + stderr.decode()

        # 🔥 Tere screenshot wala Premium JSON-like Format
        if not output:
            return await msg.edit_text("**OUTPUT :**\n```text\n[No Output / Command Executed Successfully]\n```")

        # 📄 Handle large outputs beyond Telegram's 4096 char limit
        if len(output) > 4000:
            with open("output.txt", "w", encoding="utf-8") as f:
                f.write(output)
            await msg.delete()
            await message.reply_document("output.txt", caption=f"**OUTPUT :** (File too long)\n`{cmd}`")
            os.remove("output.txt")
        else:
            await msg.edit_text(f"**OUTPUT :**\n```text\n{output}\n```")

    except Exception as e:
        await msg.edit_text(f"**ERROR :**\n```text\n{traceback.format_exc()}\n```")

# ==========================================
# 📥 File Upload System (Your Mini FTP)
# ==========================================
@app.on_message(filters.document & owner_filter)
async def file_receiver(client: Client, message: Message):
    msg = await message.reply_text("📥 **Downloading file to server...**")
    file_name = message.document.file_name
    await message.download(file_name=file_name)
    
    await msg.edit_text(f"✅ **File Saved as:** `{file_name}`\n\nAb tu isko `/sh python {file_name}` se chala sakta hai! 🚀")

# ==========================================
# ⚡ Main Loop Execution
# ==========================================
async def main():
    await start_web_server()
    await app.start()
    print("🚀 Ayuprime's Terminal Bot is Online and Ready!")
    await asyncio.Event().wait()

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
