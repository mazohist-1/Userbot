"""Чаты-клоны и копирование в них сообщений.

Лички копируются в один клон «Private Messages», группы — каждая в свой
клон «Clone: <название>». Сообщение копируется целиком (текст, форматирование,
медиа, альбом) и получает шапку: от кого, ID, группа, тип.
"""

import asyncio
import copy
import logging
import tempfile
import time

from telethon import TelegramClient, errors, utils
from telethon.helpers import add_surrogate
from telethon.tl.functions.channels import CreateChannelRequest
from telethon.tl.types import MessageEntityBold, MessageEntityCode, MessageMediaWebPage

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

# Эти ошибки отправки пробрасываются наверх как есть: их обрабатывает очередь.
PASS_THROUGH = CLONE_GONE + (
    errors.FloodWaitError,
    errors.SlowModeWaitError,
    errors.AuthKeyError,
    errors.UnauthorizedError,
)

KIND_LABELS = {
    "text": "текст",
    "photo": "фото",
    "video": "видео",
    "document": "документ",
    "voice": "голосовое",
    "round": "кружок",
    "sticker": "стикер",
    "gif": "GIF",
    "audio": "аудио",
    "album": "альбом",
    "other": "другое",
}
# медиа, к которому нельзя приложить подпись: шапка уходит отдельным сообщением
NO_CAPTION = {"sticker", "round", "other"}

TEXT_LIMIT = 4096
CAPTION_LIMIT = 1024


class SkipMessage(Exception):
    """Сообщение пересылать не нужно или невозможно — убрать из очереди."""


def media_kind(message) -> str:
    if message.media is None or isinstance(message.media, MessageMediaWebPage):
        return "text"
    for kind, attr in (("sticker", "sticker"), ("round", "video_note"), ("voice", "voice"),
                       ("gif", "gif"), ("video", "video"), ("audio", "audio"),
                       ("photo", "photo"), ("document", "document")):
        if getattr(message, attr, None):
            return kind
    return "other"


def u16(s: str) -> int:
    return len(add_surrogate(s))


class Rich:
    """Текст с форматированием (entities), собираемый по кускам."""

    def __init__(self):
        self.text = ""
        self.entities = []

    def add(self, s: str, entity=None):
        if s and entity:
            self.entities.append(entity(offset=u16(self.text), length=u16(s)))
        self.text += s
        return self

    def extend(self, text: str, entities):
        shift = u16(self.text)
        for e in entities or ():
            e = copy.copy(e)
            e.offset += shift
            self.entities.append(e)
        self.text += text
        return self

    def __add__(self, other: "Rich") -> "Rich":
        return Rich().extend(self.text, self.entities).extend(other.text, other.entities)

    def __len__(self):
        return u16(self.text)


def body_of(message) -> Rich:
    return Rich().extend(message.message or "", message.entities)


def units(messages: list) -> list:
    """Разбивает список сообщений на единицы отправки: альбом или одиночное."""
    out = []
    for m in messages:
        gid = getattr(m, "grouped_id", None)
        if gid and out and getattr(out[-1][0], "grouped_id", None) == gid and len(out[-1]) < 10:
            out[-1].append(m)
        else:
            out.append([m])
    return out


