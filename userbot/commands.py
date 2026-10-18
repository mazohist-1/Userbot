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
from types import SimpleNamespace

from telethon import errors, utils
from telethon.tl.types import InputMessagesFilterPhotos, User

from .cloner import units
from .links import Link, LinkError, parse_link

log = logging.getLogger(__name__)

CMD_RE = re.compile(r"^\.([A-Za-z]+)(?=\s|$)")
COMMANDS = {"get", "ex", "rm", "list", "aaa", "clone", "group", "status", "commands", "help"}

HELP = """<b>Команды юзербота</b> (сообщение с командой сразу удаляется)

<code>.get &lt;ссылка&gt;</code> — сообщение по ссылке, в текущий чат
<code>.get reply</code> — сообщение, на которое вы ответили
<code>.get last N [ссылка]</code> — последние N сообщений чата или темы
<code>.get lastI N [ссылка]</code> — последние N фотографий
<code>.get search "текст" [ссылка]</code> — поиск по тексту
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

Без ссылки <code>.get last/lastI/search</code> работают в текущем чате."""


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
