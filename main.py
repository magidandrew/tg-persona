from telethon import TelegramClient, events, Button
from telethon.types import Message
from typing import Dict, List, Pattern, Tuple
import re
import asyncio
import logging
import os
from dotenv import load_dotenv
from openai import AsyncOpenAI
from config import SYSTEM_PROMPT, CHAT_NAME_FILTER, CHAT_TITLE_BLACKLIST, GPT_MODEL, GPT_JSON_SCHEMA
from datetime import datetime, timedelta, time
import aiosqlite
import json
from telethon.tl import functions
from telethon.tl.types import InputPeerNotifySettings, InputNotifyPeer
import pickle

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
        """Initialize SQLite database and create necessary tables"""
        async with aiosqlite.connect('telegram_monitor.db') as db:
            # Existing messages table
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
            
            # New pending messages table
            await db.execute('''
                CREATE TABLE IF NOT EXISTS pending_messages (
                    message_id TEXT PRIMARY KEY,
                    chat_id INTEGER,
                    response TEXT,
                    context BLOB,  -- Will store pickled context list
                    confidence INTEGER,
                    urgency TEXT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            await db.commit()

    async def _save_pending_message(self, message_id: str, message_data: dict):
        """Save a pending message to the database"""
        async with aiosqlite.connect('telegram_monitor.db') as db:
            await db.execute('''
                INSERT OR REPLACE INTO pending_messages 
                (message_id, chat_id, response, context, confidence, urgency)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (
                message_id,
                message_data['chat_id'],
                message_data['response'],
                pickle.dumps(message_data['context']),
                message_data['confidence'],
                message_data['urgency']
            ))
            await db.commit()

    async def _load_pending_messages(self):
        """Load all pending messages from the database"""
        async with aiosqlite.connect('telegram_monitor.db') as db:
            async with db.execute('SELECT * FROM pending_messages') as cursor:
                rows = await cursor.fetchall()
                result = {}
                for row in rows:
                    result[row[0]] = {
                        'chat_id': row[1],
                        'response': row[2],
                        'context': pickle.loads(row[3]),
                        'confidence': row[4],
                        'urgency': row[5]
                    }
                return result

    async def _delete_pending_message(self, message_id: str):
        """Delete a pending message from the database"""
        async with aiosqlite.connect('telegram_monitor.db') as db:
            await db.execute('DELETE FROM pending_messages WHERE message_id = ?', (message_id,))
            await db.commit()

    def __init__(self, api_id: str,  api_hash: str, phone: str, bot_token: str, openai_api_key: str):
        self.client = TelegramClient('user_session', api_id, api_hash)
        self.bot = TelegramClient('bot_session', api_id, api_hash)
        self.delay_time_seconds = 1 # debug
        self.delay_check_interval_seconds = 1 # debug
        # self.delay_time_seconds = 120 
        # self.delay_check_interval_seconds = 15 
        self.max_unique_senders = 2
        self.bot_token = bot_token
        self.phone = phone
        self.patterns: Dict[Pattern, callable] = {}
        self.logger = logger
        self.tg_username = None
        self.openai_api_key = openai_api_key
        self.openai_client = AsyncOpenAI(api_key=openai_api_key)
        self.pending_messages = {}  # Will be populated after DB initialization
        self.stats = {
            'group_chat_replies': 0,
            'tagged_messages': 0,
            'private_chats': 0,
            'total_messages_processed': 0,
            'absinthe_group_messages': 0
        }
        self._schedule_daily_stats_reset()
        asyncio.create_task(self._init_and_load_db())
        self.last_message_times = {}  # Store last message time for each chat
        self.message_queues = {}      # Store queued messages for each chat
        self.processing_tasks = {}    # Store processing tasks for each chat
        self.delay_tasks = {}  # Store the delay tasks for each chat
        # asyncio.create_task(self._mute_matching_chats()) # fixme: for now, turned off
        self.notification_times = (time(1, 0), time(13, 0)) # in UTC time # 1 AM and 1 PM UTC
        self._schedule_pending_messages_notifications()

    async def _init_and_load_db(self):
        """Initialize database and load pending messages"""
        await self._init_db()
        self.pending_messages = await self._load_pending_messages()

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
                chat_id = chat_from.id

                if re.search(CHAT_NAME_FILTER, chat_title, re.IGNORECASE) and chat_title not in CHAT_TITLE_BLACKLIST:
                    # Get the last message sender before processing
                    async for msg in self.client.iter_messages(chat_from, limit=1):
                        sender = await msg.get_sender()
                        if sender and sender.username == self.tg_username:
                            self.logger.info("Last message was sent by me, ignoring...")
                            return

                    # Update last message time
                    current_time = datetime.now()
                    self.last_message_times[chat_id] = current_time

                    # Initialize message queue if needed
                    if chat_id not in self.message_queues:
                        self.message_queues[chat_id] = []

                    # Add message to queue
                    self.message_queues[chat_id].append({
                        'text': event.message.text,
                        'sender': await event.get_sender(),
                        'timestamp': current_time,
                        'chat': chat_from
                    })

                    if chat_id in self.delay_tasks:
                        # Cancel existing delay task
                        self.delay_tasks[chat_id].cancel()

                    # Create new delay task
                    self.delay_tasks[chat_id] = asyncio.create_task(self._delayed_processing(chat_id))
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
    
    async def _call_gpt(self, message_contexts: list[str], original_chat_id: int) -> Tuple[str, bool]:
        """Call the GPT API with the message and send to bot for approval"""
        try:
            # Get your user ID first
            me = await self.client.get_me()
            
            messages = [{"role": "system", "content": SYSTEM_PROMPT}]
            
            for msg in message_contexts:
                sender, content = msg.split(": ", 1)
                role = "assistant" if sender == self.tg_username else "user"
                messages.append({"role": role, "content": content})

            # call openai api
            response = await self.openai_client.beta.chat.completions.parse(
                model=GPT_MODEL,
                response_format=GPT_JSON_SCHEMA,
                messages=messages
            )
            
            raw_gpt_response = response.choices[0].message.content
            self.logger.info(f"Raw GPT response: {raw_gpt_response}")
            decoded_gpt_response = json.loads(raw_gpt_response)
            gpt_response = decoded_gpt_response['response']
            should_respond = True if decoded_gpt_response['should_respond'] else False
        
            if not should_respond:
                self.logger.info(f"Skipping response for {original_chat_id} because: {decoded_gpt_response['reason']}")
            
            # Only proceed if we're sending to the bot owner
            if me and me.id and should_respond: 
                message_id = f"{original_chat_id}_{datetime.now().timestamp()}"
                
                # Create message data
                message_data = {
                    'response': gpt_response,
                    'chat_id': original_chat_id,
                    'context': message_contexts,
                    'confidence': decoded_gpt_response['confidence'],
                    'urgency': decoded_gpt_response['urgency']
                }
                
                # Save to database
                await self._save_pending_message(message_id, message_data)
                self.pending_messages[message_id] = message_data
                
                # Create inline keyboard
                buttons = [
                    [
                        Button.inline("✅ Approve", f"approve_{message_id}"),
                        Button.inline("✏️ Edit", f"edit_{message_id}"),
                        Button.inline("❌ Reject", f"reject_{message_id}")
                    ]
                ]

                urgency = decoded_gpt_response['urgency']
                if urgency == 'high':
                    urgency_emoji = "🚨"
                elif urgency == 'medium':
                    urgency_emoji = "🟠"
                elif urgency == 'low':
                    urgency_emoji = "🟢"
                
                message = f"{urgency_emoji} New message to review:\n\n"
                message += "Context:\n"
                message += "\n".join(message_contexts)
                message += f"\n\n📤 Proposed Response: confidence <{decoded_gpt_response['confidence']}>\n"
                message += gpt_response
                
                await self.bot.send_message(me.id, message, buttons=buttons)
            
            return gpt_response, should_respond
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
                await self.client.send_message(
                    message_data['chat_id'],
                    message_data['response']
                )
                await event.edit("✅ Message approved and sent!")
                await self._delete_pending_message(message_id)
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
                await self._delete_pending_message(message_id)
                del self.pending_messages[message_id]
            
        except Exception as e:
            self.logger.error(f"Error in button handler: {str(e)}")
            await event.answer("An error occurred while processing your request", alert=True)

    async def _delayed_processing(self, chat_id: int):
        """Handle the delayed processing with reset capability"""
        try:
            while True:
                current_time = datetime.now()
                last_message_time = self.last_message_times.get(chat_id)
                
                if not last_message_time:
                    return
                    
                time_since_last = (current_time - last_message_time).total_seconds()
                
                if time_since_last >= self.delay_time_seconds:
                    if chat_id in self.message_queues and self.message_queues[chat_id]:
                        chat = self.message_queues[chat_id][0]['chat']
                        formatted_messages = []
                        unique_senders = []  # Changed from set to list
                        
                        # Fetch last 50 messages to get better context
                        async for message in self.client.iter_messages(chat, limit=50):
                            if message.sender and message.text:  # Only process text messages with senders
                                sender_username = message.sender.username if message.sender.username else str(message.sender.id)
                                
                                # Add sender if they're different from the last sender
                                if not unique_senders or sender_username != unique_senders[-1]:
                                    unique_senders.append(sender_username)
                                
                                # Include message if we haven't exceeded max unique senders
                                if len(unique_senders) <= self.max_unique_senders:
                                    timestamp = message.date.strftime("%Y-%m-%d %H:%M:%S")
                                    username = f"@{message.sender.username}" if message.sender.username else "no_username"
                                    formatted_messages.insert(0, f"sender_username <{username}> [{timestamp}]: {message.text}")
                                else:
                                    break

                        if formatted_messages:
                            self.logger.info(f"Formatted messages: {formatted_messages}")
                            gpt_response, should_respond = await self._call_gpt(formatted_messages, chat_id)
                            if should_respond:
                                self.logger.info(f"GPT response: {gpt_response}")

                        # Clear the queue after processing
                        self.message_queues[chat_id] = []
                        
                        # Clean up
                        if chat_id in self.delay_tasks:
                            del self.delay_tasks[chat_id]
                        return
                    
                await asyncio.sleep(self.delay_check_interval_seconds)

        except asyncio.CancelledError:
            self.logger.info(f"Delayed processing cancelled for chat {chat_id}")
            raise
        except Exception as e:
            self.logger.error(f"Error in delayed processing: {str(e)}")

    async def _mute_matching_chats(self):
        await asyncio.sleep(5)  # Initial delay to let other initialization complete
        """Continuously mute all chats matching the CHAT_NAME_FILTER pattern"""
        try:
            # Get matching chats once
            matching_chats = []
            async for dialog in self.client.iter_dialogs():
                if dialog.is_group and re.search(CHAT_NAME_FILTER, dialog.title, re.IGNORECASE):
                    matching_chats.append(dialog)
                    self.logger.info(f"Found matching chat to mute: {dialog.title}")

            while True:
                for dialog in matching_chats:
                    try:
                        # Create notification settings for muting
                        settings = InputPeerNotifySettings(
                            show_previews=False,
                            silent=True,
                            mute_until=int((datetime.now() + timedelta(seconds=10)).timestamp()),
                            sound=None
                        )
                        
                        # Create the peer for the notification update
                        peer = InputNotifyPeer(
                            peer=dialog.input_entity
                        )
                        
                        # Update notification settings
                        await self.client(functions.account.UpdateNotifySettingsRequest(
                            peer=peer,
                            settings=settings
                        ))
                        
                        self.logger.info(f"Muted chat: {dialog.title}")
                    except Exception as e:
                        self.logger.error(f"Error muting chat {dialog.title}: {str(e)}")
                
                # Wait before the next muting cycle
                await asyncio.sleep(30)  # Increased to 30 seconds
                
        except Exception as e:
            self.logger.error(f"Error in mute job: {str(e)}")
            # Restart the task if it fails
            asyncio.create_task(self._mute_matching_chats())

    def _schedule_pending_messages_notifications(self):
        """Schedule notifications about pending messages at specified times"""
        async def _notification_job():
            while True:
                now = datetime.now()
                today_times = [datetime.combine(now.date(), t) for t in self.notification_times]
                
                next_time = None
                for t in today_times:
                    if now < t:
                        next_time = t
                        break
                if not next_time:
                    tomorrow = now.date() + timedelta(days=1)
                    next_time = datetime.combine(tomorrow, self.notification_times[0])
                
                seconds_until_next = (next_time - now).total_seconds()
                await asyncio.sleep(seconds_until_next)
                await self._send_pending_messages_summary()

        asyncio.create_task(_notification_job())

    async def _send_pending_messages_summary(self):
        """Send a summary of pending messages grouped by urgency"""
        try:
            me = await self.client.get_me()
            if not me:
                return

            # Count messages by urgency
            urgency_counts = {'high': 0, 'medium': 0, 'low': 0}
            for msg_data in self.pending_messages.values():
                urgency = msg_data.get('urgency', 'low')
                urgency_counts[urgency] += 1

            # Create summary message
            current_time = datetime.now().strftime('%I:%M %p')
            summary = f"📊 Pending Messages Summary ({current_time})\n\n"
            summary += f"🚨 Urgent: {urgency_counts['high']}\n"
            summary += f"🟠 Medium: {urgency_counts['medium']}\n"
            summary += f"🟢 Low: {urgency_counts['low']}\n"
            
            total = sum(urgency_counts.values())
            summary += f"\nTotal pending: {total}"

            if total > 0:
                await self.bot.send_message(me.id, summary)
            
        except Exception as e:
            self.logger.error(f"Error sending pending messages summary: {str(e)}")

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