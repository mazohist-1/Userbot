"""Загрузка настроек из переменных окружения / файла .env."""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

CHAT_KINDS = {"private", "bot", "group", "channel"}


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "да"}


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw.strip())


def _list(name: str, default: str = "") -> list[str]:
    raw = os.getenv(name, default)
    return [x.strip() for x in raw.replace(";", ",").split(",") if x.strip()]


def _chats(name: str, default: str = "") -> frozenset:
    """Список чатов: числовые id (как int) или @username (как str)."""
    out = set()
    for item in _list(name, default):
        try:
            out.add(int(item))
        except ValueError:
            out.add(item.lstrip("@").lower())
    return frozenset(out)


@dataclass(frozen=True)
class Config:
    api_id: int
    api_hash: str
    data_dir: Path
    session_path: Path
    db_path: Path

    chat_types: frozenset
    include_outgoing: bool
    exclude_chats: frozenset
    only_chats: frozenset

    clone_prefix: str
    archive_clones: bool

    catchup_interval: int
    catchup_limit: int
    max_retries: int
    copy_media_max_mb: int
    keep_days: int
    max_disconnect: int
    fatal_exit_delay: int
    log_level: str


def load() -> Config:
    load_dotenv()

    api_id = os.getenv("API_ID", "").strip()
    api_hash = os.getenv("API_HASH", "").strip()
    if not api_id or not api_hash:
        raise SystemExit("Не заданы API_ID и API_HASH (см. README и .env.example)")

    data_dir = Path(os.getenv("DATA_DIR", "data")).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)

    chat_types = frozenset(t.lower() for t in _list("CHAT_TYPES", "private,group"))
    unknown = chat_types - CHAT_KINDS
    if unknown:
        raise SystemExit(f"CHAT_TYPES: неизвестные типы {sorted(unknown)}, допустимы {sorted(CHAT_KINDS)}")

    return Config(
        api_id=int(api_id),
        api_hash=api_hash,
        data_dir=data_dir,
        session_path=data_dir / os.getenv("SESSION_NAME", "userbot"),
        db_path=data_dir / "state.db",
        chat_types=chat_types,
        include_outgoing=_bool("INCLUDE_OUTGOING", False),
        # 777000 — служебный чат Telegram с кодами входа. Если переслать код
        # куда-либо, Telegram его аннулирует, поэтому он исключён по умолчанию.
        exclude_chats=_chats("EXCLUDE_CHATS") | {777000},
        only_chats=_chats("ONLY_CHATS"),
        clone_prefix=os.getenv("CLONE_PREFIX", "📥 "),
        archive_clones=_bool("ARCHIVE_CLONES", True),
        catchup_interval=_int("CATCHUP_INTERVAL_MIN", 30) * 60,
        catchup_limit=_int("CATCHUP_LIMIT", 500),
        max_retries=_int("MAX_RETRIES", 5),
        copy_media_max_mb=_int("COPY_MEDIA_MAX_MB", 200),
        keep_days=_int("KEEP_DAYS", 30),
        max_disconnect=_int("MAX_DISCONNECT_MIN", 5) * 60,
        fatal_exit_delay=_int("FATAL_EXIT_DELAY_SEC", 0),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
    )
