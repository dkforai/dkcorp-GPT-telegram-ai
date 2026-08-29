from __future__ import annotations

import logging

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from app.config import Settings
from app.database import Database
from app.knowledge import load_knowledge
from app.prompts import build_system_prompt
from app.providers import AIProvider

logger = logging.getLogger(__name__)


class InternalBot:
    def __init__(self, settings: Settings, database: Database, provider: AIProvider):
        self.settings = settings
        self.database = database
        self.provider = provider

    def build_application(self) -> Application:
        application = Application.builder().token(self.settings.telegram_bot_token).build()
        application.add_handler(CommandHandler("start", self.start))
        application.add_handler(CommandHandler("help", self.help))
        application.add_handler(CommandHandler("whoami", self.whoami))
        application.add_handler(CommandHandler("reset", self.reset))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.chat))
        return application

    def _authorized_user(self, update: Update):
        telegram_user = update.effective_user
        if telegram_user is None:
            return None
        return self.database.get_user(telegram_user.id)

    async def _reject(self, update: Update) -> None:
        telegram_id = update.effective_user.id if update.effective_user else "unknown"
        logger.warning("Akses ditolak untuk Telegram ID %s", telegram_id)
        if update.effective_message:
            await update.effective_message.reply_text(
                f"Akses belum terdaftar. Kirim Telegram ID ini ke admin: {telegram_id}"
            )

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = self._authorized_user(update)
        if not user:
            await self._reject(update)
            return
        await update.effective_message.reply_text(
            f"Halo {user.name}. Saya siap membantu sebagai asisten internal. "
            "Ketik /help untuk melihat perintah."
        )

    async def help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized_user(update):
            await self._reject(update)
            return
        await update.effective_message.reply_text(
            "/whoami — lihat profil akses\n"
            "/reset — hapus konteks percakapan\n"
            "/help — daftar perintah"
        )

    async def whoami(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = self._authorized_user(update)
        if not user:
            await self._reject(update)
            return
        await update.effective_message.reply_text(
            f"Nama: {user.name}\nRole: {user.role or '-'}\nDivision: {user.division or '-'}"
        )

    async def reset(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = self._authorized_user(update)
        if not user:
            await self._reject(update)
            return
        self.database.clear_history(user.telegram_id)
        await update.effective_message.reply_text("Riwayat percakapan sudah dihapus.")

    async def chat(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = self._authorized_user(update)
        if not user:
            await self._reject(update)
            return

        message = update.effective_message
        if message is None or not message.text:
            return

        await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
        user_text = message.text.strip()
        history = self.database.get_history(user.telegram_id, self.settings.history_limit)
        knowledge = load_knowledge(
            self.settings.knowledge_dir, self.settings.knowledge_max_chars
        )
        system_prompt = build_system_prompt(user, knowledge)

        try:
            answer = await self.provider.generate(system_prompt, history, user_text)
            answer = answer[: self.settings.max_response_chars]
            self.database.add_message(user.telegram_id, "user", user_text)
            self.database.add_message(user.telegram_id, "assistant", answer)
            self.database.prune_history(user.telegram_id)
            for chunk in _split_message(answer):
                await message.reply_text(chunk)
        except Exception:
            logger.exception("Gagal memproses chat untuk Telegram ID %s", user.telegram_id)
            await message.reply_text(
                "Maaf, AI sedang gagal memproses pesan. Coba lagi atau hubungi admin."
            )


def _split_message(text: str, size: int = 4000) -> list[str]:
    chunks: list[str] = []
    remaining = text
    while len(remaining) > size:
        split_at = remaining.rfind("\n", 0, size)
        if split_at < size // 2:
            split_at = size
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks

