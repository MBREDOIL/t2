import os
import re
import json
import logging
import asyncio
import aiohttp
import aiofiles
import hashlib
from datetime import datetime
from typing import Dict, List, Tuple
from urllib.parse import urlparse
from pyrogram.handlers import MessageHandler, CallbackQueryHandler
from pyrogram import Client, filters, enums
from pyrogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery
)
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
SUPPORTED_TYPES = {
    'document': ['application/pdf', 'text/plain'],
    'image': ['image/jpeg', 'image/png'],
    'audio': ['audio/mpeg', 'audio/ogg'],
    'video': ['video/mp4', 'video/quicktime']
}
TIMEZONE = 'Asia/Kolkata'
OWNER_ID = int(os.getenv("OWNER_ID"))  # Set in environment

class URLTrackerBot:
    def __init__(self):
        self.app = Client(
            "url_tracker",
            api_id=os.getenv("API_ID"),
            api_hash=os.getenv("API_HASH"),
            bot_token=os.getenv("BOT_TOKEN")
        )
        self.scheduler = AsyncIOScheduler(timezone=TIMEZONE)
        self.data = {
            'users': {},
            'sudo': [],
            'authorized': []
        }
        
        # Register handlers
        handlers = [
            (self.track_handler, 'track'),
            (self.untrack_handler, 'untrack'),
            (self.list_handler, 'list'),
            (self.docs_handler, 'documents'),
            (self.sudo_handler, 'addsudo'),
            (self.sudo_handler, 'removesudo'),
            (self.auth_handler, 'authchat'),
            (self.auth_handler, 'unauthchat'),
            (self.nightmode_handler, None)  # Callback
        ]
        
        for handler, command in handlers:
            if command:
                self.app.add_handler(MessageHandler(handler, filters.command(command)))
            else:
                self.app.add_handler(CallbackQueryHandler(handler))

    async def load_data(self):
        """Load persistent data"""
        try:
            async with aiofiles.open('data.json', 'r') as f:
                self.data = json.loads(await f.read())
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
        return chat_id in self.data['authorized'] or chat_id == OWNER_ID

    async def split_send(self, chat_id: int, text: str):
        """Split and send long text"""
        for i in range(0, len(text), MAX_MESSAGE_LENGTH):
            await self.app.send_message(chat_id, text[i:i+MAX_MESSAGE_LENGTH])

    async def extract_resources(self, url: str) -> List[Dict]:
        """Extract all resources from webpage"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    soup = BeautifulSoup(await resp.text(), 'lxml')
                    resources = []
                    
                    # Extract all links and media
                    for tag in soup.find_all(['a', 'img', 'audio', 'video', 'source']):
                        resource = {'url': None, 'name': '', 'type': 'document'}
                        
                        if tag.name == 'a' and (href := tag.get('href')):
                            resource['url'] = href
                            resource['name'] = tag.get_text(strip=True) or href.split('/')[-1]
                        elif (src := tag.get('src')):
                            resource['url'] = src
                            resource['name'] = tag.get('alt', tag.get('title', src.split('/')[-1]))
                        
                        if resource['url']:
                            # Make absolute URL
                            resource['url'] = urljoin(url, resource['url'])
                            # Determine type
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

    async def generate_text_file(self, url: str) -> str:
        """Generate formatted text file with resources"""
        resources = await self.extract_resources(url)
        filename = f"resources_{hashlib.md5(url.encode()).hexdigest()[:6]}.txt"
        content = f"Resources for {url}:\n\n"
        
        for res in resources:
            content += f"{res['name']}\n{res['url']}\n\n"
        
        async with aiofiles.open(filename, 'w') as f:
            await f.write(content)
        
        return filename

    async def check_updates(self, url: str, user_id: int):
        """Check for updates and notify user"""
        try:
            # Get current hash
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    current_hash = hashlib.sha256(await resp.read()).hexdigest()
            
            # Compare with stored hash
            user_data = self.data['users'].get(str(user_id), {})
            if url not in user_data:
                return
            
            if user_data[url]['hash'] != current_hash:
                # Send documents
                txt_file = await self.generate_text_file(url)
                await self.app.send_document(user_id, txt_file)
                os.remove(txt_file)
                
                # Send media files
                resources = await self.extract_resources(url)
                for res in resources:
                    try:
                        async with session.get(res['url']) as r:
                            if r.status == 200:
                                content = await r.read()
                                if len(content) > MAX_FILE_SIZE:
                                    continue
                                
                                send_method = {
                                    'image': self.app.send_photo,
                                    'video': self.app.send_video,
                                    'audio': self.app.send_audio
                                }.get(res['type'], self.app.send_document)
                                
                                await send_method(
                                    user_id,
                                    **{res['type']: content},
                                    caption=res['name']
                                )
                    except Exception as e:
                        logger.error(f"Failed to send {res['type']}: {e}")
                
                # Update hash
                self.data['users'][str(user_id)][url]['hash'] = current_hash
                await self.save_data()
                
        except Exception as e:
            logger.error(f"Update check failed: {e}")

    async def track_handler(self, client: Client, message: Message):
        """Handle /track command"""
        if not self.is_authorized(message.chat.id):
            return await message.reply("❌ Not authorized!")
        
        try:
            # Parse command: /track "Site Name" url interval
            _, name, url, interval = re.split(r'\s+', message.text, 3)
            interval = int(interval)
            parsed = urlparse(url)
            if not parsed.scheme:
                url = f"http://{url}"
            
            # Store tracking data
            user_id = message.from_user.id
            self.data['users'].setdefault(str(user_id), {})[url] = {
                'name': name,
                'interval': interval,
                'hash': '',
                'nightmode': False
            }
            
            # Schedule job
            trigger = IntervalTrigger(minutes=interval)
            if self.data['users'][str(user_id)][url]['nightmode']:
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
            
            # Send response with controls
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
            await message.reply("❌ Invalid format. Use: /track \"Site Name\" url interval")

    async def untrack_handler(self, client: Client, message: Message):
        """Handle /untrack command"""
        if not self.is_authorized(message.chat.id):
            return
        
        try:
            url = message.command[1]
            user_id = message.from_user.id
            user_key = str(user_id)
            
            if url in self.data['users'].get(user_key, {}):
                # Remove job and data
                self.scheduler.remove_job(f"{user_id}_{url}")
                del self.data['users'][user_key][url]
                await self.save_data()
                await message.reply(f"❌ Stopped tracking {url}")
            else:
                await message.reply("URL not found in your tracked list")
                
        except IndexError:
            await message.reply("Usage: /untrack url")

    async def list_handler(self, client: Client, message: Message):
        """Handle /list command"""
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
        
        await self.split_send(message.chat.id, response)

    async def docs_handler(self, client: Client, message: Message):
        """Handle /documents command"""
        if not self.is_authorized(message.chat.id):
            return
        
        try:
            url = message.command[1]
            user_id = message.from_user.id
            user_data = self.data['users'].get(str(user_id), {})
            
            if url not in user_data:
                return await message.reply("URL not tracked")
            
            # Generate and send document
            txt_file = await self.generate_text_file(url)
            await self.app.send_document(
                message.chat.id,
                txt_file,
                caption=f"Resources for {url}"
            )
            os.remove(txt_file)
            
        except IndexError:
            await message.reply("Usage: /documents url")

    async def sudo_handler(self, client: Client, message: Message):
        """Handle sudo user management"""
        if not self.is_owner(message.from_user.id):
            return await message.reply("❌ Owner only!")
        
        try:
            cmd, user_id = message.command
            user_id = int(user_id)
            
            if cmd == 'addsudo':
                if user_id not in self.data['sudo']:
                    self.data['sudo'].append(user_id)
                    await message.reply(f"✅ Added sudo user {user_id}")
                else:
                    await message.reply("User already sudo")
                    
            elif cmd == 'removesudo':
                if user_id in self.data['sudo']:
                    self.data['sudo'].remove(user_id)
                    await message.reply(f"❌ Removed sudo user {user_id}")
                else:
                    await message.reply("User not in sudo list")
            
            await self.save_data()
            
        except (IndexError, ValueError):
            await message.reply("Usage: /addsudo user_id or /removesudo user_id")

    async def auth_handler(self, client: Client, message: Message):
        """Handle chat authorization"""
        if not self.is_owner(message.from_user.id):
            return
        
        try:
            cmd = message.command[0]
            if cmd == 'authchat':
                self.data['authorized'].append(message.chat.id)
                await message.reply("✅ Chat authorized")
            elif cmd == 'unauthchat':
                if message.chat.id in self.data['authorized']:
                    self.data['authorized'].remove(message.chat.id)
                    await message.reply("❌ Chat access removed")
                else:
                    await message.reply("Chat not authorized")
            
            await self.save_data()
            
        except Exception as e:
            logger.error(f"Auth error: {e}")

    async def nightmode_handler(self, client: Client, query: CallbackQuery):
        """Toggle night mode for tracking"""
        _, user_id, url = query.data.split('_')
        user_id = int(user_id)
        user_key = str(user_id)
        
        if user_key not in self.data['users'] or url not in self.data['users'][user_key]:
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

    async def run(self):
        """Start the bot"""
        await self.load_data()
        self.scheduler.start()
        await self.app.start()
        logger.info("Bot running...")
        await asyncio.Event().wait()

if __name__ == "__main__":
    bot = URLTrackerBot()
    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        logger.info("Stopping bot...")
        bot.scheduler.shutdown()
