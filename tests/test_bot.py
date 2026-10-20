"""Тесты логики без подключения к Telegram: python -m unittest discover tests"""

import asyncio
import os
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

os.environ.setdefault("API_ID", "1")
os.environ.setdefault("API_HASH", "x")

from telethon import errors  # noqa: E402
from telethon.tl.types import (  # noqa: E402
    Document, DocumentAttributeAudio, DocumentAttributeSticker, DocumentAttributeVideo,
    Channel, ChatPhotoEmpty, DocumentAttributeAnimated, InputStickerSetEmpty, Message, MessageEntityBold, MessageMediaDocument, MessageMediaPhoto,
    PeerUser, Photo,
)

from telethon.tl.functions.updates import GetStateRequest  # noqa: E402
from userbot import config  # noqa: E402
from userbot.app import App  # noqa: E402
from userbot.cloner import Cloner, Rich, SkipMessage, media_kind, units  # noqa: E402
from userbot.commands import Commands, is_command, is_muted, media_batches, split_args, topic_of  # noqa: E402
from userbot.links import Link, LinkError, parse_link  # noqa: E402
from userbot.storage import Storage  # noqa: E402


def make_cfg(tmp, **env):
    os.environ["DATA_DIR"] = tmp
    for k, v in env.items():
        os.environ[k] = v
    try:
        return config.load()
    finally:
        for k in env:
            del os.environ[k]


def msg(mid, out=False, private=True, group=False, text="", sender_id=None, date=None):
    return SimpleNamespace(id=mid, out=out, is_private=private, is_group=group, chat=None,
                           message=text, sender_id=sender_id, date=date)


def rpc(cls, **kw):
    return cls(request=None, **kw) if kw else cls(request=None)


def doc(*attrs):
    return MessageMediaDocument(document=Document(
        id=1, access_hash=1, file_reference=b"", date=None, mime_type="x", size=10, dc_id=1,
        attributes=list(attrs)))


def chan(cid, title="x"):
    return Channel(id=cid, title=title, photo=ChatPhotoEmpty(), date=None)


def tl_msg(media=None, text=""):
    return Message(id=1, peer_id=PeerUser(5), date=None, message=text, media=media)


class StorageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.s = Storage(Path(self.tmp) / "t.db")

    def test_pending_dedup_and_forwarded(self):
        self.assertTrue(self.s.add_pending(1, 10))
        self.assertFalse(self.s.add_pending(1, 10))
        pid, src, mid, att = self.s.next_pending()
        self.s.mark_forwarded(src, mid, 99)
        self.assertIsNone(self.s.next_pending())
        self.assertFalse(self.s.add_pending(1, 10))

    def test_last_forwarded_before(self):
        self.s.mark_forwarded(1, 5, None)
        t = time.time() + 0.01
        self.assertEqual(self.s.last_forwarded_id(1, t), 5)
        self.assertIsNone(self.s.last_forwarded_id(1, 0))

    def test_prune_keeps_last(self):
        self.s.mark_forwarded(1, 5, None)
        self.s.mark_forwarded(1, 6, None)
        self.s.db.execute("UPDATE forwarded SET done_at=0")
        self.s.prune(1)
        self.assertEqual([r[0] for r in self.s.db.execute("SELECT msg_id FROM forwarded")], [6])

    def test_state_persists(self):
        self.s.set_clone(-1002, -1001, "t")
        self.s.set_private_clone(-1003)
        self.s.blacklist_add(7, "spam")
        self.s.set_group_forwarding(False)
        self.s.close()
        s2 = Storage(Path(self.tmp) / "t.db")
        self.assertEqual(s2.get_clone(-1002), -1001)
        self.assertEqual(s2.clone_ids(), {-1001, -1003})
        self.assertTrue(s2.is_blacklisted(7))
        self.assertFalse(s2.group_forwarding())

    def test_migration_from_v1_disables_old_clones(self):
        path = Path(self.tmp) / "old.db"
        db = sqlite3.connect(str(path))
        db.executescript("""CREATE TABLE clones (source_id INTEGER PRIMARY KEY, clone_id INTEGER NOT NULL UNIQUE,
                            title TEXT, created_at REAL NOT NULL);
                            INSERT INTO clones VALUES (5, -1009, 'old', 0);""")
        db.commit()
        db.close()
        s = Storage(path)
        self.assertEqual(s.groups(), [])
        self.assertIn(-1009, s.clone_ids())  # старый клон никогда не станет источником
        s.set_clone(-1002, -1001, "new")
        s.close()
        self.assertEqual(Storage(path).get_clone(-1002), -1001)  # повторная миграция не стирает

    def test_delete_clone_keeps_id_as_legacy(self):
        self.s.set_clone(-1002, -1001, "t")
        self.assertEqual(self.s.delete_clone(-1002), -1001)
        self.assertIsNone(self.s.get_clone(-1002))
        self.assertIn(-1001, self.s.clone_ids())

    def test_group_toggle_sets_since(self):
        self.assertTrue(self.s.group_forwarding())
        self.s.set_group_forwarding(True)
        self.assertGreater(self.s.group_since(), time.time() - 5)


