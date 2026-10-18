"""Основной цикл: приём сообщений, очередь пересылки, сторож соединения,
догрузка пропущенного и автоматический перезапуск клиента."""

import asyncio
import logging
import time

from telethon import TelegramClient, errors, events, utils
from telethon.tl.functions.updates import GetStateRequest
from telethon.tl.types import MessageService

from .cloner import Cloner, SkipMessage
from .config import Config
from .storage import Storage

log = logging.getLogger(__name__)

# Сессия больше не действует — перезапуск не поможет, нужен повторный вход.
FATAL_ERRORS = (
    errors.AuthKeyUnregisteredError,
    errors.AuthKeyDuplicatedError,
    errors.SessionRevokedError,
    errors.SessionExpiredError,
    errors.UserDeactivatedError,
    errors.UserDeactivatedBanError,
)

NETWORK_ERRORS = (ConnectionError, asyncio.TimeoutError)

PING_INTERVAL = 300       # проверка связи с Telegram, сек
TICK = 10                 # как часто сторож смотрит на состояние соединения
MESSAGE_CACHE_LIMIT = 5000


class FatalError(Exception):
    """Ошибка, после которой работать дальше бессмысленно (нет авторизации)."""


class RestartClient(Exception):
    """Клиент завис или потерял связь — пересоздать его."""


def new_client(cfg: Config) -> TelegramClient:
    return TelegramClient(
        str(cfg.session_path),
        cfg.api_id,
        cfg.api_hash,
        connection_retries=10,     # потом клиент отключится, и мы пересоздадим его сами
        retry_delay=5,
        request_retries=5,
        auto_reconnect=True,
        flood_sleep_threshold=60,  # короткие FloodWait Telethon переждёт сам
        device_model="Userbot",
        system_version="Linux",
        app_version="2.0",
    )


