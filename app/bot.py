from __future__ import annotations

import logging

from telegram import Update
from telegram.constants import ChatAction
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from app.company_context import load_company_content
from app.config import Settings
from app.database import Database, Membership, User
from app.prompts import build_system_prompt
from app.providers import AIProvider
from app.role_profiles import load_role_profiles, resolve_communication_profile
from app.telegram_renderer import markdown_to_telegram_html

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
        application.add_handler(CommandHandler("company", self.company))
        application.add_handler(CommandHandler("reset", self.reset))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.chat))
        return application

    def _authorized_user(self, update: Update):
        telegram_user = update.effective_user
        if telegram_user is None:
            return None
        return self.database.get_user(telegram_user.id)

    def _communication_profile(self, user: User, membership: Membership):
        profiles = load_role_profiles(self.settings.role_profiles_file)
        return resolve_communication_profile(
            membership.communication_profile or user.communication_profile,
            membership.job_title or membership.role_level or user.role,
            profiles,
        )

    def _active_membership(self, user: User) -> Membership | None:
        return self.database.get_active_membership(user.telegram_id)

    def _company_list_text(self, user: User) -> str:
        memberships = self.database.list_memberships(user.telegram_id)
        if not memberships:
            return "Akunmu belum mempunyai akses perusahaan. Hubungi admin."
        active = self.database.get_active_membership(user.telegram_id)
        lines = ["Pilih perusahaan aktif:"]
        for membership in memberships:
            marker = "✓ " if active and active.company_id == membership.company_id else ""
            lines.append(
                f"{marker}/company {membership.company_id} — {membership.company_name}"
            )
        return "\n".join(lines)

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
        membership = self._active_membership(user)
        company_text = (
            f" Perusahaan aktif: {membership.company_name}."
            if membership
            else " Pilih perusahaan dengan /company sebelum mulai."
        )
        await update.effective_message.reply_text(
            f"Halo {user.name}. Saya siap membantu sebagai asisten internal."
            f"{company_text} Ketik /help untuk melihat perintah."
        )

    async def help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized_user(update):
            await self._reject(update)
            return
        await update.effective_message.reply_text(
            "/whoami — lihat profil akses\n"
            "/company — lihat atau ganti perusahaan aktif\n"
            "/reset — hapus konteks percakapan\n"
            "/help — daftar perintah"
        )

    async def whoami(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = self._authorized_user(update)
        if not user:
            await self._reject(update)
            return
        membership = self._active_membership(user)
        if membership is None:
            await update.effective_message.reply_text(self._company_list_text(user))
            return
        profile = self._communication_profile(user, membership)
        await update.effective_message.reply_text(
            f"Nama: {user.name}\n"
            f"Perusahaan aktif: {membership.company_name}\n"
            f"Company ID: {membership.company_id}\n"
            f"Jabatan: {membership.job_title or '-'}\n"
            f"Division: {membership.division or '-'}\n"
            f"Role level: {membership.role_level or '-'}\n"
            f"Communication profile: {profile.label}"
        )

    async def company(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = self._authorized_user(update)
        if not user:
            await self._reject(update)
            return
        if not context.args:
            await update.effective_message.reply_text(self._company_list_text(user))
            return

        company_id = context.args[0].strip().casefold()
        membership = self.database.set_active_company(user.telegram_id, company_id)
        if membership is None:
            await update.effective_message.reply_text(
                "Perusahaan tidak ditemukan atau aksesmu tidak tersedia.\n\n"
                + self._company_list_text(user)
            )
            return
        await update.effective_message.reply_text(
            f"Perusahaan aktif diubah ke {membership.company_name}. "
            "History dan knowledge berikutnya akan memakai konteks perusahaan ini."
        )

    async def reset(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = self._authorized_user(update)
        if not user:
            await self._reject(update)
            return
        membership = self._active_membership(user)
        if membership is None:
            await update.effective_message.reply_text(self._company_list_text(user))
            return
        self.database.clear_history(user.telegram_id, membership.company_id)
        await update.effective_message.reply_text(
            f"Riwayat percakapan untuk {membership.company_name} sudah dihapus."
        )

    async def chat(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = self._authorized_user(update)
        if not user:
            await self._reject(update)
            return
        membership = self._active_membership(user)
        if membership is None:
            await update.effective_message.reply_text(self._company_list_text(user))
            return
        company = self.database.get_company(membership.company_id)
        if company is None:
            logger.error(
                "Company aktif %s tidak ditemukan untuk user %s",
                membership.company_id,
                user.telegram_id,
            )
            await update.effective_message.reply_text(
                "Konfigurasi perusahaan aktif tidak tersedia. Hubungi admin."
            )
            return

        message = update.effective_message
        if message is None or not message.text:
            return

        await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
        user_text = message.text.strip()
        history = self.database.get_history(
            user.telegram_id,
            self.settings.history_limit,
            membership.company_id,
        )
        company_content = load_company_content(
            company,
            self.settings.project_root,
            self.settings.knowledge_max_chars,
        )
        communication_profile = self._communication_profile(user, membership)
        system_prompt = build_system_prompt(
            user,
            membership,
            company,
            company_content,
            communication_profile,
        )

        try:
            answer = await self.provider.generate(system_prompt, history, user_text)
            answer = answer[: self.settings.max_response_chars]
            self.database.add_message(
                user.telegram_id, "user", user_text, membership.company_id
            )
            self.database.add_message(
                user.telegram_id, "assistant", answer, membership.company_id
            )
            self.database.prune_history(
                user.telegram_id, membership.company_id
            )
            for chunk in _split_message(answer, size=3500):
                rendered = markdown_to_telegram_html(chunk)
                try:
                    await message.reply_text(
                        rendered,
                        parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True,
                    )
                except BadRequest:
                    logger.warning(
                        "Format HTML ditolak Telegram untuk user %s; fallback plain text",
                        user.telegram_id,
                    )
                    await message.reply_text(
                        chunk,
                        disable_web_page_preview=True,
                    )
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
