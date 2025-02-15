import os
import re
import json
import logging
import asyncio
import aiohttp
import aiofiles
import hashlib
from typing import List, Dict
from datetime import datetime
from urllib.parse import urlparse, urljoin

from pyrogram import Client, filters, enums
from pyrogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery
)
from pyrogram.handlers import MessageHandler, CallbackQueryHandler
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.combining import AndTrigger
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger
from bs4 import BeautifulSoup

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Configuration
MAX_MESSAGE_LENGTH = 4096
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB
TIMEZONE = "Asia/Kolkata"
SUPPORTED_TYPES = {
    'document': ['application/pdf', 'text/plain'],
    'image': ['image/jpeg', 'image/png'],
    'audio': ['audio/mpeg', 'audio/ogg'],
    'video': ['video/mp4', 'video/quicktime']
}

# Environment variables
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])

class URLTrackerBot:
    def __init__(self):
        self.app = Client(
            "url_tracker_bot",
            api_id=API_ID,
            api_hash=API_HASH,
            bot_token=BOT_TOKEN
        )
        self.scheduler = AsyncIOScheduler(timezone=TIMEZONE)
        self.data = {
            'users': {},
            'sudo': [],
            'authorized': [OWNER_ID]  # Auto-authorize owner
        }
        
        # Register handlers
        self.app.add_handler(MessageHandler(self.track_handler, filters.command("track")))
        self.app.add_handler(MessageHandler(self.untrack_handler, filters.command("untrack")))
        self.app.add_handler(MessageHandler(self.list_handler, filters.command("list")))
        self.app.add_handler(MessageHandler(self.docs_handler, filters.command("documents")))
        self.app.add_handler(MessageHandler(self.sudo_handler, filters.command("addsudo") | filters.command("removesudo")))
        self.app.add_handler(MessageHandler(self.auth_handler, filters.command("authchat") | filters.command("unauthchat")))
        self.app.add_handler(CallbackQueryHandler(self.nightmode_handler, filters.regex(r"^nightmode_")))

    async def load_data(self):
        """Load persistent data"""
        try:
            async with aiofiles.open('data.json', 'r') as f:
                self.data.update(json.loads(await f.read()))
        except (FileNotFoundError, json.JSONDecodeError):
            pass

    async def save_data(self):
        """Save all data"""
        async with aiofiles.open('data.json', 'w') as f:
            await f.write(json.dumps(self.data, indent=2))

    def is_owner(self, user_id: int) -> bool:
        return user_id == OWNER_ID

    def is_sudo(self, user_id: int) -> bool:
        return user_id in self.data['sudo'] or self.is_owner(user_id)

    def is_authorized(self, chat_id: int) -> bool:
        return chat_id in self.data['authorized'] or self.is_owner(chat_id)

    async def send_split_messages(self, chat_id: int, text: str):
        """Send long messages in chunks"""
        for i in range(0, len(text), MAX_MESSAGE_LENGTH):
            await self.app.send_message(chat_id, text[i:i+MAX_MESSAGE_LENGTH])

    async def extract_resources(self, url: str) -> List[dict]:
        """Extract resources from webpage"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    soup = BeautifulSoup(await resp.text(), 'lxml')
                    resources = []
                    
                    for tag in soup.find_all(['a', 'img', 'audio', 'video', 'source']):
                        resource = {'url': None, 'name': '', 'type': 'document'}
                        
                        if tag.name == 'a' and (href := tag.get('href')):
                            resource['url'] = urljoin(url, href)
                            resource['name'] = tag.get_text(strip=True) or href.split('/')[-1]
                        elif (src := tag.get('src')):
                            resource['url'] = urljoin(url, src)
                            resource['name'] = tag.get('alt', tag.get('title', src.split('/')[-1]))
                        
                        if resource['url']:
                            resource['type'] = next(
                                (k for k, v in SUPPORTED_TYPES.items() 
                                 if any(resource['url'].endswith(ext) for ext in v)),
                                'document'
                            )
                            resources.append(resource)
                    return resources
        except Exception as e:
            logger.error(f"Extraction error: {e}")
            return []

    async def check_updates(self, url: str, user_id: int):
        """Check for updates and notify user"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    content = await resp.read()
                    current_hash = hashlib.sha256(content).hexdigest()
                
                user_key = str(user_id)
                if url not in self.data['users'].get(user_key, {}):
                    return
                
                stored_hash = self.data['users'][user_key][url]['hash']
                if stored_hash != current_hash:
                    # Send updates
                    await self.send_updates(user_id, url, content)
                    self.data['users'][user_key][url]['hash'] = current_hash
                    await self.save_data()
                    
        except Exception as e:
            logger.error(f"Update check failed: {e}")

    async def send_updates(self, user_id: int, url: str, content: bytes):
        """Send detected updates to user"""
        try:
            resources = await self.extract_resources(url)
            text_content = f"🔔 Update detected for {url}\n\nResources:\n"
            text_content += "\n".join([f"{r['type'].title()}: {r['name']}\n{r['url']}" for r in resources])
            
            # Send text document
            async with aiofiles.open('update.txt', 'wb') as f:
                await f.write(text_content.encode())
            await self.app.send_document(user_id, 'update.txt')
            os.remove('update.txt')
            
            # Send media files
            for resource in resources:
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.get(resource['url']) as resp:
                            if resp.status == 200 and int(resp.headers.get('Content-Length', 0)) <= MAX_FILE_SIZE:
                                file_content = await resp.read()
                                send_method = {
                                    'image': self.app.send_photo,
                                    'video': self.app.send_video,
                                    'audio': self.app.send_audio
                                }.get(resource['type'], self.app.send_document)
                                
                                await send_method(
                                    user_id,
                                    **{resource['type']: file_content},
                                    caption=resource['name']
                                )
                except Exception as e:
                    logger.error(f"Failed to send {resource['type']}: {e}")
                    
        except Exception as e:
            logger.error(f"Update notification failed: {e}")

    async def track_handler(self, client: Client, message: Message):
        """Handle /track command"""
        try:
            # Basic command format: /track <name> <url> <interval>
            if not self.is_authorized(message.chat.id):
                return await message.reply("❌ You're not authorized!")
            
            parts = message.text.split(maxsplit=3)
            if len(parts) < 4:
                return await message.reply("Usage: /track <name> <url> <interval>")
            
            _, name, url, interval = parts
            parsed = urlparse(url)
            if not parsed.scheme:
                url = f"http://{url}"
            
            # Store tracking data
            user_id = message.from_user.id
            user_key = str(user_id)
            self.data['users'].setdefault(user_key, {})[url] = {
                'name': name,
                'interval': int(interval),
                'hash': '',
                'nightmode': False
            }
            
            # Schedule job
            trigger = IntervalTrigger(minutes=int(interval))
            self.scheduler.add_job(
                self.check_updates,
                trigger=trigger,
                args=[url, user_id],
                id=f"{user_id}_{url}",
                replace_existing=True
            )
            
            # Send response
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "🌙 Toggle Night Mode",
                    callback_data=f"nightmode_{user_id}_{url}"
                )
            ]])
            await message.reply(
                f"✅ Tracking started for:\n{name}\n"
                f"URL: {url}\nInterval: {interval} minutes",
                reply_markup=keyboard
            )
            await self.save_data()
            
        except Exception as e:
            await message.reply(f"❌ Error: {str(e)}")

    async def untrack_handler(self, client: Client, message: Message):
        """Handle /untrack command"""
        try:
            if not self.is_authorized(message.chat.id):
                return
            
            if len(message.command) < 2:
                return await message.reply("Usage: /untrack <url>")
            
            url = message.command[1]
            user_id = message.from_user.id
            user_key = str(user_id)
            
            if url in self.data['users'].get(user_key, {}):
                self.scheduler.remove_job(f"{user_id}_{url}")
                del self.data['users'][user_key][url]
                await self.save_data()
                await message.reply(f"❌ Stopped tracking {url}")
            else:
                await message.reply("URL not found in your tracked list")
                
        except Exception as e:
            await message.reply(f"❌ Error: {str(e)}")

    async def list_handler(self, client: Client, message: Message):
        """Handle /list command"""
        try:
            if not self.is_authorized(message.chat.id):
                return
            
            user_id = message.from_user.id
            tracked = self.data['users'].get(str(user_id), {})
            
            if not tracked:
                return await message.reply("You're not tracking any URLs")
            
            response = "📋 Your Tracked URLs:\n\n"
            for url, data in tracked.items():
                response += (
                    f"• {data['name']}\n"
                    f"URL: {url}\n"
                    f"Interval: {data['interval']}m\n"
                    f"Night Mode: {'ON' if data['nightmode'] else 'OFF'}\n\n"
                )
            
            await self.send_split_messages(message.chat.id, response)
            
        except Exception as e:
            await message.reply(f"❌ Error: {str(e)}")

    async def docs_handler(self, client: Client, message: Message):
        """Handle /documents command"""
        try:
            if not self.is_authorized(message.chat.id):
                return
            
            if len(message.command) < 2:
                return await message.reply("Usage: /documents <url>")
            
            url = message.command[1]
            user_id = message.from_user.id
            user_data = self.data['users'].get(str(user_id), {})
            
            if url not in user_data:
                return await message.reply("URL not tracked")
            
            resources = await self.extract_resources(url)
            text_content = f"Resources for {url}:\n\n"
            text_content += "\n".join([f"{r['type'].title()}: {r['name']}\n{r['url']}" for r in resources])
            
            async with aiofiles.open('resources.txt', 'wb') as f:
                await f.write(text_content.encode())
            await self.app.send_document(message.chat.id, 'resources.txt')
            os.remove('resources.txt')
            
        except Exception as e:
            await message.reply(f"❌ Error: {str(e)}")

    async def sudo_handler(self, client: Client, message: Message):
        """Handle sudo commands"""
        try:
            if not self.is_owner(message.from_user.id):
                return await message.reply("❌ Owner only command!")
            
            if len(message.command) < 2:
                return await message.reply("Usage: /addsudo <user_id> or /removesudo <user_id>")
            
            cmd = message.command[0]
            user_id = int(message.command[1])
            
            if cmd == 'addsudo':
                if user_id not in self.data['sudo']:
                    self.data['sudo'].append(user_id)
                    await message.reply(f"✅ Added sudo user {user_id}")
                else:
                    await message.reply("User already in sudo list")
            elif cmd == 'removesudo':
                if user_id in self.data['sudo']:
                    self.data['sudo'].remove(user_id)
                    await message.reply(f"❌ Removed sudo user {user_id}")
                else:
                    await message.reply("User not in sudo list")
            
            await self.save_data()
            
        except Exception as e:
            await message.reply(f"❌ Error: {str(e)}")

    async def auth_handler(self, client: Client, message: Message):
        """Handle chat authorization"""
        try:
            if not self.is_owner(message.from_user.id):
                return
            
            cmd = message.command[0]
            chat_id = message.chat.id
            
            if cmd == 'authchat':
                if chat_id not in self.data['authorized']:
                    self.data['authorized'].append(chat_id)
                    await message.reply("✅ Chat authorized")
                else:
                    await message.reply("Chat already authorized")
            elif cmd == 'unauthchat':
                if chat_id in self.data['authorized']:
                    self.data['authorized'].remove(chat_id)
                    await message.reply("❌ Chat access removed")
                else:
                    await message.reply("Chat not authorized")
            
            await self.save_data()
            
        except Exception as e:
            await message.reply(f"❌ Error: {str(e)}")

    async def nightmode_handler(self, client: Client, query: CallbackQuery):
        """Handle night mode toggle"""
        try:
            data = query.data.split('_')
            user_id = int(data[1])
            url = data[2]
            user_key = str(user_id)
            
            if url not in self.data['users'].get(user_key, {}):
                return await query.answer("URL not found!", show_alert=True)
            
            # Toggle night mode
            current = self.data['users'][user_key][url]['nightmode']
            self.data['users'][user_key][url]['nightmode'] = not current
            
            # Update job trigger
            job = self.scheduler.get_job(f"{user_id}_{url}")
            if job:
                interval = self.data['users'][user_key][url]['interval']
                trigger = IntervalTrigger(minutes=interval)
                
                if not current:
                    trigger = AndTrigger([
                        trigger,
                        CronTrigger(hour='6-22', timezone=TIMEZONE)
                    ])
                
                self.scheduler.reschedule_job(job.id, trigger=trigger)
                await query.edit_message_text(
                    f"🌙 Night mode {'enabled' if not current else 'disabled'}\n"
                    f"for {self.data['users'][user_key][url]['name']}"
                )
                await self.save_data()
            
            await query.answer()
            
        except Exception as e:
            logger.error(f"Night mode error: {e}")
            await query.answer("Error occurred!", show_alert=True)

    async def run(self):
        """Start the bot"""
        await self.load_data()
        self.scheduler.start()
        await self.app.start()
        logger.info("Bot is running...")
        await asyncio.Event().wait()

if __name__ == "__main__":
    bot = URLTrackerBot()
    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        logger.info("Stopping bot...")
        bot.scheduler.shutdown()
    finally:
        asyncio.run(bot.app.stop())