class App:
    def __init__(self, cfg: Config, store: Storage):
        self.cfg = cfg
        self.store = store
        self.client = new_client(cfg)
        self.cloner: Cloner | None = None
        self.me_id = None
        self.exclude_ids: set = set()
        self.only_ids: set = set()
        self.cache: dict = {}
        self.wake = asyncio.Event()
        self.catchup_lock = asyncio.Lock()
        self.heartbeat = cfg.data_dir / "heartbeat"
        self._bg: set = set()
        self._bg_error: BaseException | None = None

    # --- запуск ------------------------------------------------------------

    async def run(self):
        log.info("Подключаюсь к Telegram")
        await self.client.connect()
        if not await self.client.is_user_authorized():
            raise FatalError("Сессия не авторизована. Сначала выполните вход: python -m userbot login")

        me = await self.client.get_me()
        self.me_id = me.id
        log.info("Вошли как %s (id %s)", utils.get_display_name(me), me.id)

        self.cloner = Cloner(self.client, self.cfg, self.store)
        self.exclude_ids = await self._resolve_chats(self.cfg.exclude_chats)
        self.only_ids = await self._resolve_chats(self.cfg.only_chats)
        self.store.prune(self.cfg.keep_days)

        self.client.add_event_handler(self.on_message, events.NewMessage())
        self._touch_heartbeat()

        log.info("Клонов: %d, в очереди: %d", len(self.store.sources()), self.store.pending_count())
        self.spawn(self.catch_up(time.time(), "запуск"))

        tasks = [
            asyncio.create_task(self.worker(), name="worker"),
            asyncio.create_task(self.watchdog(), name="watchdog"),
            asyncio.create_task(self.client.run_until_disconnected(), name="client"),
        ]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            if self._bg_error:
                raise self._bg_error
            for t in done:
                exc = t.exception()
                if exc:
                    raise exc
            raise RestartClient(f"задача {next(iter(done)).get_name()} завершилась (клиент отключился)")
        finally:
            for t in tasks + list(self._bg):
                t.cancel()
            await asyncio.gather(*tasks, *self._bg, return_exceptions=True)

    async def close(self):
        try:
            await asyncio.wait_for(self.client.disconnect(), 15)
        except Exception as e:
            log.debug("Ошибка при отключении: %r", e)

    def spawn(self, coro):
        task = asyncio.create_task(coro)
        self._bg.add(task)
        task.add_done_callback(self._bg_done)

    def _bg_done(self, task):
        self._bg.discard(task)
        if task.cancelled() or task.exception() is None:
            return
        exc = task.exception()
        log.error("Фоновая задача упала: %r", exc)
        if isinstance(exc, FATAL_ERRORS):
            # передаём ошибку в run(): отключение завершит задачу клиента
            self._bg_error = exc
            asyncio.ensure_future(self.client.disconnect())

    async def _resolve_chats(self, items) -> set:
        ids = set()
        for item in items:
            if isinstance(item, int):
                ids.add(item)
                continue
            try:
                ids.add(utils.get_peer_id(await self.client.get_input_entity(item)))
            except Exception as e:
                log.warning("Не удалось найти чат %r из настроек: %r", item, e)
        return ids

    def _touch_heartbeat(self):
        try:
            self.heartbeat.write_text(str(int(time.time())))
        except OSError:
            pass

    # --- фильтр и приём ----------------------------------------------------

    def accept(self, message, chat_id: int, chat=None) -> bool:
        if isinstance(message, MessageService):
            return False
        if chat_id in self.cloner.clone_ids:
            return False
        if message.out and not self.cfg.include_outgoing:
            return False
        if chat_id == self.me_id or chat_id in self.exclude_ids:
            return False
        if self.only_ids and chat_id not in self.only_ids:
            return False
        return self.kind(message, chat) in self.cfg.chat_types

    @staticmethod
    def kind(message, chat) -> str:
        if message.is_private:
            return "bot" if getattr(chat, "bot", False) else "private"
        if message.is_group:
            return "group"
        return "channel"

    async def on_message(self, event):
        try:
            message = event.message
            chat_id = event.chat_id
            if chat_id in self.cloner.clone_ids:
                return
            chat = None
            if message.is_private:
                try:
                    chat = await event.get_chat()
                except Exception:
                    pass
            if not self.accept(message, chat_id, chat):
                return
            self.enqueue(chat_id, message)
        except Exception:
            # обработчик никогда не должен ронять приём обновлений
            log.exception("Ошибка в обработчике нового сообщения")

    def enqueue(self, chat_id: int, message) -> bool:
        if not self.store.add_pending(chat_id, message.id):
            return False
        if len(self.cache) < MESSAGE_CACHE_LIMIT:
            self.cache[(chat_id, message.id)] = message
        self.wake.set()
        return True

    # --- очередь пересылки -------------------------------------------------

    async def worker(self):
        while True:
            row = self.store.next_pending()
            if row is None:
                self.wake.clear()
                if self.store.next_pending() is None:
                    try:
                        await asyncio.wait_for(self.wake.wait(), 60)
                    except asyncio.TimeoutError:
                        pass
                continue
            await self.process(row)

    async def process(self, row):
        pending_id, chat_id, msg_id, attempts = row
        key = (chat_id, msg_id)
        try:
            clone_msg_id = await self.cloner.forward(chat_id, msg_id, self.cache.get(key))
        except FATAL_ERRORS:
            raise
        except errors.FloodWaitError as e:
            log.warning("FloodWait %s с, жду", e.seconds)
            await asyncio.sleep(e.seconds + 1)
            return
        except errors.SlowModeWaitError as e:
            await asyncio.sleep(e.seconds + 1)
            return
        except SkipMessage as e:
            log.info("Сообщение %s в чате %s пропущено: %s", msg_id, chat_id, e)
            self.store.drop_pending(pending_id)
            self.cache.pop(key, None)
            return
        except NETWORK_ERRORS as e:
            # нет связи: сообщение остаётся в очереди, ждём восстановления
            log.warning("Нет связи при пересылке (%r), повторю", e)
            await asyncio.sleep(5)
            return
        except asyncio.CancelledError:
            raise
        except Exception as e:
            attempts += 1
            if attempts >= self.cfg.max_retries:
                log.error("Сообщение %s из чата %s не переслано после %d попыток: %r",
                          msg_id, chat_id, attempts, e)
                self.store.mark_failed(pending_id, chat_id, msg_id, repr(e))
                self.cache.pop(key, None)
            else:
                log.warning("Ошибка пересылки %s/%s (попытка %d): %r", chat_id, msg_id, attempts, e)
                self.store.bump_attempts(pending_id)
                await asyncio.sleep(min(60, 2 ** attempts))
            return

        self.store.mark_forwarded(chat_id, msg_id, clone_msg_id)
        self.cache.pop(key, None)
        log.debug("Переслано %s/%s", chat_id, msg_id)

    # --- догрузка пропущенного ---------------------------------------------

    async def catch_up(self, before_ts: float, reason: str):
        """Для каждого чата с клоном забирает сообщения новее последнего
        обработанного до before_ts и ставит их в очередь (дубли отсекаются)."""
        if self.catchup_lock.locked():
            return
        async with self.catchup_lock:
            total = 0
            for chat_id in self.store.sources():
                last = self.store.last_forwarded_id(chat_id, before_ts)
                if last is None or chat_id in self.exclude_ids:
                    continue
                try:
                    entity = await self.cloner.resolve(chat_id)
                    async for m in self.client.iter_messages(
                        entity, min_id=last, reverse=True, limit=self.cfg.catchup_limit
                    ):
                        if self.accept(m, chat_id, m.chat) and self.enqueue(chat_id, m):
                            total += 1
                except errors.FloodWaitError as e:
                    log.warning("FloodWait %s с при догрузке", e.seconds)
                    await asyncio.sleep(e.seconds + 1)
                except FATAL_ERRORS:
                    raise
                except NETWORK_ERRORS as e:
                    log.warning("Догрузка (%s) прервана: нет связи (%r)", reason, e)
                    return
                except Exception as e:
                    log.debug("Догрузка чата %s не удалась: %r", chat_id, e)
                await asyncio.sleep(0.5)  # бережём лимиты API
            if total:
                log.info("Догрузка (%s): найдено пропущенных сообщений: %d", reason, total)

    # --- сторож ------------------------------------------------------------

    async def watchdog(self):
        down_since = None
        last_ping = time.monotonic()
        last_catchup = time.time()
        ping_fails = 0
        while True:
            await asyncio.sleep(TICK)

            if not self.client.is_connected():
                if down_since is None:
                    down_since = time.time()
                    log.warning("Соединение с Telegram потеряно, жду переподключения")
                elif time.time() - down_since > self.cfg.max_disconnect:
                    raise RestartClient("нет соединения дольше MAX_DISCONNECT_MIN")
                continue

            if down_since is not None:
                log.info("Соединение восстановлено, догружаю пропущенное")
                self.spawn(self.catch_up(down_since, "после разрыва"))
                down_since = None
                last_ping = 0  # сразу проверить, что обновления снова идут

            if time.monotonic() - last_ping >= PING_INTERVAL:
                last_ping = time.monotonic()
                try:
                    # GetState и проверяет связь, и «будит» поток обновлений
                    await asyncio.wait_for(self.client(GetStateRequest()), 30)
                    ping_fails = 0
                    self._touch_heartbeat()
                except FATAL_ERRORS:
                    raise
                except errors.FloodWaitError:
                    pass
                except Exception as e:
                    ping_fails += 1
                    log.warning("Проверка связи не прошла (%d/3): %r", ping_fails, e)
                    if ping_fails >= 3:
                        raise RestartClient("Telegram не отвечает")

            # подстраховка от «тихой» остановки обновлений
            if self.cfg.catchup_interval and time.time() - last_catchup >= self.cfg.catchup_interval:
                last_catchup = time.time()
                self.spawn(self.catch_up(time.time(), "плановая"))


async def run_forever(cfg: Config):
    store = Storage(cfg.db_path)
    backoff = 5
    try:
        while True:
            app = App(cfg, store)
            started = time.monotonic()
            try:
                await app.run()
            except FATAL_ERRORS as e:
                raise FatalError(f"Сессия недействительна ({type(e).__name__}). Войдите заново: python -m userbot login")
            except (FatalError, asyncio.CancelledError):
                raise
            except RestartClient as e:
                log.warning("Перезапуск клиента: %s", e)
            except Exception:
                log.exception("Непредвиденная ошибка, перезапускаю клиент")
            finally:
                await app.close()

            if time.monotonic() - started > 600:
                backoff = 5
            log.info("Повторное подключение через %d с", backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 300)
    finally:
        store.close()
