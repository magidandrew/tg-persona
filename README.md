# Telegram Message Monitor Bot
A Telegram bot that monitors messages in groups and private chats, processes them through GPT-4, and maintains statistics. Specialized handling for Absinthe-related group chats.

## Requirements
- Python 3.x
- Telegram API credentials (api_id, api_hash)
- OpenAI API key
- Telegram phone number
- A bot created via BotFather

## Quick Start
1. Clone and install:
```bash
git clone <repository-url>
cd <repository-name>
pip3 install -r requirements.txt
```
2. Create a telegram app on [https://my.telegram.org/apps](https://my.telegram.org/apps)
   1. Copy the key into your .env file
3. Create a bot on botfather.
   1. Copy the key into your .env file
4. Create an api key on [https://platform.openai.com/api-keys](https://platform.openai.com/api-keys)
5. Run `python3 main.py`. Provide your OTP and password if it requests it.

The bot will:
- Monitor all incoming messages
- Process messages in Absinthe-related groups
- Track when you're mentioned
- Generate responses using GPT-4o
- Log daily statistics at midnight in the logfile

## Statistics Tracked

- Group chat replies
- Tagged messages
- Private chats
- Total messages processed
- Absinthe group messages
- OpenAI API calls

2. Create `.env` file:
```env
tg_app_id=your_telegram_app_id
tg_api_hash=your_telegram_api_hash
tg_phone=your_telegram_phone_number
openai_api_key=your_openai_api_key
```

3. Run:
```bash
python3 main.py
```

## Statistics
View daily stats with:
```bash
grep -i "december 24 2024" telegram_monitor.log
```

## Configuration
Modify `system_prompt.py` to adjust GPT behavior and chat title blacklist.