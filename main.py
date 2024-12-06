from telethon import TelegramClient, events, Button
from telethon.types import Message
from typing import Dict, List, Pattern
import re
import asyncio
import logging
import os
from dotenv import load_dotenv
from openai import AsyncOpenAI
from config import SYSTEM_PROMPT, CHAT_NAME_FILTER, CHAT_TITLE_BLACKLIST, GPT_MODEL
from datetime import datetime, timedelta
import aiosqlite

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
    async def _init_db(self):
        """Initialize SQLite database and create messages table"""
        async with aiosqlite.connect('telegram_monitor.db') as db:
            await db.execute('''
                CREATE TABLE IF NOT EXISTS message_tracking (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    chat_id INTEGER,
                    message_context TEXT,
                    gpt_response TEXT,
                    edited_text TEXT NULL,
                    action TEXT DEFAULT 'pending',
                    FOREIGN KEY (chat_id) REFERENCES chats(id)
                )
            ''')
            await db.commit()

    def __init__(self, api_id: str,  api_hash: str, phone: str, bot_token: str, openai_api_key: str):
        self.client = TelegramClient('user_session', api_id, api_hash)
        self.bot = TelegramClient('bot_session', api_id, api_hash)
        self.bot_token = bot_token
        self.phone = phone
        self.patterns: Dict[Pattern, callable] = {}
        self.logger = logger
        self.tg_username = None
        self.openai_api_key = openai_api_key
        self.openai_client = AsyncOpenAI(api_key=openai_api_key)
        self.pending_messages = {}  # Store pending messages waiting for approval
        self.stats = {
            'group_chat_replies': 0,
            'tagged_messages': 0,
            'private_chats': 0,
            'total_messages_processed': 0,
            'absinthe_group_messages': 0
        }
        self._schedule_daily_stats_reset()
        asyncio.create_task(self._init_db())

    async def start(self):
        """Start the client and message monitoring"""
        # start user client
        await self.client.start(phone=self.phone)
        self.logger.info("Client started successfully")
        me = await self.client.get_me()
        self.tg_username = me.username
        self.logger.info(f"Logged in as {me.first_name}. Username: {self.tg_username}")

        # start bot client
        await self.bot.start(bot_token=self.bot_token)
        bot_me = await self.bot.get_me()
        self.logger.info("Bot client started successfully")

         # Set up callback query handler for button clicks
        @self.bot.on(events.CallbackQuery)
        async def handle_callback(event):
            await self._handle_button_press(event)

        # Add message handler for bot to ignore other users
        @self.bot.on(events.NewMessage)
        async def handle_bot_messages(event):
            me = await self.client.get_me()
            if event.sender_id != me.id:
                    await event.reply("Sorry, I only respond to my owner.")
                    return

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
                chat_from = event.chat if event.chat else (await event.get_chat())
                chat_title = chat_from.title
                if re.search(CHAT_NAME_FILTER, chat_title, re.IGNORECASE) and chat_title not in CHAT_TITLE_BLACKLIST:
                    messages = []
                    # get the last 5 messages in the chat for context
                    async for msg in self.client.iter_messages(chat_from, limit=2):
                        sender = await msg.get_sender()
                        sender_name = sender.first_name if sender else "Unknown"
                        username = f"@{sender.username}" if sender and sender.username else "no_username"
                        timestamp = msg.date.strftime("%Y-%m-%d %H:%M:%S")
                        messages.insert(0, f"{username} [{timestamp}]: {msg.text}")
                        self.logger.info(f"{username} [{timestamp}]: {msg.text}")
                    gpt_response = await self._call_gpt(messages, chat_from.id)
                    self.logger.info(f"GPT response: {gpt_response}")

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
    
    async def _call_gpt(self, message_contexts: list[str], original_chat_id: int) -> str:
        """Call the GPT API with the message and send to bot for approval"""
        try:
            # Get your user ID first
            me = await self.client.get_me()
            
            messages = [{"role": "system", "content": SYSTEM_PROMPT}]
            
            for msg in message_contexts:
                sender, content = msg.split(": ", 1)
                role = "assistant" if sender == self.tg_username else "user"
                messages.append({"role": role, "content": content})

            response = await self.openai_client.chat.completions.create(
                model=GPT_MODEL,
                messages=messages
            )
            
            gpt_response = response.choices[0].message.content
            
            # Only proceed if we're sending to the bot owner
            if me and me.id:
                # Generate a unique ID for this message
                message_id = f"{original_chat_id}_{len(self.pending_messages)}"
                
                # Store the pending message
                self.pending_messages[message_id] = {
                    'response': gpt_response,
                    'chat_id': original_chat_id,
                    'context': message_contexts
                }
                
                # Create inline keyboard
                buttons = [
                    [
                        Button.inline("✅ Approve", f"approve_{message_id}"),
                        Button.inline("✏️ Edit", f"edit_{message_id}"),
                        Button.inline("❌ Reject", f"reject_{message_id}")
                    ]
                ]
                
                message = " New message to review:\n\n"
                message += "Context:\n"
                message += "\n".join(message_contexts)
                message += "\n\n📤 Proposed Response:\n"
                message += gpt_response
                
                await self.bot.send_message(me.id, message, buttons=buttons)
            
            return gpt_response
        except Exception as e:
            self.logger.error(f"Error in _call_gpt: {str(e)}")
            return f"Error generating response: {str(e)}"

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

    async def _handle_button_press(self, event):
        """Handle button presses for message approval/rejection/editing"""
        try:
            # Check if the user pressing the button is you
            me = await self.client.get_me()
            if event.sender_id != me.id:
                await event.answer("⚠️ You're not authorized to perform this action", alert=True)
                return

            data = event.data.decode()
            action, message_id = data.split('_', 1)
            
            if message_id not in self.pending_messages:
                await event.answer("Message no longer available")
                return
            
            message_data = self.pending_messages[message_id]
            
            if action == "approve":
                # Send the approved message
                await self.client.send_message(
                    message_data['chat_id'],
                    message_data['response']
                )
                await event.edit("✅ Message approved and sent!")
                del self.pending_messages[message_id]
            
            elif action == "edit":
                # Send the original GPT response in a separate message for easy copying
                await self.bot.send_message(
                    me.id,
                    f"EDIT: {message_data['response']}"
                )
                # Send the edit instruction message
                await event.edit("Please reply to this message with your edited version. Start with 'EDIT:' followed by your new message. Proposed response to copy")
                
                # Add a message handler for the edit response
                @self.bot.on(events.NewMessage(from_users=me.id))
                async def edit_handler(response_event):
                    if response_event.text.lower().startswith("edit:"):
                        # Remove the handler after we get our response
                        self.bot.remove_event_handler(edit_handler)
                        
                        # Get the edited message (remove the "EDIT:" prefix)
                        edited_message = response_event.text[5:].strip()
                        
                        # Update the pending message
                        message_data['response'] = edited_message
                        
                        # Show the new version with approve/reject buttons
                        buttons = [
                            [
                                Button.inline("✅ Approve", f"approve_{message_id}"),
                                Button.inline("✏️ Edit", f"edit_{message_id}"),
                                Button.inline("❌ Reject", f"reject_{message_id}")
                            ]
                        ]
                        
                        preview_message = "Updated message to review:\n\n"
                        preview_message += "Context:\n"
                        preview_message += "\n".join(message_data['context'])
                        preview_message += "\n\n📤 Proposed Response:\n"
                        preview_message += edited_message
                        
                        await response_event.delete()
                        await self.bot.send_message(me.id, preview_message, buttons=buttons)
            
            else:  # reject
                await event.edit("❌ Message rejected")
                del self.pending_messages[message_id]
            
        except Exception as e:
            self.logger.error(f"Error in button handler: {str(e)}")
            await event.answer("An error occurred while processing your request", alert=True)

async def main():
    monitor = MessageMonitor(
        api_id=os.getenv('tg_app_id'),
        api_hash=os.getenv('tg_api_hash'),
        phone=os.getenv('tg_phone'),
        bot_token=os.getenv('tg_bot_token'),
        openai_api_key=os.getenv('openai_api_key')
    )

    # Run the monitor
    await monitor.run()

if __name__ == "__main__":
    asyncio.run(main())