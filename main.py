from telethon import TelegramClient, events
from telethon.types import Message
from typing import Dict, List, Pattern
import re
import asyncio
import logging
import os
from dotenv import load_dotenv
import openai
from .system_prompt import system_prompt, chat_title_blacklist
from datetime import datetime, timedelta

load_dotenv()

# Enhanced logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('telegram_monitor.log'),
        logging.StreamHandler()
    ]
)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

class MessageMonitor:
    def __init__(self, api_id: str, api_hash: str, phone: str, openai_api_key: str):
        self.client = TelegramClient('session_name', api_id, api_hash)
        self.phone = phone
        self.patterns: Dict[Pattern, callable] = {}
        self.logger = logger
        self.tg_username = None
        self.openai_api_key = openai_api_key
        self.stats = {
            'group_chat_replies': 0,
            'tagged_messages': 0,
            'private_chats': 0,
            'total_messages_processed': 0,
            'absinthe_group_messages': 0
        }
        self._schedule_daily_stats_reset()

    async def start(self):
        """Start the client and message monitoring"""
        await self.client.start(phone=self.phone)
        self.logger.info("Client started successfully")
        me = await self.client.get_me()
        self.tg_username = me.username
        self.logger.info(f"Logged in as {me.first_name}. Username: {self.tg_username}")

        @self.client.on(events.NewMessage)
        async def handle_new_message(event: events.NewMessage.Event):
            """Handle incoming messages and check against patterns"""
            self.stats['total_messages_processed'] += 1
            
            message_text = event.message.text
            group_chat = event.is_group
            mentioned = False
            is_private_chat = False

            if await self._check_mentions(event):
                self.stats['tagged_messages'] += 1
                chat = await event.get_chat()
                chat_name = chat.title if hasattr(chat, 'title') else f"Private chat with {chat.first_name}"
                sender = await event.get_sender()
                sender_name = sender.first_name
                mentioned = True
                self.logger.info(f"You were mentioned by {sender_name} in {chat_name}")
                self.logger.info(f"Message: {message_text}")

            if group_chat:
                chat_from = event.chat if event.chat else (await event.get_chat()) # telegram MAY not send the chat enity
                chat_title = chat_from.title
                if re.search(r'absinthe', chat_title, re.IGNORECASE) and chat_title not in chat_title_blacklist:
                    messages = []
                    async for msg in self.client.iter_messages(chat_from, limit=5):  # Adjust limit as needed
                        sender = await msg.get_sender()
                        sender_name = sender.first_name if sender else "Unknown"
                        messages.insert(0, f"{sender_name}: {msg.text}")  # Insert at beginning to maintain chronological order
                    self._call_gpt(messages)
                    self.stats['absinthe_group_messages'] += 1
                    self.logger.info(f"Absinthe group chat: {chat_title}")
            else:
                self.stats['private_chats'] += 1
                self.logger.info(f"Private chat with: {event.sender_id}")

            for pattern, callback in self.patterns.items():
                if re.search(pattern, message_text):
                    await callback(event)
                    
    async def _check_mentions(self, event: events.NewMessage.Event) -> bool:
        """Check if the user was mentioned in the message"""
        if not self.tg_username:
            return False
            
        message: Message = event.message
        # Check text mentions (@username)
        if message.text and f"@{self.tg_username}" in message.text.lower():
            return True
            
        # Check entity mentions (clickable mentions)
        if message.mentioned:
            return True
            
        return False
    
    async def _call_gpt(self, message_contexts: list[str]) -> str:
        """Call the GPT API with the message"""
        fmt_message_contexts = [{"role": "system", "content": f"{msg}"} for msg in message_contexts]
        messages = [{"role": "system", "content": f"{system_prompt}"}, *fmt_message_contexts]
        response = openai.ChatCompletion.create(
            api_key=self.openai_api_key,
            model="gpt-4o",
            messages=messages
        )
        return response.choices[0].message.content

    def add_pattern(self, pattern: str, callback: callable):
        """Add a new pattern and associated callback"""
        compiled_pattern = re.compile(pattern, re.IGNORECASE)
        self.patterns[compiled_pattern] = callback
        self.logger.info(f"Added new pattern: {pattern}")

    async def run(self):
        """Run the message monitor"""
        await self.start()
        await self.client.run_until_disconnected()

    async def _log_daily_stats(self):
        """Log the daily stats and reset counters"""
        self.logger.info(f"\n=== {datetime.now().strftime('%B %d %Y')} Daily Statistics ===")
        for metric, value in self.stats.items():
            self.logger.info(f"{metric.replace('_', ' ').title()}: {value}")
        self.logger.info("=====================\n")
        
        # Reset stats for the next day
        for key in self.stats:
            self.stats[key] = 0

    def _schedule_daily_stats_reset(self):
        """Schedule the daily stats reset at midnight"""
        async def _daily_stats_job():
            while True:
                now = datetime.now()
                next_midnight = (now + timedelta(days=1)).replace(
                    hour=0, minute=0, second=0, microsecond=0
                )
                seconds_until_midnight = (next_midnight - now).total_seconds()
                
                await asyncio.sleep(seconds_until_midnight)
                await self._log_daily_stats()

        asyncio.create_task(_daily_stats_job())

async def main():
    monitor = MessageMonitor(
        api_id=os.getenv('tg_app_id'),
        api_hash=os.getenv('tg_api_hash'),
        phone=os.getenv('tg_phone'),
        openai_api_key=os.getenv('openai_api_key')
    )

    # async def on_match(event):
    #     print(f"Matched message: {event.message.text}")
    #     print(f"From: {event.sender_id}")

    # # Add patterns to monitor
    # monitor.add_pattern(r'hello world', on_match)
    # monitor.add_pattern(r'test message', on_match)

    # Run the monitor
    await monitor.run()

if __name__ == "__main__":
    asyncio.run(main())