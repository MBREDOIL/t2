import os
import re
import json
import logging
import asyncio
import aiohttp
import aiofiles
import hashlib
from datetime import datetime
from urllib.parse import urlparse, urljoin
from typing import Dict, List

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
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID"))

class DataManager:
    """Handles all data storage and retrieval"""
    def __init__(self):
        self.data = {
            'tracked': {},
            'authorized': [],
            'sudo': [OWNER_ID]
        }
        self.load_data()

    def load_data(self):
        try:
            with open('data.json', 'r') as f:
                self.data.update(json.load(f))
        except (FileNotFoundError, json.JSONDecodeError):
            pass

    def save_data(self):
        with open('data.json', 'w') as f:
            json.dump(self.data, f)

class URLTrackerBot:
    def __init__(self):
        self.app = Client(
            "url_tracker_bot",
            api_id=API_ID,
            api_hash=API_HASH,
            bot_token=BOT_TOKEN
        )
        self.scheduler = AsyncIOScheduler(timezone=TIMEZONE)
        self.data = DataManager()
        self.http = aiohttp.ClientSession()
        
        # Register handlers
        self.register_handlers()

    def register_handlers(self):
        handlers = [
            (self.track_handler, 'track'),
            (self.untrack_handler, 'untrack'),
            (self.list_handler, 'list'),
            (self.docs_handler, 'documents'),
            (self.sudo_handler, 'addsudo'),
            (self.sudo_handler, 'removesudo'),
            (self.auth_handler, 'authchat'),
            (self.auth_handler, 'unauthchat'),
            (self.nightmode_handler, None)
        ]
        
        for handler, command in handlers:
            if command:
                self.app.add_handler(MessageHandler(
                    handler, 
                    filters.command(command) & self.is_authorized
                ))
            else:
                self.app.add_handler(CallbackQueryHandler(
                    handler, 
                    filters.regex(r"^nightmode_")
                ))

    # ------------------- Authorization Checks ------------------- #
    def is_authorized(self, _, message: Message):
        return (
            message.chat.id in self.data.data['authorized'] or 
            message.from_user.id in self.data.data['sudo']
        )

    # ------------------- Utility Functions ------------------- #
    async def split_send(self, chat_id: int, text: str):
        """Split long messages into chunks"""
        for i in range(0, len(text), MAX_MESSAGE_LENGTH):
            await self.app.send_message(chat_id, text[i:i+MAX_MESSAGE_LENGTH])

    async def extract_resources(self, url: str) -> List[dict]:
        """Extract media/resources from webpage"""
        try:
            async with self.http.get(url) as resp:
                soup = BeautifulSoup(await resp.text(), 'lxml')
                resources = []
                
                for tag in soup.find_all(['a', 'img', 'audio', 'video', 'source']):
                    resource = {}
                    if tag.name == 'a' and (href := tag.get('href')):
                        resource['url'] = urljoin(url, href)
                        resource['name'] = tag.get_text(strip=True) or href.split('/')[-1]
                    elif (src := tag.get('src')):
                        resource['url'] = urljoin(url, src)
                        resource['name'] = tag.get('alt', tag.get('title', src.split('/')[-1]))
                    
                    if resource.get('url'):
                        resource['type'] = next(
                            (k for k, v in SUPPORTED_TYPES.items() 
                             if any(resource['url'].lower().endswith(ext) for ext in v)),
                            'document'
                        )
                        resources.append(resource)
                return resources
        except Exception as e:
            logger.error(f"Extraction error: {e}")
            return []

    # ------------------- Core Tracking Logic ------------------- #
    async def check_updates(self, url: str, user_id: int):
        """Check for website updates"""
        try:
            async with self.http.get(url) as resp:
                content = await resp.read()
                current_hash = hashlib.sha256(content).hexdigest()
            
            user_key = str(user_id)
            tracked = self.data.data['tracked'].get(user_key, {})
            if url not in tracked:
                return
            
            if tracked[url]['hash'] != current_hash:
                await self.send_updates(user_id, url, content)
                self.data.data['tracked'][user_key][url]['hash'] = current_hash
                self.data.save_data()
                
        except Exception as e:
            logger.error(f"Update check failed: {e}")

    async def send_updates(self, user_id: int, url: str, content: bytes):
        """Send detected updates to user"""
        try:
            # Send text document
            resources = await self.extract_resources(url)
            text_content = f"🔔 Updates for {url}:\n\n" + "\n".join(
                f"{r['type'].title()}: {r['name']}\n{r['url']}" 
                for r in resources
            )
            
            if len(text_content) > MAX_MESSAGE_LENGTH:
                async with aiofiles.open('updates.txt', 'w') as f:
                    await f.write(text_content)
                await self.app.send_document(user_id, 'updates.txt')
                os.remove('updates.txt')
            else:
                await self.split_send(user_id, text_content)
            
            # Send media files
            for resource in resources:
                try:
                    async with self.http.get(resource['url']) as resp:
                        if resp.status == 200:
                            file_content = await resp.read()
                            if len(file_content) > MAX_FILE_SIZE:
                                continue
                            
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

    # ------------------- Command Handlers ------------------- #
    async def track_handler(self, client: Client, message: Message):
        """Handle /track command"""
        try:
            # Command format: /track "Site Name" url interval [night]
            match = re.match(r'/track\s+"(.+?)"\s+(\S+)\s+(\d+)(?:\s+(night))?', message.text)
            if not match:
                return await message.reply("❌ Invalid format. Use: /track \"Name\" url interval [night]")
            
            name, url, interval, night = match.groups()
            parsed = urlparse(url)
            if not parsed.scheme:
                url = f"http://{url}"
            
            # Store tracking data
            user_id = message.from_user.id
            user_key = str(user_id)
            interval = int(interval)
            night_mode = bool(night)
            
            self.data.data['tracked'].setdefault(user_key, {})[url] = {
                'name': name,
                'interval': interval,
                'hash': '',
                'night_mode': night_mode
            }
            
            # Schedule job
            trigger = IntervalTrigger(minutes=interval)
            if night_mode:
                trigger = AndTrigger([
                    trigger,
                    CronTrigger(hour='6-22', timezone=TIMEZONE)
                ])
            
            self.scheduler.add_job(
                self.check_updates,
                trigger=trigger,
                args=[url, user_id],
                id=f"{user_id}_{url}",
                replace_existing=True
            )
            
            # Send confirmation
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "🌙 Toggle Night Mode",
                    callback_data=f"nightmode_{user_id}_{url}"
                )
            ]])
            await message.reply(
                f"✅ Tracking started:\n{name}\n"
                f"URL: {url}\nInterval: {interval} minutes\n"
                f"Night Mode: {'ON' if night_mode else 'OFF'}",
                reply_markup=keyboard
            )
            self.data.save_data()
            
        except Exception as e:
            await message.reply(f"❌ Error: {str(e)}")

    async def untrack_handler(self, client: Client, message: Message):
        """Handle /untrack command"""
        try:
            url = message.command[1]
            user_id = message.from_user.id
            user_key = str(user_id)
            
            if url in self.data.data['tracked'].get(user_key, {}):
                self.scheduler.remove_job(f"{user_id}_{url}")
                del self.data.data['tracked'][user_key][url]
                self.data.save_data()
                await message.reply(f"❌ Stopped tracking {url}")
            else:
                await message.reply("URL not found in your tracked list")
                
        except IndexError:
            await message.reply("Usage: /untrack <url>")
        except Exception as e:
            await message.reply(f"❌ Error: {str(e)}")

    async def list_handler(self, client: Client, message: Message):
        """Handle /list command"""
        try:
            user_id = message.from_user.id
            tracked = self.data.data['tracked'].get(str(user_id), {})
            
            if not tracked:
                return await message.reply("You're not tracking any URLs")
            
            response = "📋 Tracked URLs:\n\n" + "\n\n".join(
                f"• {data['name']}\nURL: {url}\n"
                f"Interval: {data['interval']}m\n"
                f"Night Mode: {'ON' if data['night_mode'] else 'OFF'}"
                for url, data in tracked.items()
            )
            await self.split_send(message.chat.id, response)
            
        except Exception as e:
            await message.reply(f"❌ Error: {str(e)}")

    async def docs_handler(self, client: Client, message: Message):
        """Handle /documents command"""
        try:
            url = message.command[1]
            user_id = message.from_user.id
            user_data = self.data.data['tracked'].get(str(user_id), {})
            
            if url not in user_data:
                return await message.reply("URL not tracked")
            
            resources = await self.extract_resources(url)
            text_content = f"📑 Resources for {url}:\n\n" + "\n".join(
                f"{r['type'].title()}: {r['name']}\n{r['url']}" 
                for r in resources
            )
            
            if len(text_content) > MAX_MESSAGE_LENGTH:
                async with aiofiles.open('resources.txt', 'w') as f:
                    await f.write(text_content)
                await message.reply_document('resources.txt')
                os.remove('resources.txt')
            else:
                await message.reply(text_content)
                
        except IndexError:
            await message.reply("Usage: /documents <url>")
        except Exception as e:
            await message.reply(f"❌ Error: {str(e)}")

    async def sudo_handler(self, client: Client, message: Message):
        """Handle sudo commands"""
        try:
            if message.from_user.id != OWNER_ID:
                return await message.reply("❌ Owner only command!")
            
            cmd = message.command[0]
            user_id = int(message.command[1])
            
            if cmd == 'addsudo':
                if user_id not in self.data.data['sudo']:
                    self.data.data['sudo'].append(user_id)
                    await message.reply(f"✅ Added sudo user {user_id}")
                else:
                    await message.reply("User already in sudo list")
            elif cmd == 'removesudo':
                if user_id in self.data.data['sudo']:
                    self.data.data['sudo'].remove(user_id)
                    await message.reply(f"❌ Removed sudo user {user_id}")
                else:
                    await message.reply("User not in sudo list")
            
            self.data.save_data()
            
        except (IndexError, ValueError):
            await message.reply("Usage: /addsudo <user_id> or /removesudo <user_id>")
        except Exception as e:
            await message.reply(f"❌ Error: {str(e)}")

    async def auth_handler(self, client: Client, message: Message):
        """Handle chat authorization"""
        try:
            if message.from_user.id != OWNER_ID:
                return await message.reply("❌ Owner only command!")
            
            cmd = message.command[0]
            chat_id = message.chat.id
            
            if cmd == 'authchat':
                if chat_id not in self.data.data['authorized']:
                    self.data.data['authorized'].append(chat_id)
                    await message.reply("✅ Chat authorized")
                else:
                    await message.reply("Chat already authorized")
            elif cmd == 'unauthchat':
                if chat_id in self.data.data['authorized']:
                    self.data.data['authorized'].remove(chat_id)
                    await message.reply("❌ Chat access removed")
                else:
                    await message.reply("Chat not authorized")
            
            self.data.save_data()
            
        except Exception as e:
            await message.reply(f"❌ Error: {str(e)}")

    async def nightmode_handler(self, client: Client, query: CallbackQuery):
        """Toggle night mode"""
        try:
            data = query.data.split('_')
            user_id = int(data[1])
            url = data[2]
            user_key = str(user_id)
            
            if url not in self.data.data['tracked'].get(user_key, {}):
                return await query.answer("URL not found!", show_alert=True)
            
            # Toggle night mode
            current = self.data.data['tracked'][user_key][url]['night_mode']
            self.data.data['tracked'][user_key][url]['night_mode'] = not current
            
            # Update job trigger
            job = self.scheduler.get_job(f"{user_id}_{url}")
            if job:
                interval = self.data.data['tracked'][user_key][url]['interval']
                trigger = IntervalTrigger(minutes=interval)
                if not current:
                    trigger = AndTrigger([
                        trigger,
                        CronTrigger(hour='6-22', timezone=TIMEZONE)
                    ])
                
                self.scheduler.reschedule_job(job.id, trigger=trigger)
                await query.edit_message_text(
                    f"🌙 Night mode {'enabled' if not current else 'disabled'}\n"
                    f"for {self.data.data['tracked'][user_key][url]['name']}"
                )
                self.data.save_data()
            
            await query.answer()
            
        except Exception as e:
            logger.error(f"Night mode error: {e}")
            await query.answer("Error occurred!", show_alert=True)

    async def run(self):
        """Start the bot"""
        await self.app.start()
        self.scheduler.start()
        logger.info("Bot started successfully")
        await asyncio.Event().wait()

if __name__ == "__main__":
    bot = URLTrackerBot()
    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        logger.info("Stopping bot...")
        bot.scheduler.shutdown()
    finally:
        asyncio.run(bot.http.close())