class LinksTest(unittest.TestCase):
    def test_formats(self):
        cases = {
            "https://t.me/username/123": Link("username", 123),
            "t.me/username/123": Link("username", 123),
            "https://t.me/c/1234567890/123": Link(-1001234567890, 123),
            "https://t.me/username/424/1142": Link("username", 1142, 424),
            "https://t.me/c/1234567890/424/1142": Link(-1001234567890, 1142, 424),
            "https://t.me/username/1142?thread=456": Link("username", 1142, 456),
            "https://t.me/c/1234567890/77?thread=456": Link(-1001234567890, 77, 456),
            "tg://resolve?domain=username&post=123": Link("username", 123),
            "tg://resolve?domain=username&post=5&thread=9": Link("username", 5, 9),
            "tg://privatepost?channel=1234567890&post=7": Link(-1001234567890, 7),
            "https://t.me/username": Link("username"),
            "@username": Link("username"),
            "https://www.telegram.me/s/username/12?single": Link("username", 12),
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(parse_link(raw), expected)

    def test_bad_links(self):
        for raw in ("", "https://example.com/a/1", "https://t.me/+AbCdEf", "https://t.me/user/abc",
                    "tg://resolve?post=1", "https://t.me/c/", "https://t.me/a/1/2/3"):
            with self.subTest(raw=raw), self.assertRaises(LinkError):
                parse_link(raw)


class MediaKindTest(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(media_kind(tl_msg(text="hi")), "text")
        self.assertEqual(media_kind(tl_msg(MessageMediaPhoto(photo=Photo(
            id=1, access_hash=1, file_reference=b"", date=None, sizes=[], dc_id=1)))), "photo")
        self.assertEqual(media_kind(tl_msg(doc(DocumentAttributeAudio(duration=1, voice=True)))), "voice")
        self.assertEqual(media_kind(tl_msg(doc(DocumentAttributeVideo(
            duration=1, w=1, h=1, round_message=True)))), "round")
        self.assertEqual(media_kind(tl_msg(doc(DocumentAttributeSticker(
            alt="", stickerset=InputStickerSetEmpty())))), "sticker")
        self.assertEqual(media_kind(tl_msg(doc())), "document")

    def test_units_groups_albums(self):
        ms = [SimpleNamespace(id=i, grouped_id=g) for i, g in ((1, None), (2, 7), (3, 7), (4, None), (5, 8))]
        self.assertEqual([[m.id for m in u] for u in units(ms)], [[1], [2, 3], [4], [5]])

    def test_rich_shifts_entities(self):
        r = Rich().add("👤 ").add("Имя", MessageEntityBold) + Rich().extend("x", [MessageEntityBold(0, 1)])
        self.assertEqual(r.text, "👤 Имяx")
        self.assertEqual([(e.offset, e.length) for e in r.entities], [(3, 3), (6, 1)])  # эмодзи = 2 в UTF-16


class ClonerTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = make_cfg(self.tmp)
        self.store = Storage(self.cfg.db_path)
        self.client = MagicMock()
        self.client.get_input_entity = AsyncMock(side_effect=lambda x: f"peer{x}")
        self.client.get_messages = AsyncMock(return_value=[])
        self.cloner = Cloner(self.client, self.cfg, self.store)
        self.cloner.header = AsyncMock(return_value=Rich().add("H"))
        self.created = []
        self.cloner._create_channel = AsyncMock(side_effect=self._create)
        self.cloner._find_own_channel = AsyncMock(return_value=None)

    async def _create(self, title, about, archive=False):
        self.created.append(title)
        return chan(1000 + len(self.created), title)

    def message(self, mid=10, private=True, group=False, grouped_id=None):
        return SimpleNamespace(id=mid, is_private=private, is_group=group, grouped_id=grouped_id)

    async def test_private_goes_to_single_clone(self):
        self.cloner.send_copy = AsyncMock(return_value=7)
        self.assertEqual(await self.cloner.clone_message(1, 10, self.message(10)), [(10, 7)])
        self.assertEqual(await self.cloner.clone_message(2, 11, self.message(11)), [(11, 7)])
        self.assertEqual(self.created, ["Private Messages"])
        self.assertEqual(self.store.private_clone(), -1000000001001)
        self.assertIn(-1000000001001, self.cloner.clone_ids)

    async def test_private_clone_found_by_title(self):
        self.cloner._find_own_channel = AsyncMock(return_value=chan(55, "Private Messages"))
        self.cloner.send_copy = AsyncMock(return_value=7)
        await self.cloner.clone_message(1, 10, self.message(10))
        self.assertEqual(self.created, [])
        self.assertEqual(self.store.private_clone(), -1000000000055)

    async def test_group_only_when_added(self):
        self.cloner.send_copy = AsyncMock(return_value=7)
        with self.assertRaises(SkipMessage):
            await self.cloner.clone_message(-1002, 10, self.message(10, private=False, group=True))
        self.store.set_clone(-1002, -1003, "Clone: g")
        await self.cloner.clone_message(-1002, 10, self.message(10, private=False, group=True))
        self.assertEqual(self.cloner.send_copy.await_args.args[0], "peer-1003")
        self.assertEqual(self.created, [])

    async def test_album_forwarded_once(self):
        album = [SimpleNamespace(id=i, grouped_id=5) for i in (10, 11, 12)]
        self.client.get_messages = AsyncMock(return_value=[None] * 9 + album + [None] * 7)
        self.cloner.send_copy = AsyncMock(return_value=7)
        done = await self.cloner.clone_message(1, 10, self.message(10, grouped_id=5))
        self.assertEqual(done, [(10, 7), (11, 7), (12, 7)])
        self.assertEqual(len(self.cloner.send_copy.await_args.args[1]), 3)

    async def test_recreates_deleted_private_clone(self):
        self.cloner.send_copy = AsyncMock(side_effect=[rpc(errors.ChannelPrivateError), 8])
        self.client.get_messages = AsyncMock(side_effect=rpc(errors.ChannelPrivateError))
        self.assertEqual(await self.cloner.clone_message(1, 10, self.message(10)), [(10, 8)])
        self.assertEqual(len(self.created), 2)
        self.assertIn(-1000000001001, self.store.clone_ids())  # старый клон остался «клоном»

    async def test_deleted_message_skipped(self):
        self.client.get_messages = AsyncMock(return_value=None)
        with self.assertRaises(SkipMessage):
            await self.cloner.clone_message(1, 10)

    async def test_text_gets_header(self):
        self.client.send_message = AsyncMock(return_value=SimpleNamespace(id=3))
        m = SimpleNamespace(media=None, message="привет", entities=None, web_preview=None)
        self.assertEqual(await self.cloner.send_copy("c", [m], Rich().add("H")), 3)
        self.assertEqual(self.client.send_message.await_args.args[1], "H\n\nпривет")

    async def test_sticker_header_separately(self):
        self.client.send_message = AsyncMock(return_value=SimpleNamespace(id=3))
        self.client.send_file = AsyncMock(return_value=SimpleNamespace(id=4))
        m = tl_msg(doc(DocumentAttributeSticker(alt="", stickerset=InputStickerSetEmpty())))
        self.assertEqual(await self.cloner.send_copy("c", [m], Rich().add("H")), 3)
        self.assertEqual(self.client.send_file.await_args.kwargs["reply_to"], 3)

    async def test_restricted_media_reuploaded(self):
        self.client.send_file = AsyncMock(side_effect=[rpc(errors.ChatForwardsRestrictedError),
                                                       SimpleNamespace(id=5)])
        self.client.download_media = AsyncMock(return_value=str(Path(self.tmp) / "f.jpg"))
        m = tl_msg(doc(DocumentAttributeAudio(duration=1, voice=True)), text="hi")
        self.assertEqual(await self.cloner.send_copy("c", [m], Rich().add("H")), 5)
        self.assertTrue(self.client.send_file.await_args.kwargs["voice_note"])


class AppTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = make_cfg(self.tmp)
        self.store = Storage(self.cfg.db_path)
        self.app = App(self.cfg, self.store)
        self.app.cloner = MagicMock()
        self.app.cloner.clone_ids = {-500}
        self.app.me_id = 42
        self.app.exclude_ids = set(self.cfg.exclude_chats)

    async def test_filters(self):
        a = self.app
        self.assertTrue(a.accept(msg(1), 1))
        self.assertFalse(a.accept(msg(1, out=True), 1))
        self.assertFalse(a.accept(msg(1), -500))       # клон
        self.assertFalse(a.accept(msg(1), 42))         # избранное
        self.assertFalse(a.accept(msg(1), 777000))     # коды входа
        self.assertFalse(a.accept(msg(1), 1, SimpleNamespace(bot=True)))
        self.assertTrue(a.accept(msg(1, text=".list"), 1))  # чужое сообщение с точкой — не команда

    async def test_groups_only_when_added_and_enabled(self):
        a = self.app
        g = msg(1, private=False, group=True)
        self.assertFalse(a.accept(g, -1002))
        self.store.set_clone(-1002, -1003, "t")
        self.assertTrue(a.accept(g, -1002))
        self.store.set_group_forwarding(False)
        self.assertFalse(a.accept(g, -1002))
        self.store.set_group_forwarding(True)
        old = msg(1, private=False, group=True, date=datetime(2020, 1, 1, tzinfo=timezone.utc))
        self.assertFalse(a.accept(old, -1002))  # пришло, пока пересылка была выключена

    async def test_blacklist(self):
        self.store.blacklist_add(7, "x")
        self.assertFalse(self.app.accept(msg(1, sender_id=7), 7))
        self.store.set_clone(-1002, -1003, "t")
        self.assertFalse(self.app.accept(msg(1, private=False, group=True, sender_id=7), -1002))
        self.assertTrue(self.app.accept(msg(1, private=False, group=True, sender_id=8), -1002))

    async def test_own_commands_never_forwarded(self):
        cfg = make_cfg(self.tmp, INCLUDE_OUTGOING="true")
        app = App(cfg, self.store)
        app.cloner = self.app.cloner
        app.exclude_ids = set()
        self.assertFalse(app.accept(msg(1, out=True, text=".get last 5"), 1))
        self.assertTrue(app.accept(msg(1, out=True, text="привет"), 1))

    async def test_success_marks_forwarded(self):
        self.app.cloner.clone_message = AsyncMock(return_value=[(10, 5), (11, 5)])
        self.app.enqueue(1, msg(10))
        self.app.enqueue(1, msg(11))
        await self.app.process(self.store.next_pending())
        self.assertTrue(self.store.is_forwarded(1, 10))
        self.assertTrue(self.store.is_forwarded(1, 11))
        self.assertIsNone(self.store.next_pending())
        self.assertEqual(self.app.cache, {})

    async def test_floodwait_keeps_item(self):
        e = errors.FloodWaitError(request=None, capture=0)
        self.app.cloner.clone_message = AsyncMock(side_effect=e)
        self.app.enqueue(1, msg(10))
        await self.app.process(self.store.next_pending())
        self.assertEqual(self.store.next_pending()[3], 0)

    async def test_network_error_keeps_item(self):
        self.app.cloner.clone_message = AsyncMock(side_effect=ConnectionError("down"))
        self.app.enqueue(1, msg(10))
        orig = asyncio.sleep
        asyncio.sleep = AsyncMock()
        try:
            await self.app.process(self.store.next_pending())
        finally:
            asyncio.sleep = orig
        self.assertIsNotNone(self.store.next_pending())

    async def test_retries_then_failed(self):
        self.app.cloner.clone_message = AsyncMock(side_effect=ValueError("boom"))
        self.app.enqueue(1, msg(10))
        orig = asyncio.sleep
        asyncio.sleep = AsyncMock()
        try:
            for _ in range(self.cfg.max_retries):
                await self.app.process(self.store.next_pending())
        finally:
            asyncio.sleep = orig
        self.assertIsNone(self.store.next_pending())
        self.assertEqual(self.store.failed_count(), 1)

    async def test_fatal_propagates(self):
        self.app.cloner.clone_message = AsyncMock(side_effect=rpc(errors.AuthKeyUnregisteredError))
        self.app.enqueue(1, msg(10))
        with self.assertRaises(errors.AuthKeyUnregisteredError):
            await self.app.process(self.store.next_pending())

    async def test_catch_up_enqueues_missed_only(self):
        self.store.mark_forwarded(1, 10, None)
        self.store.mark_forwarded(1, 12, None)  # пришло вживую после разрыва
        self.store.mark_forwarded(-1009, 3, None)  # старая группа без клона — не трогаем
        self.store.db.execute("UPDATE forwarded SET done_at=100 WHERE msg_id=10")
        self.store.db.execute("UPDATE forwarded SET done_at=300 WHERE msg_id=12")
        seen = []

        async def history(entity, min_id=0, reverse=False, limit=None):
            seen.append(entity)
            self.assertEqual(min_id, 10)
            for i in (11, 12, 13):
                yield msg(i)

        async def dialogs(limit=None):
            yield SimpleNamespace(is_user=True, id=1, unread_count=3)  # уже известный чат
            yield SimpleNamespace(is_user=False, id=-1005, unread_count=3)

        self.app.client = MagicMock()
        self.app.client.iter_messages = history
        self.app.client.iter_dialogs = dialogs
        self.app.cloner.resolve = AsyncMock(side_effect=lambda x: f"peer{x}")
        await self.app.catch_up(200, "тест")
        queued = [r[0] for r in self.store.db.execute("SELECT msg_id FROM pending ORDER BY id")]
        self.assertEqual(queued, [11, 13])
        self.assertEqual(seen, ["peer1"])

    async def test_catch_up_new_private_chat(self):
        async def dialogs(limit=None):
            yield SimpleNamespace(is_user=True, id=9, unread_count=2, input_entity="p9", entity=None)

        async def history(entity, limit=None):
            for i in (21, 20):  # от новых к старым, как отдаёт Telegram
                yield msg(i)

        self.app.client = MagicMock()
        self.app.client.iter_messages = history
        self.app.client.iter_dialogs = dialogs
        await self.app.catch_up(200, "тест")
        queued = [r[0] for r in self.store.db.execute("SELECT msg_id FROM pending ORDER BY id")]
        self.assertEqual(queued, [20, 21])


class OfflineStatusTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = Storage(make_cfg(self.tmp).db_path)

    def make_app(self, **env):
        app = App(make_cfg(self.tmp, **env), self.store)
        app.me_id = 42
        app.cloner = SimpleNamespace(clone_ids={-500})
        return app

    async def test_client_reports_sends_only(self):
        from telethon.tl.functions.messages import SendMessageRequest
        from userbot.app import Client
        from telethon import TelegramClient
        calls = []
        client = Client.__new__(Client)
        client.on_send = lambda: calls.append(1)
        orig = TelegramClient.__call__
        TelegramClient.__call__ = AsyncMock(return_value="ok")
        try:
            send = SendMessageRequest(peer=None, message="x")
            self.assertEqual(await client(send), "ok")
            await client(GetStateRequest())
            await client([GetStateRequest(), send])
        finally:
            TelegramClient.__call__ = orig
        self.assertEqual(len(calls), 2)

    async def test_go_offline(self):
        from telethon.tl.functions.account import UpdateStatusRequest
        app = self.make_app()
        app.client = AsyncMock()
        await app.go_offline(0)
        req = app.client.await_args.args[0]
        self.assertIsInstance(req, UpdateStatusRequest)
        self.assertTrue(req.offline)

    async def test_not_while_user_active(self):
        app = self.make_app()
        app.client = AsyncMock()
        await app.on_own_message(SimpleNamespace(chat_id=-500))  # копия в клон — это бот
        await app.on_own_message(SimpleNamespace(chat_id=42))    # «Избранное»
        await app.go_offline(0)
        self.assertEqual(app.client.await_count, 1)
        await app.on_own_message(SimpleNamespace(chat_id=7))     # пишете сами
        await app.go_offline(0)
        self.assertEqual(app.client.await_count, 1)

    async def test_schedule_debounced_and_switchable(self):
        app = self.make_app()
        app.go_offline = AsyncMock()
        app.schedule_offline()
        app.schedule_offline()
        await app._offline_task
        self.assertEqual(app.go_offline.await_count, 1)
        off = self.make_app(FORCE_OFFLINE="false")
        off.schedule_offline()
        self.assertIsNone(off._offline_task)


class CommandsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = make_cfg(self.tmp)
        self.store = Storage(self.cfg.db_path)
        self.app = App(self.cfg, self.store)
        self.app.me_id = 42
        self.app.client = MagicMock()
        self.app.client.send_message = AsyncMock()
        self.app.cloner = Cloner(self.app.client, self.cfg, self.store)
        self.cmd = Commands(self.app)
        self.ctx = SimpleNamespace(chat_id=-1002, chat="here", topic=None, reply_id=None)

    def answer(self):
        args = self.app.client.send_message.await_args.args
        return args[0], args[1]

    def test_parsing(self):
        self.assertTrue(is_command(".get last 5"))
        self.assertTrue(is_command(".CLONE list"))
        self.assertFalse(is_command(".getx"))
        self.assertFalse(is_command("..."))
        self.assertFalse(is_command("текст .get"))
        self.assertEqual(split_args('search «два слова» https://t.me/a/1'),
                         ["search", "два слова", "https://t.me/a/1"])
        self.assertEqual(split_args('search "не закрыто'), ["search", '"не', "закрыто"])

    def test_topic_of(self):
        R = SimpleNamespace
        self.assertEqual(topic_of(R(reply_to=None)), (None, None))
        self.assertEqual(topic_of(R(reply_to=R(forum_topic=True, reply_to_top_id=None, reply_to_msg_id=424))),
                         (424, None))
        self.assertEqual(topic_of(R(reply_to=R(forum_topic=True, reply_to_top_id=424, reply_to_msg_id=500))),
                         (424, 500))
        self.assertEqual(topic_of(R(reply_to=R(forum_topic=False, reply_to_top_id=None, reply_to_msg_id=9))),
                         (None, 9))

    async def test_answers_go_to_saved_messages(self):
        await self.cmd.run("aaa", [], self.ctx)
        target, text = self.answer()
        self.assertEqual(target, "me")
        self.assertIn("-1002", text)

    async def test_blacklist_commands(self):
        from telethon.tl.types import User
        self.app.client.get_entity = AsyncMock(return_value=User(id=7, first_name="Спам"))
        await self.cmd.run("ex", ["@spam"], self.ctx)
        self.assertTrue(self.store.is_blacklisted(7))
        await self.cmd.run("list", [], self.ctx)
        self.assertIn("Спам", self.answer()[1])
        await self.cmd.run("rm", ["7"], self.ctx)
        self.assertFalse(self.store.is_blacklisted(7))

    async def test_clone_add_remove_list(self):
        group = Channel(id=1002, title="Группа", photo=ChatPhotoEmpty(), date=None, megagroup=True)
        self.app.client.get_entity = AsyncMock(return_value=group)
        self.app.cloner._create_channel = AsyncMock(return_value=chan(2000))
        await self.cmd.run("clone", ["add", "-1000000001002"], self.ctx)
        self.assertEqual(self.store.get_clone(-1000000001002), -1000000002000)
        self.assertEqual(self.app.cloner._create_channel.await_args.args[0], "Clone: Группа")
        await self.cmd.run("clone", ["add", "-1000000001002"], self.ctx)
        self.assertIn("уже есть", self.answer()[1])
        await self.cmd.run("clone", ["list"], self.ctx)
        self.assertIn("Clone: Группа", self.answer()[1])
        await self.cmd.run("clone", ["remove", "1002"], self.ctx)  # ID без -100 тоже понимаем
        self.assertIsNone(self.store.get_clone(-1000000001002))
        self.assertIn(-1000000002000, self.app.cloner.clone_ids)

    async def test_group_toggle(self):
        await self.cmd.run("group", ["off"], self.ctx)
        self.assertFalse(self.store.group_forwarding())
        await self.cmd.run("group", ["on"], self.ctx)
        self.assertTrue(self.store.group_forwarding())
        await self.cmd.run("group", [], self.ctx)
        self.assertIn("⚠️", self.answer()[1])

    async def test_bad_link_reported(self):
        await self.cmd.run("get", ["https://example.com/1"], self.ctx)
        self.assertIn("Ссылка не распознана", self.answer()[1])

    async def test_get_link_copies_here(self):
        m = SimpleNamespace(id=1142, grouped_id=None, action=None)
        self.app.client.get_entity = AsyncMock(return_value="chat")
        self.app.client.get_messages = AsyncMock(return_value=m)
        self.app.cloner.short_header = AsyncMock(return_value=Rich().add("H"))
        self.app.cloner.send_copy = AsyncMock(return_value=1)
        await self.cmd.run("get", ["https://t.me/username/424/1142"], self.ctx)
        self.app.client.get_messages.assert_awaited_with("chat", ids=1142)
        self.assertEqual(self.app.cloner.send_copy.await_args.args[0], "here")

    def test_media_batches(self):
        photo = MessageMediaPhoto(photo=Photo(id=1, access_hash=1, file_reference=b"", date=None, sizes=[], dc_id=1))
        p, d = tl_msg(photo), tl_msg(doc())
        gif = tl_msg(doc(DocumentAttributeVideo(duration=1, w=1, h=1), DocumentAttributeAnimated()))
        sizes = [len(b) for b in media_batches([p] * 12 + [d, d, gif, gif, p])]
        self.assertEqual(sizes, [10, 2, 2, 1, 1, 1])

    async def test_getm_message_link_sends_album_media(self):
        photo = MessageMediaPhoto(photo=Photo(id=1, access_hash=1, file_reference=b"", date=None, sizes=[], dc_id=1))
        album = [tl_msg(photo), tl_msg(doc(DocumentAttributeAudio(duration=1, voice=True))), tl_msg(doc())]
        self.app.client.get_entity = AsyncMock(return_value=chan(5, "Канал"))
        self.app.client.get_messages = AsyncMock(return_value=album[0])
        self.app.cloner.album_of = AsyncMock(return_value=album)
        self.app.cloner.send_copy = AsyncMock(return_value=1)
        orig = asyncio.sleep
        asyncio.sleep = AsyncMock()
        try:
            await self.cmd.run("getm", ["https://t.me/kanal/7"], self.ctx)
        finally:
            asyncio.sleep = orig
        sent = [len(c.args[1]) for c in self.app.cloner.send_copy.await_args_list]
        self.assertEqual(sent, [1, 1])  # голосовое пропущено, фото и файл разными пачками
        self.assertIn("Отправлено медиа: 2", self.answer()[1])

    async def test_getm_chat_link_respects_limit(self):
        photo = MessageMediaPhoto(photo=Photo(id=1, access_hash=1, file_reference=b"", date=None, sizes=[], dc_id=1))

        async def history(chat, reply_to=None):
            for _ in range(30):
                yield tl_msg(photo)
                yield tl_msg(text="текст")

        self.app.client.get_entity = AsyncMock(return_value=chan(5, "Канал"))
        self.app.client.iter_messages = history
        self.app.cloner.send_copy = AsyncMock(return_value=1)
        orig = asyncio.sleep
        asyncio.sleep = AsyncMock()
        try:
            await self.cmd.run("getm", ["15", "https://t.me/kanal"], self.ctx)
        finally:
            asyncio.sleep = orig
        sent = [len(c.args[1]) for c in self.app.cloner.send_copy.await_args_list]
        self.assertEqual(sent, [10, 5])
        self.assertIn("лимит 15", self.answer()[1])

    async def _getmu(self, args, history, **kw):
        self.app.client.iter_messages = history
        self.app.cloner.send_copy = AsyncMock(return_value=1)
        for k, v in kw.items():
            setattr(self.app.client, k, v)
        orig = asyncio.sleep
        asyncio.sleep = AsyncMock()
        try:
            await self.cmd.run("getmu", args, self.ctx)
        finally:
            asyncio.sleep = orig
        return [len(c.args[1]) for c in self.app.cloner.send_copy.await_args_list]

    def _media_from(self, sender):
        photo = MessageMediaPhoto(photo=Photo(id=1, access_hash=1, file_reference=b"", date=None, sizes=[], dc_id=1))
        return Message(id=1, peer_id=PeerUser(5), date=None, message="", media=photo, from_id=PeerUser(sender))

    async def test_getmu_message_link_uses_author(self):
        src = self._media_from(7)
        src.get_input_sender = AsyncMock(return_value="author7")
        calls = []

        async def history(chat, from_user=None, reply_to=None, limit=None):
            calls.append(from_user)
            for _ in range(3):
                yield self._media_from(7)

        sent = await self._getmu(["https://t.me/kanal/9"], history,
                                 get_entity=AsyncMock(return_value=chan(5, "Канал")),
                                 get_messages=AsyncMock(return_value=src))
        self.assertEqual(calls, ["author7"])
        self.assertEqual(sent, [3])

    async def test_getmu_chat_and_unknown_id_scans_history(self):
        async def history(chat, from_user=None, reply_to=None, limit=None):
            self.assertIsNone(from_user)
            for sender in (7, 8, 7):
                yield self._media_from(sender)

        async def entity(peer):
            if peer == "kanal":
                return chan(5, "Канал")
            raise ValueError("нет в кэше")

        self.app.cloner._load_dialogs = AsyncMock(return_value=False)
        sent = await self._getmu(["https://t.me/kanal", "7"], history, get_entity=AsyncMock(side_effect=entity))
        self.assertEqual(sent, [2])
        self.assertIn("Отправлено медиа от 7: 2", self.answer()[1])

    async def test_getmu_chat_link_needs_user(self):
        await self.cmd.run("getmu", ["https://t.me/kanal"], self.ctx)
        self.assertIn("укажите вторым параметром", self.answer()[1])

    def test_is_muted(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        later, earlier = datetime(2027, 1, 1, tzinfo=timezone.utc), datetime(2025, 1, 1, tzinfo=timezone.utc)
        S = SimpleNamespace
        self.assertFalse(is_muted(S(mute_until=None), S(mute_until=None), now))
        self.assertTrue(is_muted(S(mute_until=later), None, now))
        self.assertFalse(is_muted(S(mute_until=earlier), S(mute_until=later), now))  # своя настройка важнее
        self.assertTrue(is_muted(S(mute_until=None), S(mute_until=later), now))      # общая для типа чата
        self.assertTrue(is_muted(S(mute_until=2 ** 31 - 1), None, now))

    async def test_unread_only_from_chats_with_notifications(self):
        later = datetime(2100, 1, 1, tzinfo=timezone.utc)
        S = SimpleNamespace

        def dlg(did, unread, muted=False, user=True, mark=False):
            return S(id=did, name=f"чат{did}", unread_count=unread, is_user=user, is_group=not user,
                     input_entity=f"p{did}", dialog=S(unread_mark=mark, read_inbox_max_id=0,
                                                      notify_settings=S(mute_until=later if muted else None)))

        async def dialogs():
            for d in (dlg(1, 3), dlg(2, 5, muted=True), dlg(3, 0), dlg(777000, 1), dlg(-5, 2, user=False),
                      dlg(4, 0, mark=True)):
                yield d

        async def notify(req):
            return S(mute_until=later if type(req.peer).__name__ == "InputNotifyChats" else None)

        self.app.client = MagicMock(side_effect=notify)
        self.app.client.iter_dialogs = dialogs
        self.app.cloner.client = self.app.client
        self.app.exclude_ids = {777000}
        chats = await self.cmd.unread_chats()
        self.assertEqual([(d.id, c) for d, c in chats], [(1, 3), (4, 1)])  # группа -5 заглушена по умолчанию

    async def test_status_and_help(self):
        self.app.me = SimpleNamespace(first_name="Я", last_name=None)
        self.app.client.is_connected = MagicMock(return_value=True)
        await self.cmd.run("status", [], self.ctx)
        self.assertIn("работает", self.answer()[1])
        await self.cmd.run("commands", [], self.ctx)
        self.assertIn(".get lastI", self.answer()[1])
        self.assertIn(".getM", self.answer()[1])
        self.assertTrue(is_command(".getM https://t.me/a/1"))


if __name__ == "__main__":
    unittest.main()
