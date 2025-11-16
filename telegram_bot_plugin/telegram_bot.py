from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.error import RetryAfter
import db
import core
import asyncio
from logger import get_logger

# Get logger for this module
logger = get_logger(__name__)


async def hello(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        ver = db.get_parameter("version")
        await update.message.reply_text(
            f"Hello {update.effective_user.first_name}! Vinted-Notifications is running under version {ver}.\n"
        )
    except Exception as e:
        logger.error(f"Error in hello command: {str(e)}", exc_info=True)
        try:
            await update.message.reply_text(
                "An error occurred. Please try again later."
            )
        except Exception as e2:
            logger.error(f"Error sending error message: {str(e2)}")


class LeRobot:
    def __init__(self, queue):
        from telegram import Bot
        from telegram.ext import ApplicationBuilder, CommandHandler

        try:

            self.bot = Bot(db.get_parameter("telegram_token"))
            self.app = (
                ApplicationBuilder().token(db.get_parameter("telegram_token")).build()
            )

            # Create the item queue to send to telegram
            self.new_items_queue = queue

            # Handler verify if bot is running
            self.app.add_handler(CommandHandler("hello", hello))
            # Keyword handlers

            job_queue = self.app.job_queue
            # Set the commands
            job_queue.run_once(self.set_commands, when=1)
            # Every day we check for a new version
            job_queue.run_repeating(self.check_version, interval=86400, first=1)
            # Every second we check for new posts to send to telegram
            job_queue.run_once(self.check_telegram_queue, when=1)

            self.app.run_polling()
        except Exception as e:
            logger.error(f"Error initializing bot: {str(e)}", exc_info=True)


    ### TELEGRAM SPECIFIC FUNCTIONS ###

    async def send_new_post(self, owner_id, content, url, text, buy_url=None, buy_text=None):
        try:
            async with self.bot:
                chat_ID = db.get_user_telegram_chat_id(owner_id)
                buttons = [[InlineKeyboardButton(text=text, url=url)]]
                if buy_url and buy_text:
                    buttons.append([InlineKeyboardButton(text=buy_text, url=buy_url)])
                await self.bot.send_message(
                    chat_ID,
                    content,
                    parse_mode="HTML",
                    read_timeout=40,
                    write_timeout=40,
                    reply_markup=InlineKeyboardMarkup(buttons),
                )
        except RetryAfter as e:
            retry_after = e.retry_after
            logger.error(
                f"Flood control exceeded. Retrying in {retry_after + 2} seconds"
            )
            await asyncio.sleep(retry_after + 2)
            # Retry sending the message
            await self.send_new_post(content, url, text, buy_url, buy_text)
        except Exception as e:
            logger.error(f"Error sending new post: {str(e)}", exc_info=True)

    async def check_version(self, context: ContextTypes.DEFAULT_TYPE):
        try:
            # get latest version from the repository
            should_update, VER, latest_version, url = core.check_version()

            if not should_update:
                await self.send_new_post(
                    f"Version {latest_version} is now available. Please update the bot.",
                    url,
                    "Open Github",
                )
        except Exception as e:
            logger.error(f"Error checking for new version: {str(e)}", exc_info=True)

    async def check_telegram_queue(self, context: ContextTypes.DEFAULT_TYPE):
        try:
            while 1:
                if not self.new_items_queue.empty():
                    content, url, text, buy_url, buy_text, owner_id = self.new_items_queue.get()
                    await self.send_new_post(owner_id, content, url, text, buy_url, buy_text)
                else:
                    await asyncio.sleep(0.1)
                    pass
        except Exception as e:
            logger.error(f"Error checking telegram queue: {str(e)}", exc_info=True)

    async def set_commands(self, context: ContextTypes.DEFAULT_TYPE):
        try:
            await self.bot.set_my_commands(
                [
                    ("hello", "Verify if bot is running"),
                ]
            )
            logger.info("Bot commands set successfully")
        except Exception as e:
            logger.error(f"Error setting bot commands: {str(e)}", exc_info=True)
