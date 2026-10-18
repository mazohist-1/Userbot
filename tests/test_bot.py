"""Тесты логики без подключения к Telegram: python -m unittest discover tests"""

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

os.environ.setdefault("API_ID", "1")
os.environ.setdefault("API_HASH", "x")

from telethon import errors  # noqa: E402

from userbot import config  # noqa: E402
from userbot.app import App  # noqa: E402
from userbot.cloner import Cloner, SkipMessage  # noqa: E402
from userbot.storage import Storage  # noqa: E402


def make_cfg(tmp):
    os.environ["DATA_DIR"] = tmp
    return config.load()


def msg(mid, out=False, private=True, group=False):
    return SimpleNamespace(id=mid, out=out, is_private=private, is_group=group, chat=None)


def rpc(cls, **kw):
    e = cls(request=None, **kw) if kw else cls(request=None)
    return e


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
        import time
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

    def test_clone_mapping_persists(self):
        self.s.set_clone(1, -1001, "t")
        self.s.close()
        s2 = Storage(Path(self.tmp) / "t.db")
        self.assertEqual(s2.get_clone(1), -1001)
        self.assertEqual(s2.clone_ids(), {-1001})


class ClonerTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = make_cfg(self.tmp)
        self.store = Storage(self.cfg.db_path)
        self.client = MagicMock()
        self.client.get_input_entity = AsyncMock(side_effect=lambda x: f"peer{x}")
        self.client.get_messages = AsyncMock(return_value=[])
        self.cloner = Cloner(self.client, self.cfg, self.store)
        self.cloner._create_clone = AsyncMock(side_effect=self._create)
        self.created = 0

    async def _create(self, source_id):
        self.created += 1
        cid = -100000 - self.created
        self.store.set_clone(source_id, cid, "t")
        self.cloner.clone_ids.add(cid)
        return f"clone{cid}"

    async def test_creates_clone_once(self):
        self.client.forward_messages = AsyncMock(return_value=SimpleNamespace(id=7))
        self.assertEqual(await self.cloner.forward(1, 10), 7)
        self.assertEqual(await self.cloner.forward(1, 11), 7)
        self.assertEqual(self.created, 1)

    async def test_recreates_deleted_clone(self):
        self.client.forward_messages = AsyncMock(
            side_effect=[rpc(errors.ChannelPrivateError), SimpleNamespace(id=8)])
        self.client.get_messages = AsyncMock(side_effect=rpc(errors.ChannelPrivateError))
        self.assertEqual(await self.cloner.forward(1, 10), 8)
        self.assertEqual(self.created, 2)

    async def test_source_gone_does_not_recreate(self):
        self.client.forward_messages = AsyncMock(side_effect=rpc(errors.ChannelPrivateError))
        with self.assertRaises(SkipMessage):
            await self.cloner.forward(1, 10)
        self.assertEqual(self.created, 1)

    async def test_forward_restricted_copies(self):
        self.client.forward_messages = AsyncMock(side_effect=rpc(errors.ChatForwardsRestrictedError))
        self.cloner.copy = AsyncMock(return_value=SimpleNamespace(id=9))
        m = SimpleNamespace(id=10)
        self.assertEqual(await self.cloner.forward(1, 10, m), 9)
        self.cloner.copy.assert_awaited_once()

    async def test_deleted_message_skipped(self):
        self.client.forward_messages = AsyncMock(side_effect=rpc(errors.MessageIdInvalidError))
        with self.assertRaises(SkipMessage):
            await self.cloner.forward(1, 10)


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
        self.assertFalse(a.accept(msg(1, private=False), -1001))  # канал не в CHAT_TYPES
        self.assertTrue(a.accept(msg(1, private=False, group=True), -1002))
        self.assertFalse(a.accept(msg(1), 1, SimpleNamespace(bot=True)))

    async def test_success_marks_forwarded(self):
        self.app.cloner.forward = AsyncMock(return_value=5)
        self.app.enqueue(1, msg(10))
        await self.app.process(self.store.next_pending())
        self.assertTrue(self.store.is_forwarded(1, 10))
        self.assertEqual(self.app.cache, {})

    async def test_floodwait_keeps_item(self):
        e = errors.FloodWaitError(request=None, capture=0)
        self.app.cloner.forward = AsyncMock(side_effect=e)
        self.app.enqueue(1, msg(10))
        await self.app.process(self.store.next_pending())
        self.assertEqual(self.store.next_pending()[3], 0)

    async def test_network_error_keeps_item(self):
        self.app.cloner.forward = AsyncMock(side_effect=ConnectionError("down"))
        self.app.enqueue(1, msg(10))
        orig = asyncio.sleep
        asyncio.sleep = AsyncMock()
        try:
            await self.app.process(self.store.next_pending())
        finally:
            asyncio.sleep = orig
        self.assertIsNotNone(self.store.next_pending())

    async def test_retries_then_failed(self):
        self.app.cloner.forward = AsyncMock(side_effect=ValueError("boom"))
        self.app.enqueue(1, msg(10))
        orig = asyncio.sleep
        asyncio.sleep = AsyncMock()
        try:
            for _ in range(self.cfg.max_retries):
                await self.app.process(self.store.next_pending())
        finally:
            asyncio.sleep = orig
        self.assertIsNone(self.store.next_pending())
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM failed").fetchone()[0], 1)

    async def test_fatal_propagates(self):
        self.app.cloner.forward = AsyncMock(side_effect=rpc(errors.AuthKeyUnregisteredError))
        self.app.enqueue(1, msg(10))
        with self.assertRaises(errors.AuthKeyUnregisteredError):
            await self.app.process(self.store.next_pending())

    async def test_catch_up_enqueues_missed_only(self):
        self.store.set_clone(1, -500, "t")
        self.store.mark_forwarded(1, 10, None)
        self.store.mark_forwarded(1, 12, None)  # пришло вживую после разрыва
        self.store.db.execute("UPDATE forwarded SET done_at=100 WHERE msg_id=10")
        self.store.db.execute("UPDATE forwarded SET done_at=300 WHERE msg_id=12")

        async def history(entity, min_id, reverse, limit):
            self.assertEqual(min_id, 10)
            for i in (11, 12, 13):
                yield msg(i)

        self.app.client = MagicMock()
        self.app.client.iter_messages = history
        self.app.cloner.resolve = AsyncMock(return_value="peer")
        await self.app.catch_up(200, "тест")
        queued = [r[0] for r in self.store.db.execute("SELECT msg_id FROM pending ORDER BY id")]
        self.assertEqual(queued, [11, 13])


if __name__ == "__main__":
    unittest.main()
