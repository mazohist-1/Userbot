"""Команды, которые вы пишете сами в любом чате (сообщение сразу удаляется).

Ответы приходят в «Избранное» (CMD_REPLY=saved) или в тот же чат (CMD_REPLY=here).
Команда .get кладёт найденные сообщения в чат, где её написали.
"""

import asyncio
import html
import logging
import re
import shlex
import time
from datetime import datetime, timezone
from types import SimpleNamespace

from telethon import errors, utils
from telethon.tl.functions.account import GetNotifySettingsRequest
from telethon.tl.types import (
    InputMessagesFilterPhotos, InputNotifyBroadcasts, InputNotifyChats, InputNotifyUsers, User,
)

from .cloner import Rich, media_kind, units
from .links import Link, LinkError, parse_link

log = logging.getLogger(__name__)

CMD_RE = re.compile(r"^\.([A-Za-z]+)(?=\s|$)")
COMMANDS = {"get", "getm", "getmu", "unread", "ex", "rm", "list", "aaa", "clone", "group", "status", "commands", "help"}

HELP = """<b>Команды юзербота</b> (сообщение с командой сразу удаляется)

<code>.get &lt;ссылка&gt;</code> — сообщение по ссылке, в текущий чат
<code>.get reply</code> — сообщение, на которое вы ответили
<code>.get last N [ссылка]</code> — последние N сообщений чата или темы
<code>.get lastI N [ссылка]</code> — последние N фотографий
<code>.get search "текст" [ссылка]</code> — поиск по тексту
<code>.getM [N] &lt;ссылка&gt;</code> — все медиа: видео, фото, GIF и файлы (ссылка на сообщение — из него/альбома, на чат или тему — из всего чата)
<code>.getMU [N] &lt;ссылка на сообщение&gt;</code> — все медиа автора этого сообщения в том чате
<code>.getMU [N] &lt;ссылка на чат&gt; @user|ID</code> — все медиа этого пользователя в чате
<code>.unread [N]</code> — непрочитанное из чатов с включёнными уведомлениями (до N из каждого)
<code>.unread list</code> — только список таких чатов
<code>.ex @user</code> — в чёрный список (можно ID или ответом на сообщение)
<code>.rm @user</code> — убрать из чёрного списка
<code>.list</code> — чёрный список
<code>.aaa</code> — ID текущего чата
<code>.clone add ID</code> — создать клон группы (без ID — текущей)
<code>.clone remove ID</code> — перестать клонировать группу
<code>.clone list</code> — все клоны
<code>.group on/off</code> — вкл/выкл пересылку из групп
<code>.status</code> — состояние бота
<code>.commands</code> — этот список

Без ссылки <code>.get last/lastI/search</code> и <code>.getM</code> работают в текущем чате."""


MEDIA_KINDS = {"photo", "video", "gif", "document", "audio"}
ALBUM_GROUP = {"photo": "visual", "video": "visual", "document": "file", "audio": "file"}


def is_media_file(message) -> bool:
    return not getattr(message, "action", None) and media_kind(message) in MEDIA_KINDS


def media_batches(media: list) -> list:
    """Делит медиа на альбомы до 10 штук: Telegram не смешивает в альбоме фото с файлами,
    а GIF в альбом не берёт вовсе."""
    out, last = [], None
    for m in media:
        group = ALBUM_GROUP.get(media_kind(m))
        if group and group == last and len(out[-1]) < 10:
            out[-1].append(m)
        else:
            out.append([m])
        last = group
    return out


def is_muted(settings, default=None, now=None) -> bool:
    """Выключены ли уведомления: своя настройка чата, иначе общая для его типа."""
    now = now or datetime.now(timezone.utc)
    for s in (settings, default):
        until = getattr(s, "mute_until", None)
        if until is not None:
            if isinstance(until, (int, float)):
                until = datetime.fromtimestamp(until, timezone.utc)
            return until > now
    return False


class UsageError(Exception):
    """Команда написана неправильно — показать подсказку."""


def is_command(text) -> bool:
    m = CMD_RE.match(text or "")
    return bool(m) and m.group(1).lower() in COMMANDS


def split_args(text: str) -> list:
    text = text.replace("“", '"').replace("”", '"').replace("«", '"').replace("»", '"')
    try:
        return shlex.split(text)
    except ValueError:  # незакрытая кавычка
        return text.split()


