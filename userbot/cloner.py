"""Создание чатов-клонов и пересылка в них сообщений."""

import asyncio
import html
import logging
import tempfile
import time

from telethon import TelegramClient, errors, utils
from telethon.tl.functions.channels import CreateChannelRequest
from telethon.tl.types import MessageMediaDocument, MessageMediaPhoto, MessageMediaWebPage

from .config import Config
from .storage import Storage

log = logging.getLogger(__name__)

# Ошибки, означающие, что клона больше нет или писать в него нельзя
# (его удалили, из него вышли и т.п.). Клон создаётся заново.
CLONE_GONE = (
    errors.ChannelPrivateError,
    errors.ChannelInvalidError,
    errors.ChatWriteForbiddenError,
    errors.ChatAdminRequiredError,
    errors.ChatIdInvalidError,
    errors.PeerIdInvalidError,
    errors.UserBannedInChannelError,
)


class SkipMessage(Exception):
    """Сообщение пересылать не нужно или невозможно — убрать из очереди."""


class Cloner:
    def __init__(self, client: TelegramClient, cfg: Config, store: Storage):
        self.client = client
        self.cfg = cfg
        self.store = store
        self.clone_ids = store.clone_ids()
        self._create_lock = asyncio.Lock()
        self._dialogs_loaded_at = 0.0
        self._too_many_until = 0.0

    async def resolve(self, peer_id: int):
        """Сущность по id. Если её нет в кэше сессии — один раз подгружаем диалоги."""
        try:
            return await self.client.get_input_entity(peer_id)
        except ValueError:
            if time.time() - self._dialogs_loaded_at < 600:
                raise
            log.info("Сущность %s не в кэше, загружаю список диалогов", peer_id)
            self._dialogs_loaded_at = time.time()
            await self.client.get_dialogs()
            return await self.client.get_input_entity(peer_id)

    async def ensure_clone(self, source_id: int):
        clone_id = self.store.get_clone(source_id)
        if clone_id is not None:
            try:
                return await self.resolve(clone_id)
            except ValueError:
                log.warning("Клон %s чата %s не найден, создаю новый", clone_id, source_id)
                self.forget_clone(source_id)

        async with self._create_lock:
            # пока ждали блокировку, клон мог создать другой вызов
            clone_id = self.store.get_clone(source_id)
            if clone_id is not None:
                return await self.resolve(clone_id)
            return await self._create_clone(source_id)

    async def _create_clone(self, source_id: int):
        if time.time() < self._too_many_until:
            raise SkipMessage("достигнут лимит Telegram на число каналов, клон не создан")

        try:
            source = await self.client.get_entity(source_id)
            name = utils.get_display_name(source) or str(source_id)
        except Exception:
            name = str(source_id)
        title = (self.cfg.clone_prefix + name)[:128]

        try:
            result = await self.client(CreateChannelRequest(
                title=title,
                about=f"Клон чата {name} (id {source_id}). Создан юзерботом.",
                megagroup=False,
            ))
        except errors.ChannelsTooMuchError:
            # не долбим API попытками, повторим через час
            self._too_many_until = time.time() + 3600
            log.error("Telegram не даёт создать ещё один канал (лимит на аккаунт). "
                      "Удалите лишние каналы или сузьте CHAT_TYPES / ONLY_CHATS.")
            raise SkipMessage("лимит Telegram на число каналов")

        channel = result.chats[0]
        clone_id = utils.get_peer_id(channel)
        self.store.set_clone(source_id, clone_id, title)
        self.clone_ids.add(clone_id)
        log.info("Создан клон «%s» (%s) для чата %s", title, clone_id, source_id)

        if self.cfg.archive_clones:
            try:
                await self.client.edit_folder(channel, 1)
            except Exception as e:
                log.debug("Не удалось убрать клон в архив: %r", e)
        return channel

    async def _clone_alive(self, clone) -> bool:
        try:
            await self.client.get_messages(clone, limit=1)
            return True
        except CLONE_GONE:
            return False

    def forget_clone(self, source_id: int):
        clone_id = self.store.get_clone(source_id)
        self.store.delete_clone(source_id)
        # id старого клона оставляем в clone_ids, чтобы его сообщения
        # никогда не начали клонироваться как обычный чат
        log.info("Клон %s чата %s забыт", clone_id, source_id)

    async def forward(self, source_id: int, msg_id: int, message=None) -> int:
        """Пересылает сообщение в клон. Возвращает id сообщения в клоне."""
        for attempt in range(2):
            clone = await self.ensure_clone(source_id)
            try:
                try:
                    source = await self.resolve(source_id)
                except ValueError:
                    raise SkipMessage("исходный чат недоступен")
                try:
                    result = await self.client.forward_messages(clone, msg_id, from_peer=source)
                except errors.ChatForwardsRestrictedError:
                    # в чате запрещена пересылка — копируем содержимое
                    if message is None:
                        message = await self.client.get_messages(source, ids=msg_id)
                    if message is None:
                        raise SkipMessage("сообщение удалено")
                    result = await self.copy(clone, message)
            except CLONE_GONE as e:
                # та же ошибка бывает, если недоступен исходный чат (например,
                # из него вышли). Клон пересоздаём, только если он и правда пропал.
                if await self._clone_alive(clone):
                    raise SkipMessage(f"исходный чат недоступен ({type(e).__name__})")
                if attempt == 0:
                    log.warning("Клон чата %s недоступен (%s), пересоздаю", source_id, type(e).__name__)
                    self.forget_clone(source_id)
                    continue
                raise
            except (errors.MessageIdInvalidError, errors.MediaEmptyError):
                raise SkipMessage("сообщение удалено или не может быть переслано")

            if isinstance(result, list):
                result = result[0] if result else None
            if result is None:
                raise SkipMessage("Telegram не вернул пересланное сообщение")
            return result.id
        raise RuntimeError("недостижимо")

    async def copy(self, clone, message):
        """Копирование для чатов с запретом пересылки: текст + скачанное медиа."""
        sender = None
        try:
            sender = await message.get_sender()
        except Exception:
            pass
        header = f"<b>{html.escape(utils.get_display_name(sender) or 'Неизвестный')}</b>\n" if sender else ""
        text = header + (message.text or "")

        media = message.media
        if media is None or isinstance(media, MessageMediaWebPage):
            return await self.client.send_message(clone, text or "[пустое сообщение]", parse_mode="html")

        if isinstance(media, (MessageMediaPhoto, MessageMediaDocument)):
            size = message.file.size if message.file and message.file.size else 0
            if size > self.cfg.copy_media_max_mb * 1024 * 1024:
                note = f"\n[медиа {size // (1024 * 1024)} МБ не скопировано: больше COPY_MEDIA_MAX_MB]"
                return await self.client.send_message(clone, text + note, parse_mode="html")

            tmp_root = self.cfg.data_dir / "tmp"
            tmp_root.mkdir(exist_ok=True)
            with tempfile.TemporaryDirectory(dir=tmp_root) as tmp:
                path = await self.client.download_media(message, file=tmp)
                if not path:
                    return await self.client.send_message(clone, text + "\n[медиа недоступно]", parse_mode="html")
                attributes = message.document.attributes if message.document else None
                caption = text if len(text) <= 1024 else ""
                sent = await self.client.send_file(
                    clone, path, caption=caption, parse_mode="html", attributes=attributes,
                    force_document=False,
                )
                if not caption and text:
                    await self.client.send_message(clone, text, parse_mode="html")
                return sent

        # геопозиция, контакт, кубик и т.п. — пробуем отправить как есть
        try:
            return await self.client.send_message(clone, text, file=media, parse_mode="html")
        except errors.RPCError:
            kind = type(media).__name__.replace("MessageMedia", "")
            return await self.client.send_message(clone, f"{text}\n[{kind}: не удалось скопировать]",
                                                  parse_mode="html")
