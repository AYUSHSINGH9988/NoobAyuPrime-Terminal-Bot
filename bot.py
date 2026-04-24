import os
import asyncio
from pyrogram import Client, filters, idle
from pyrogram.types import Message
from aiohttp import web

# Config (Environment Variables se lega, warna default values use karega)
API_ID    = int(os.environ.get("API_ID", "33675350"))
API_HASH  = os.environ.get("API_HASH", "2f97c845b067a750c9f36fec497acf97")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8704517873:AAG89ly6CKrXTVeOj3OKNqHYq4mvZyHFBsM")
OWNER_ID  = int(os.environ.get("OWNER_ID", "8493596199"))

# Client Setup (in_memory=True jaruri hai cloud deployments ke liye)
app = Client("SimpleShellBot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN, in_memory=True)

# ════════════════════════════════════════════════════════════════════════════
# 🌐 Web Server for Health Check (Koyeb / Hugging Face)
# ════════════════════════════════════════════════════════════════════════════
async def health_check(request):
    return web.json_response({"status": "online", "bot": "SimpleShell"})

async def start_web_server():
    server = web.Application()
    server.router.add_get("/", health_check)
    runner = web.AppRunner(server)
    await runner.setup()
    
    # Hugging Face default 7860 use karta hai, Koyeb default 8000
    port = int(os.environ.get("PORT", 8000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"✅ Web server started on port {port}")

# ════════════════════════════════════════════════════════════════════════════
# 💻 /sh Command Handler (Sirf OWNER_ID ko allow karega)
# ════════════════════════════════════════════════════════════════════════════
@app.on_message(filters.command(["sh", "bash"]) & filters.user(OWNER_ID))
async def shell_cmd(_, message: Message):
    if len(message.command) < 2:
        return await message.reply_text("❌ Usage: `/sh <command>`")
    
    cmd = message.text.split(maxsplit=1)[1]
    msg = await message.reply_text(f"⚙️ Running...")
    
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        output = (stdout.decode() + stderr.decode()).strip() or "Done (No Output)"
        
        # Agar output limit se bada hai, toh .txt file banakar bhejega
        if len(output) > 4000:
            with open("out.txt", "w") as f: 
                f.write(output)
            await message.reply_document("out.txt")
            os.remove("out.txt")
        else:
            await msg.edit_text(f"**Output:**\n```\n{output}\n```")
    except Exception as e:
        await msg.edit_text(f"❌ Error: {str(e)}")

# ════════════════════════════════════════════════════════════════════════════
# ⚡ Main Runner
# ════════════════════════════════════════════════════════════════════════════
async def main():
    await start_web_server()
    await app.start()
    print("🚀 Simple Shell Bot is ONLINE!")
    await idle()  # Updates receive karne ke liye zaroori
    await app.stop()

if __name__ == "__main__":
    asyncio.run(main())