def topic_of(message):
    """(id темы форума, id сообщения, на которое ответили) для сообщения-команды."""
    r = message.reply_to
    if r is None:
        return None, None
    if getattr(r, "forum_topic", False):
        if r.reply_to_top_id:
            return r.reply_to_top_id, r.reply_to_msg_id
        return r.reply_to_msg_id, None  # просто сообщение в теме, без ответа
    return None, r.reply_to_msg_id


def parse_count(raw: str, limit: int) -> int:
    try:
        n = int(raw)
    except ValueError:
        raise UsageError(f"N должно быть числом, а не {raw!r}")
    if n <= 0:
        raise UsageError("N должно быть больше нуля")
    return min(n, limit)


def esc(s) -> str:
    return html.escape(str(s))


class Commands:
    def __init__(self, app):
        self.app = app

    @property
    def client(self):
        return self.app.client

    @property
    def store(self):
        return self.app.store

    @property
    def cloner(self):
        return self.app.cloner

    # --- приём -------------------------------------------------------------

    async def handle(self, event):
        try:
            text = event.message.message or ""
            m = CMD_RE.match(text)
            if not m or m.group(1).lower() not in COMMANDS:
                return
            name = m.group(1).lower()
            topic, reply_id = topic_of(event.message)
            ctx = SimpleNamespace(
                chat_id=event.chat_id, chat=await event.get_input_chat(), topic=topic, reply_id=reply_id)
            try:
                await event.delete()
            except Exception as e:
                log.debug("Не удалось удалить команду: %r", e)
            await self.run(name, split_args(text[m.end():]), ctx)
        except Exception:
            # обработчик команд никогда не должен ронять приём обновлений
            log.exception("Ошибка в обработчике команд")

    async def run(self, name: str, args: list, ctx):
        log.info("Команда .%s %s", name, " ".join(args))
        try:
            handler = getattr(self, f"cmd_{name}")
            answer = await handler(args, ctx)
        except UsageError as e:
            answer = f"⚠️ {esc(e)}\n\nСписок команд: <code>.commands</code>"
        except LinkError as e:
            answer = f"⚠️ Ссылка не распознана: {esc(e)}"
        except errors.FloodWaitError as e:
            answer = f"⏳ Telegram просит подождать {e.seconds} с. Повторите команду позже."
        except (ValueError, errors.UsernameNotOccupiedError, errors.UsernameInvalidError,
                errors.ChannelPrivateError, errors.ChannelInvalidError, errors.MsgIdInvalidError) as e:
            answer = f"⚠️ Не найдено или нет доступа: {esc(e)}"
        except Exception as e:
            log.exception("Команда .%s упала", name)
            answer = f"❌ Ошибка: {esc(repr(e))}"
        if answer:
            await self.respond(ctx, answer)

    async def respond(self, ctx, text: str):
        here = self.app.cfg.cmd_reply == "here" or ctx.chat_id == self.app.me_id
        target = ctx.chat if here else "me"
        try:
            for chunk in self._chunks(text):
                await self.client.send_message(target, chunk, parse_mode="html", link_preview=False,
                                               reply_to=ctx.topic if here else None)
        except Exception as e:
            log.warning("Не удалось отправить ответ на команду: %r", e)

    @staticmethod
    def _chunks(text: str, limit: int = 4000) -> list:
        out, cur = [], ""
        for line in text.split("\n"):
            if cur and len(cur) + len(line) + 1 > limit:
                out.append(cur)
                cur = ""
            cur = f"{cur}\n{line}" if cur else line
        if cur:
            out.append(cur)
        return out

    # --- общие помощники ---------------------------------------------------

    async def peer_from_arg(self, arg: str):
        """ID, @username или ссылка → сущность."""
        if re.fullmatch(r"-?\d+", arg):
            n = int(arg)
            try:
                return await self.cloner.entity(n)
            except ValueError:
                if n > 0:  # ID канала без -100 в начале
                    return await self.cloner.entity(-(10 ** 12 + n))
                raise
        return await self.cloner.entity(parse_link(arg).chat)

    async def deliver(self, ctx, messages: list, chat):
        """Копирует сообщения в чат, где написана команда."""
        sent = 0
        for unit in units(messages):
            header = await self.cloner.short_header(unit[0], chat)
            await self.cloner.send_copy(ctx.chat, unit, header, reply_to=ctx.topic)
            sent += len(unit)
            await asyncio.sleep(0.5)  # бережём лимиты API
        return sent

    # --- .get --------------------------------------------------------------

    async def cmd_get(self, args, ctx):
        if not args:
            raise UsageError("Укажите ссылку: .get https://t.me/канал/123")
        sub = args[0].lower()

        if sub == "reply":
            if not ctx.reply_id:
                raise UsageError("Напишите .get reply ответом на сообщение")
            message = await self.client.get_messages(ctx.chat, ids=ctx.reply_id)
            if message is None:
                return "⚠️ Сообщение не найдено"
            await self.deliver(ctx, await self.cloner.album_of(ctx.chat, message), await self._chat(ctx.chat))
            return None

        if sub in ("last", "lasti"):
            if len(args) < 2:
                raise UsageError(f"Формат: .get {args[0]} N [ссылка]")
            n = parse_count(args[1], self.app.cfg.get_limit)
            chat, thread = await self._target(args[2] if len(args) > 2 else None, ctx)
            kw = {"filter": InputMessagesFilterPhotos} if sub == "lasti" else {}
            found = [m async for m in self.client.iter_messages(chat, limit=n, reply_to=thread, **kw)]
            return await self._deliver_found(ctx, found, chat)

        if sub == "search":
            if len(args) < 2:
                raise UsageError('Формат: .get search "текст" [ссылка]')
            chat, thread = await self._target(args[2] if len(args) > 2 else None, ctx)
            found = [m async for m in self.client.iter_messages(
                chat, search=args[1], limit=self.app.cfg.search_limit, reply_to=thread)]
            return await self._deliver_found(ctx, found, chat)

        link = parse_link(args[0])
        if link.msg_id is None:
            raise UsageError("В ссылке нет номера сообщения. Для последних сообщений: .get last N <ссылка>")
        chat = await self.cloner.entity(link.chat)
        message = await self.client.get_messages(chat, ids=link.msg_id)
        if message is None or getattr(message, "action", None):
            return "⚠️ Сообщение не найдено (удалено или нет доступа)"
        await self.deliver(ctx, await self.cloner.album_of(chat, message), chat)
        return None

    async def _chat(self, peer):
        try:
            return await self.client.get_entity(peer)
        except Exception:
            return None

    async def _target(self, raw, ctx):
        """Чат и тема для last/lastI/search."""
        if raw is None:
            return await self._chat(ctx.chat) or ctx.chat, ctx.topic
        link: Link = parse_link(raw)
        chat = await self.cloner.entity(link.chat)
        thread = link.thread
        # t.me/forum/424 в форуме — это тема 424, а не одно сообщение
        if thread is None and link.msg_id and getattr(chat, "forum", False):
            thread = link.msg_id
        return chat, thread

    async def _deliver_found(self, ctx, found, chat):
        found = [m for m in found if not getattr(m, "action", None)]
        if not found:
            return "🔍 Ничего не найдено"
        await self.deliver(ctx, list(reversed(found)), chat)  # от старых к новым
        return None

    # --- .getM -----------------------------------------------------------

    async def cmd_getm(self, args, ctx):
        limit = self.app.cfg.getm_limit
        if args and re.fullmatch(r"\d+", args[0]):
            limit = parse_count(args.pop(0), limit)
        raw = args[0] if args else None

        link = parse_link(raw) if raw else None
        if link is not None and link.msg_id is not None:
            chat = await self.cloner.entity(link.chat)
            forum = getattr(chat, "forum", False)
            if link.thread is not None or not forum:
                # ссылка на сообщение: медиа из него или его альбома
                message = await self.client.get_messages(chat, ids=link.msg_id)
                if message is None or getattr(message, "action", None):
                    return "⚠️ Сообщение не найдено (удалено или нет доступа)"
                media = [m for m in await self.cloner.album_of(chat, message) if is_media_file(m)]
                if not media:
                    return "🔍 В этом сообщении нет видео, фото, GIF или файлов"
                sent = await self.deliver_media(ctx, media, chat)
                return f"✅ Отправлено медиа: {sent}"

        chat, thread = await self._target(raw, ctx)
        media = []
        async for m in self.client.iter_messages(chat, reply_to=thread):
            if is_media_file(m):
                media.append(m)
                if len(media) >= limit:
                    break
        if not media:
            return "🔍 В чате нет видео, фото, GIF или файлов"
        sent = await self.deliver_media(ctx, list(reversed(media)), chat)  # от старых к новым
        note = f" (лимит {limit}, больше: <code>.getM N ссылка</code>)" if len(media) >= limit else ""
        return f"✅ Отправлено медиа: {sent}{note}"

    async def cmd_getmu(self, args, ctx):
        limit = self.app.cfg.getm_limit
        if args and re.fullmatch(r"\d+", args[0]):
            limit = parse_count(args.pop(0), limit)
        usage = "Формат: .getMU [N] <ссылка на сообщение> или .getMU [N] <ссылка на чат> @user|ID"

        if not args:
            if not ctx.reply_id:
                raise UsageError(usage)
            # ответом на сообщение: автор этого сообщения в текущем чате
            chat, thread = await self._chat(ctx.chat) or ctx.chat, ctx.topic
            message = await self.client.get_messages(ctx.chat, ids=ctx.reply_id)
            if message is None:
                return "⚠️ Сообщение не найдено"
            author, author_id = await message.get_input_sender(), message.sender_id
        elif len(args) == 1:
            link = parse_link(args[0])
            if link.msg_id is None:
                raise UsageError("Это ссылка на чат: укажите вторым параметром @user или ID. " + usage)
            chat = await self.cloner.entity(link.chat)
            thread = link.thread
            message = await self.client.get_messages(chat, ids=link.msg_id)
            if message is None:
                return "⚠️ Сообщение не найдено (удалено или нет доступа)"
            author, author_id = await message.get_input_sender(), message.sender_id
        else:
            chat, thread = await self._target(args[0], ctx)
            author, author_id = await self._author(args[1])
        if author_id is None:
            return "⚠️ У этого сообщения нет автора (например, пост канала без подписи)"

        media = []
        if author is not None:
            found = self.client.iter_messages(chat, from_user=author, reply_to=thread)
        else:
            # пользователь не в кэше: перебираем историю и сверяем ID автора
            found = self.client.iter_messages(chat, reply_to=thread, limit=self.app.cfg.getm_scan_limit)
        async for m in found:
            if m.sender_id == author_id and is_media_file(m):
                media.append(m)
                if len(media) >= limit:
                    break
        name = await self._name(author_id)
        if not media:
            return f"🔍 У {esc(name)} в этом чате нет видео, фото, GIF или файлов"
        sent = await self.deliver_media(ctx, list(reversed(media)), chat)  # от старых к новым
        note = f" (лимит {limit}, больше: <code>.getMU N …</code>)" if len(media) >= limit else ""
        return f"✅ Отправлено медиа от {esc(name)}: {sent}{note}"

    async def _author(self, raw: str):
        """@user, ID или ссылка → (input-сущность или None, id)."""
        try:
            entity = await self.peer_from_arg(raw)
            return utils.get_input_peer(entity), utils.get_peer_id(entity)
        except (ValueError, LinkError):
            if re.fullmatch(r"\d+", raw):
                return None, int(raw)
            raise

    async def _name(self, peer_id) -> str:
        try:
            return utils.get_display_name(await self.client.get_entity(peer_id)) or str(peer_id)
        except Exception:
            return str(peer_id)

    async def deliver_media(self, ctx, media: list, chat) -> int:
        """Отправляет медиа пачками по 10: фото и видео вместе, файлы вместе, GIF по одному."""
        batches = media_batches(media)
        title = utils.get_display_name(chat) if chat else ""
        done = 0
        for i, batch in enumerate(batches, 1):
            header = Rich().add(f"📥 {title} · {i}/{len(batches)}") if title else Rich().add(f"📥 {i}/{len(batches)}")
            await self.cloner.send_copy(ctx.chat, batch, header, reply_to=ctx.topic)
            done += len(batch)
            await asyncio.sleep(1)  # бережём лимиты API
        return done

    # --- .unread ---------------------------------------------------------

    async def unread_chats(self) -> list:
        """[(диалог, сколько непрочитанных)] для чатов с включёнными уведомлениями."""
        defaults = {}
        for key, peer in (("user", InputNotifyUsers()), ("chat", InputNotifyChats()),
                          ("channel", InputNotifyBroadcasts())):
            try:
                defaults[key] = await self.client(GetNotifySettingsRequest(peer))
            except Exception as e:
                log.debug("Не удалось получить общие настройки уведомлений (%s): %r", key, e)
        skip = self.cloner.clone_ids | {self.app.me_id} | set(self.app.exclude_ids)
        out = []
        async for d in self.client.iter_dialogs():
            count = d.unread_count or (1 if getattr(d.dialog, "unread_mark", False) else 0)
            if not count or d.id in skip:
                continue
            kind = "user" if d.is_user else "chat" if d.is_group else "channel"
            if is_muted(d.dialog.notify_settings, defaults.get(kind)):
                continue
            out.append((d, count))
        return out

    async def cmd_unread(self, args, ctx):
        only_list = bool(args) and args[0].lower() == "list"
        per_chat = self.app.cfg.unread_per_chat
        if args and not only_list:
            per_chat = parse_count(args[0], self.app.cfg.get_limit)

        chats = await self.unread_chats()
        if not chats:
            return "📭 Непрочитанного в чатах с включёнными уведомлениями нет"
        lines = [f"<b>Непрочитанное ({sum(c for _, c in chats)}) в {len(chats)} чатах</b>"]
        lines += [f"• {esc(d.name or d.id)} — {c}" for d, c in chats]
        if only_list:
            return "\n".join(lines)

        sent = 0
        for d, count in chats:
            found = [m async for m in self.client.iter_messages(
                d.input_entity, min_id=d.dialog.read_inbox_max_id, limit=min(count, per_chat))]
            found = [m for m in reversed(found) if not getattr(m, "action", None)]
            if not found:  # чат помечен непрочитанным вручную: берём последнее
                found = [m async for m in self.client.iter_messages(d.input_entity, limit=1)]
            for unit in units(found):
                header = await self.cloner.header(unit[0], d.id, group=not d.is_user, count=len(unit))
                await self.cloner.send_copy(ctx.chat, unit, header, reply_to=ctx.topic)
                sent += len(unit)
                await asyncio.sleep(0.5)  # бережём лимиты API
        lines.append(f"\n✅ Прислано сообщений: {sent}. В самих чатах они остались непрочитанными.")
        return "\n".join(lines)

    # --- чёрный список -----------------------------------------------------

    async def _user_from(self, args, ctx):
        if args:
            return await self.peer_from_arg(args[0])
        if ctx.reply_id:
            message = await self.client.get_messages(ctx.chat, ids=ctx.reply_id)
            if message is not None:
                sender = await message.get_sender()
                if sender is not None:
                    return sender
        raise UsageError("Укажите @username или ID, либо напишите команду ответом на сообщение")

    async def cmd_ex(self, args, ctx):
        entity = await self._user_from(args, ctx)
        peer_id = utils.get_peer_id(entity)
        name = utils.get_display_name(entity) or str(peer_id)
        if not self.store.blacklist_add(peer_id, name):
            return f"Уже в чёрном списке: {esc(name)} (<code>{peer_id}</code>)"
        return f"🚫 {esc(name)} (<code>{peer_id}</code>) в чёрном списке, сообщения от него не пересылаются"

    async def cmd_rm(self, args, ctx):
        if args and re.fullmatch(r"-?\d+", args[0]):
            peer_id, name = int(args[0]), args[0]
        else:
            entity = await self._user_from(args, ctx)
            peer_id = utils.get_peer_id(entity)
            name = utils.get_display_name(entity) or str(peer_id)
        if not self.store.blacklist_remove(peer_id):
            return f"{esc(name)} нет в чёрном списке"
        return f"✅ {esc(name)} (<code>{peer_id}</code>) убран из чёрного списка"

    async def cmd_list(self, args, ctx):
        rows = self.store.blacklist()
        if not rows:
            return "Чёрный список пуст"
        lines = [f"<b>Чёрный список ({len(rows)})</b>"]
        lines += [f"• {esc(name)} — <code>{uid}</code>" for uid, name in rows]
        return "\n".join(lines)

    # --- чаты и клоны ------------------------------------------------------

    async def cmd_aaa(self, args, ctx):
        chat = await self._chat(ctx.chat)
        title = utils.get_display_name(chat) if chat else ""
        text = f"ID чата «{esc(title)}»: <code>{ctx.chat_id}</code>"
        if ctx.topic:
            text += f"\nID темы: <code>{ctx.topic}</code>"
        return text

    async def cmd_clone(self, args, ctx):
        sub = args[0].lower() if args else ""
        if sub == "add":
            entity = await self.peer_from_arg(args[1]) if len(args) > 1 else await self._chat(ctx.chat)
            if entity is None:
                raise UsageError("Не удалось определить чат")
            if isinstance(entity, User):
                return "⚠️ Это личный чат: лички и так копируются в «Private Messages»"
            source_id = utils.get_peer_id(entity)
            if source_id in self.cloner.clone_ids:
                return "⚠️ Это сам клон"
            if self.store.get_clone(source_id) is not None:
                return f"Клон уже есть: {esc(utils.get_display_name(entity))} (<code>{source_id}</code>)"
            _, title = await self.cloner.create_group_clone(source_id, utils.get_display_name(entity))
            note = "" if self.store.group_forwarding() else "\n⚠️ Пересылка из групп выключена: <code>.group on</code>"
            return f"✅ Создан клон «{esc(title)}» для <code>{source_id}</code>{note}"

        if sub == "remove":
            if len(args) < 2:
                raise UsageError("Формат: .clone remove ID")
            raw = args[1]
            source_id = int(raw) if re.fullmatch(r"-?\d+", raw) else utils.get_peer_id(await self.peer_from_arg(raw))
            if self.store.get_clone(source_id) is None and source_id > 0:
                source_id = -(10 ** 12 + source_id)
            rows = {s: t for s, _, t in self.store.groups()}
            if self.store.delete_clone(source_id) is None:
                return f"Клона для <code>{esc(raw)}</code> нет. Список: <code>.clone list</code>"
            return (f"🗑 Клон «{esc(rows.get(source_id, ''))}» отключён, сообщения из <code>{source_id}</code> "
                    "больше не пересылаются. Сам канал-клон остался, его можно удалить вручную.")

        if sub == "list":
            rows = self.store.groups()
            private = self.store.private_clone()
            lines = [f"<b>Клоны</b>\nЛички → «{esc(self.app.cfg.private_clone_title)}» "
                     f"(<code>{private or 'ещё не создан'}</code>)"]
            if not rows:
                lines.append("Клонов групп нет. Добавить: <code>.clone add ID</code>")
            for source_id, clone_id, title in rows:
                lines.append(f"• {esc(title)}: <code>{source_id}</code> → <code>{clone_id}</code>")
            return "\n".join(lines)

        raise UsageError("Формат: .clone add ID | .clone remove ID | .clone list")

    async def cmd_group(self, args, ctx):
        sub = args[0].lower() if args else ""
        if sub not in ("on", "off"):
            state = "включена" if self.store.group_forwarding() else "выключена"
            raise UsageError(f"Формат: .group on или .group off (сейчас {state})")
        self.store.set_group_forwarding(sub == "on")
        return "✅ Пересылка из групп включена" if sub == "on" else "⏸ Пересылка из групп выключена"

    # --- состояние ---------------------------------------------------------

    async def cmd_status(self, args, ctx):
        app = self.app
        up = int(time.time() - app.started_at)
        last = self.store.last_forwarded_at()
        last_text = time.strftime("%d.%m %H:%M", time.localtime(last)) if last else "ещё не было"
        private = self.store.private_clone()
        return "\n".join([
            "<b>Юзербот работает</b> ✅",
            f"Аккаунт: {esc(utils.get_display_name(app.me))} (<code>{app.me_id}</code>)",
            f"Без перезапуска: {up // 86400} д {up % 86400 // 3600} ч {up % 3600 // 60} мин",
            f"Связь с Telegram: {'есть' if self.client.is_connected() else 'нет'}",
            f"Лички: {'вкл' if app.cfg.forward_private else 'выкл'}"
            f" → <code>{private or 'клон ещё не создан'}</code>",
            f"Пересылка из групп: {'вкл' if self.store.group_forwarding() else 'выкл'}, "
            f"клонов групп: {len(self.store.groups())}",
            f"Чёрный список: {len(self.store.blacklist())}",
            f"В очереди: {self.store.pending_count()}, "
            f"ошибок за сутки: {self.store.failed_count(time.time() - 86400)}",
            f"Последняя пересылка: {last_text}",
        ])

    async def cmd_commands(self, args, ctx):
        return HELP

    cmd_help = cmd_commands
