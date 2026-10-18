"""Точка входа.

    python -m userbot          — запуск бота
    python -m userbot login    — первый вход в аккаунт (интерактивно)
    python -m userbot status   — клоны, очередь и ошибки из базы
"""

import asyncio
import logging
import signal
import sys
import time
from logging.handlers import RotatingFileHandler

from . import config
from .app import FatalError, new_client, run_forever

EXIT_FATAL = 3  # systemd не перезапускает бота с этим кодом (RestartPreventExitStatus)


def setup_logging(cfg):
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(cfg.log_level)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)
    file = RotatingFileHandler(cfg.data_dir / "userbot.log", maxBytes=5 * 1024 * 1024,
                               backupCount=3, encoding="utf-8")
    file.setFormatter(fmt)
    root.addHandler(file)
    logging.getLogger("telethon").setLevel(logging.WARNING)


async def login(cfg):
    client = new_client(cfg)
    await client.start()  # спросит телефон, код и пароль 2FA
    me = await client.get_me()
    print(f"Готово, вход выполнен: {me.first_name} (id {me.id}). Сессия: {cfg.session_path}.session")
    await client.disconnect()


def status(cfg):
    from .storage import Storage
    store = Storage(cfg.db_path)
    db = store.db
    print(f"Клон личек: {store.private_clone() or 'ещё не создан'}")
    print(f"Пересылка из групп: {'вкл' if store.group_forwarding() else 'выкл'}")
    print(f"Клонов групп: {len(store.groups())}")
    for source_id, clone_id, title in store.groups():
        print(f"  {source_id} → {clone_id}  {title}")
    print(f"Чёрный список: {len(store.blacklist())}")
    for user_id, name in store.blacklist():
        print(f"  {user_id}  {name}")
    print(f"В очереди: {store.pending_count()}")
    rows = db.execute("SELECT source_id, msg_id, error, failed_at FROM failed ORDER BY failed_at DESC LIMIT 20").fetchall()
    print(f"Не переслано (последние {len(rows)}):")
    for source_id, msg_id, error, ts in rows:
        print(f"  {time.strftime('%Y-%m-%d %H:%M', time.localtime(ts))}  {source_id}/{msg_id}: {error}")
    store.close()


async def main_async(cfg):
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, task.cancel)
        except NotImplementedError:  # Windows
            pass
    await run_forever(cfg)


def main():
    cfg = config.load()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"

    if cmd == "login":
        asyncio.run(login(cfg))
        return
    if cmd == "status":
        status(cfg)
        return
    if cmd != "run":
        sys.exit(__doc__)

    setup_logging(cfg)
    log = logging.getLogger("userbot")
    log.info("Юзербот запускается")
    try:
        asyncio.run(main_async(cfg))
    except (asyncio.CancelledError, KeyboardInterrupt):
        log.info("Остановлен по сигналу")
    except FatalError as e:
        log.critical("%s", e)
        if cfg.fatal_exit_delay:
            time.sleep(cfg.fatal_exit_delay)
        sys.exit(EXIT_FATAL)


if __name__ == "__main__":
    main()