class Cloner:
    def __init__(self, client: TelegramClient, cfg: Config, store: Storage):
        self.client = client
        self.cfg = cfg
        self.store = store
        self.clone_ids = store.clone_ids()
        self._create_lock = asyncio.Lock()
        self._dialogs_loaded_at = 0.0
        self._too_many_until = 0.0

    # --- сущности ----------------------------------------------------------

    async def _load_dialogs(self) -> bool:
        if time.time() - self._dialogs_loaded_at < 600:
            return False
        self._dialogs_loaded_at = time.time()
        await self.client.get_dialogs()
        return True

    async def resolve(self, peer_id):
        """Сущность по id. Если её нет в кэше сессии — один раз подгружаем диалоги."""
        try:
            return await self.client.get_input_entity(peer_id)
        except ValueError:
            log.info("Сущность %s не в кэше, загружаю список диалогов", peer_id)
            if not await self._load_dialogs():
                raise
            return await self.client.get_input_entity(peer_id)

    async def entity(self, peer):
        """Полная сущность (с названием) по id или @username."""
        try:
            return await self.client.get_entity(peer)
        except ValueError:
            if isinstance(peer, str) or not await self._load_dialogs():
                raise
            return await self.client.get_entity(peer)

    # --- клон личек --------------------------------------------------------

    async def ensure_private_clone(self):
        clone_id = self.store.private_clone()
        if clone_id is not None:
            try:
                return await self.resolve(clone_id)
            except ValueError:
                log.warning("Клон личек %s не найден, ищу или создаю заново", clone_id)
                self.forget_private_clone()
        async with self._create_lock:
            clone_id = self.store.private_clone()
            if clone_id is not None:
                return await self.resolve(clone_id)
            title = self.cfg.private_clone_title
            channel = await self._find_own_channel(title)
            if channel is not None:
                log.info("Найден клон личек «%s» (%s)", title, utils.get_peer_id(channel))
            else:
                channel = await self._create_channel(title, "Сюда юзербот копирует все входящие личные сообщения.")
            clone_id = utils.get_peer_id(channel)
            self.store.set_private_clone(clone_id)
            self.clone_ids.add(clone_id)
            return channel

    def forget_private_clone(self):
        clone_id = self.store.private_clone()
        if clone_id is not None:
            self.store.add_legacy(clone_id)
        self.store.set_private_clone(None)

    async def _find_own_channel(self, title: str):
        async for d in self.client.iter_dialogs():
            e = d.entity
            if d.is_channel and getattr(e, "creator", False) and getattr(e, "title", None) == title:
                return e
        return None

    async def _create_channel(self, title: str, about: str, archive: bool = False):
        if time.time() < self._too_many_until:
            raise SkipMessage("достигнут лимит Telegram на число каналов, клон не создан")
        try:
            result = await self.client(CreateChannelRequest(title=title[:128], about=about[:255], megagroup=False))
        except errors.ChannelsTooMuchError:
            self._too_many_until = time.time() + 3600  # не долбим API, повторим через час
            log.error("Telegram не даёт создать ещё один канал (лимит на аккаунт). Удалите лишние каналы.")
            raise SkipMessage("лимит Telegram на число каналов")
        channel = result.chats[0]
        log.info("Создан канал «%s» (%s)", title, utils.get_peer_id(channel))
        if archive:
            try:
                await self.client.edit_folder(channel, 1)
            except Exception as e:
                log.debug("Не удалось убрать клон в архив: %r", e)
        return channel

    # --- клоны групп -------------------------------------------------------

    async def create_group_clone(self, source_id: int, name: str = None):
        """Создаёт клон группы и запоминает его. Возвращает (канал, название)."""
        if name is None:
            try:
                name = utils.get_display_name(await self.entity(source_id)) or str(source_id)
            except Exception:
                name = str(source_id)
        title = (self.cfg.group_clone_prefix + name)[:128]
        async with self._create_lock:
            channel = await self._create_channel(title, f"Клон чата {name} (id {source_id}). Создан юзерботом.",
                                                 archive=self.cfg.archive_clones)
            clone_id = utils.get_peer_id(channel)
            self.store.set_clone(source_id, clone_id, title)
            self.clone_ids.add(clone_id)
        return channel, title

    async def _group_clone(self, source_id: int):
        clone_id = self.store.get_clone(source_id)
        if clone_id is not None:
            try:
                return await self.resolve(clone_id)
            except ValueError:
                log.warning("Клон %s чата %s не найден, создаю новый", clone_id, source_id)
                self.store.delete_clone(source_id)
        channel, _ = await self.create_group_clone(source_id)
        return channel

    async def _clone_alive(self, clone) -> bool:
        try:
            await self.client.get_messages(clone, limit=1)
            return True
        except CLONE_GONE:
            return False

    # --- пересылка из очереди ----------------------------------------------

    async def album_of(self, chat, message) -> list:
        """Все сообщения альбома, к которому относится message (по порядку)."""
        if not message.grouped_id:
            return [message]
        ids = list(range(max(1, message.id - 9), message.id + 10))
        found = await self.client.get_messages(chat, ids=ids)
        album = [m for m in found if m is not None and m.grouped_id == message.grouped_id]
        return sorted(album, key=lambda m: m.id) or [message]

    async def clone_message(self, source_id: int, msg_id: int, message=None) -> list:
        """Копирует сообщение (или весь его альбом) в нужный клон.
        Возвращает [(id исходного сообщения, id в клоне)]."""
        try:
            source = await self.resolve(source_id)
        except ValueError:
            raise SkipMessage("исходный чат недоступен")
        try:
            if message is None:
                message = await self.client.get_messages(source, ids=msg_id)
            if message is None:
                raise SkipMessage("сообщение удалено")
            messages = await self.album_of(source, message)
        except CLONE_GONE as e:
            raise SkipMessage(f"исходный чат недоступен ({type(e).__name__})")

        is_group = self.store.get_clone(source_id) is not None
        if not is_group:
            if message.is_private:
                pass
            elif self.cfg.auto_create_clones and message.is_group:
                is_group = True
            else:
                raise SkipMessage("чат больше не клонируется")

        header = await self.header(message, source_id, group=is_group, count=len(messages))

        for attempt in range(2):
            clone = await (self._group_clone(source_id) if is_group else self.ensure_private_clone())
            try:
                first_id = await self.send_copy(clone, messages, header)
            except CLONE_GONE as e:
                if await self._clone_alive(clone):
                    raise SkipMessage(f"не удалось отправить в клон ({type(e).__name__})")
                if attempt == 0:
                    log.warning("Клон для %s недоступен (%s), пересоздаю", source_id, type(e).__name__)
                    if is_group:
                        self.store.delete_clone(source_id)
                    else:
                        self.forget_private_clone()
                    continue
                raise
            return [(m.id, first_id) for m in messages]
        raise RuntimeError("недостижимо")

    # --- шапки -------------------------------------------------------------

    @staticmethod
    def _who(entity) -> str:
        name = utils.get_display_name(entity) if entity else ""
        return name or "Неизвестный"

    async def header(self, message, chat_id: int, group: bool, count: int = 1) -> Rich:
        sender = chat = None
        try:
            sender = await message.get_sender()
        except Exception:
            pass
        if group or message.out:
            try:
                chat = await message.get_chat()
            except Exception:
                pass

        h = Rich()
        if message.is_private and message.out:
            h.add("📤 Вы → ").add(self._who(chat), MessageEntityBold)
            h.add(" · ID ").add(str(chat_id), MessageEntityCode)
        else:
            h.add("👤 ").add(self._who(sender), MessageEntityBold)
            username = getattr(sender, "username", None)
            if username:
                h.add(f" (@{username})")
            h.add(" · ID ").add(str(message.sender_id or chat_id), MessageEntityCode)
        if group:
            h.add("\n👥 ").add(self._who(chat) if chat else str(chat_id), MessageEntityBold)
            h.add(" · ID ").add(str(chat_id), MessageEntityCode)
        kind = "album" if count > 1 else media_kind(message)
        label = KIND_LABELS[kind] + (f" ({count})" if count > 1 else "")
        h.add(f"\n📎 {label}")
        if message.fwd_from:
            h.add(" · ↪️ переслано")
        return h

    async def short_header(self, message, chat=None) -> Rich:
        """Шапка для .get: откуда и от кого."""
        sender = None
        try:
            sender = await message.get_sender()
        except Exception:
            pass
        h = Rich()
        if chat is not None:
            h.add("📥 ").add(self._who(chat), MessageEntityBold).add(" · ")
        h.add("👤 " + self._who(sender) + f" · #{message.id}")
        return h

    # --- копирование -------------------------------------------------------

    async def send_copy(self, target, messages: list, header: Rich, reply_to=None) -> int:
        """Отправляет копию сообщения (или альбома) с шапкой. Возвращает id
        первого отправленного сообщения."""
        if len(messages) > 1:
            return await self._send_album(target, messages, header, reply_to)
        message = messages[0]
        kind = media_kind(message)
        body = body_of(message)
        sep = Rich().add("\n\n") if body.text else Rich()

        if kind == "text":
            full = header + sep + body
            if len(full) <= TEXT_LIMIT:
                sent = await self._send_text(target, full, reply_to, preview=message.web_preview is not None)
                return sent.id
            first = await self._send_text(target, header, reply_to)
            await self._send_text(target, body, first.id, preview=message.web_preview is not None)
            return first.id

        if kind in NO_CAPTION:
            first = await self._send_text(target, header, reply_to)
            await self._send_media(target, message, Rich(), first.id)
            return first.id

        full = header + sep + body
        if len(full) <= CAPTION_LIMIT:
            sent = await self._send_media(target, message, full, reply_to)
            return sent.id
        sent = await self._send_media(target, message, header, reply_to)
        await self._send_text(target, body, sent.id)
        return sent.id

    async def _send_text(self, target, rich: Rich, reply_to=None, preview=False):
        return await self.client.send_message(
            target, rich.text or "[пустое сообщение]", formatting_entities=rich.entities, parse_mode=None,
            reply_to=reply_to, link_preview=preview)

    async def _send_media(self, target, message, caption: Rich, reply_to=None):
        try:
            # медиа по ссылке на файл Telegram: без скачивания, с сохранением типа
            return await self.client.send_file(
                target, message.media, caption=caption.text, formatting_entities=caption.entities, parse_mode=None,
                reply_to=reply_to)
        except PASS_THROUGH:
            raise
        except (errors.RPCError, TypeError) as e:
            # запрет пересылки в чате, устаревшая ссылка на файл, медиа без
            # прямого аналога для отправки (TypeError) и т.п.
            log.debug("Медиа по ссылке не отправилось (%r), скачиваю", e)
        return await self._reupload(target, message, caption, reply_to)

    def _too_big(self, message) -> int:
        size = message.file.size if message.file and message.file.size else 0
        return size if size > self.cfg.copy_media_max_mb * 1024 * 1024 else 0

    async def _reupload(self, target, message, caption: Rich, reply_to=None):
        kind = KIND_LABELS[media_kind(message)]
        size = self._too_big(message)
        if size:
            note = Rich().add(f"[{kind} {size // (1024 * 1024)} МБ не скопировано: больше COPY_MEDIA_MAX_MB]")
            return await self._send_text(target, caption + Rich().add("\n") + note if caption.text else note, reply_to)
        tmp_root = self.cfg.data_dir / "tmp"
        tmp_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=tmp_root) as tmp:
            path = await self.client.download_media(message, file=tmp)
            if path:
                try:
                    return await self.client.send_file(
                        target, path, caption=caption.text, formatting_entities=caption.entities, parse_mode=None,
                        reply_to=reply_to, attributes=self._attributes(message),
                        voice_note=bool(message.voice), video_note=bool(message.video_note))
                except PASS_THROUGH:
                    raise
                except errors.RPCError as e:
                    log.warning("Не удалось загрузить медиа заново: %r", e)
        note = Rich().add(f"[{kind}: не удалось скопировать]")
        return await self._send_text(target, caption + Rich().add("\n") + note if caption.text else note, reply_to)

    @staticmethod
    def _attributes(message):
        if not message.document:
            return None
        # атрибут стикера ссылается на набор и при повторной загрузке не принимается
        return [a for a in message.document.attributes if type(a).__name__ != "DocumentAttributeSticker"]

    async def _send_album(self, target, messages: list, header: Rich, reply_to=None) -> int:
        bodies = [body_of(m) for m in messages]
        first = header + (Rich().add("\n\n") + bodies[0] if bodies[0].text else Rich())
        extra = None
        if len(first) > CAPTION_LIMIT:
            first, extra = header, bodies[0]
        captions = [first] + bodies[1:]
        try:
            sent = await self._album_request(target, [m.media for m in messages], captions, reply_to)
        except PASS_THROUGH:
            raise
        except (errors.RPCError, TypeError) as e:
            log.debug("Альбом по ссылке не отправился (%r), скачиваю", e)
            sent = await self._reupload_album(target, messages, captions, reply_to)
        first_id = sent[0].id if isinstance(sent, list) else sent.id
        if extra is not None:
            await self._send_text(target, extra, first_id)
        return first_id

    async def _album_request(self, target, files, captions, reply_to):
        return await self.client.send_file(
            target, files, caption=[c.text for c in captions],
            formatting_entities=[c.entities for c in captions], parse_mode=None, reply_to=reply_to)

    async def _reupload_album(self, target, messages, captions, reply_to):
        tmp_root = self.cfg.data_dir / "tmp"
        tmp_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=tmp_root) as tmp:
            files, kept = [], []
            for m, c in zip(messages, captions):
                if self._too_big(m):
                    continue
                path = await self.client.download_media(m, file=tmp)
                if path:
                    files.append(path)
                    kept.append(c)
            if not files:
                text = captions[0] + Rich().add(f"\n[альбом из {len(messages)} шт.: не удалось скопировать]")
                return await self._send_text(target, text, reply_to)
            if kept[0] is not captions[0]:
                kept[0] = captions[0] + Rich().add("\n") + kept[0]
            return await self._album_request(target, files, kept, reply_to)
