"""Загрузка настроек из переменных окружения / файла .env."""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

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

    forward_private: bool
    forward_bots: bool
    include_outgoing: bool
    exclude_chats: frozenset
    auto_create_clones: bool

    private_clone_title: str
    group_clone_prefix: str
    archive_clones: bool

    cmd_reply: str
    get_limit: int
    getm_limit: int
    search_limit: int

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

    cmd_reply = os.getenv("CMD_REPLY", "saved").strip().lower()
    if cmd_reply not in {"saved", "here"}:
        raise SystemExit("CMD_REPLY: допустимо saved или here")

    return Config(
        api_id=int(api_id),
        api_hash=api_hash,
        data_dir=data_dir,
        session_path=data_dir / os.getenv("SESSION_NAME", "userbot"),
        db_path=data_dir / "state.db",
        forward_private=_bool("FORWARD_PRIVATE", True),
        forward_bots=_bool("FORWARD_BOTS", False),
        include_outgoing=_bool("INCLUDE_OUTGOING", False),
        # 777000 — служебный чат Telegram с кодами входа. Если переслать код
        # куда-либо, Telegram его аннулирует, поэтому он исключён по умолчанию.
        exclude_chats=_chats("EXCLUDE_CHATS") | {777000},
        auto_create_clones=_bool("AUTO_CREATE_CLONES", False),
        private_clone_title=os.getenv("PRIVATE_CLONE_TITLE", "Private Messages").strip() or "Private Messages",
        group_clone_prefix=os.getenv("GROUP_CLONE_PREFIX", "Clone: "),
        archive_clones=_bool("ARCHIVE_CLONES", False),
        cmd_reply=cmd_reply,
        get_limit=_int("GET_LIMIT", 100),
        getm_limit=_int("GETM_LIMIT", 500),
        search_limit=_int("SEARCH_LIMIT", 20),
        catchup_interval=_int("CATCHUP_INTERVAL_MIN", 30) * 60,
        catchup_limit=_int("CATCHUP_LIMIT", 500),
        max_retries=_int("MAX_RETRIES", 5),
        copy_media_max_mb=_int("COPY_MEDIA_MAX_MB", 200),
        keep_days=_int("KEEP_DAYS", 30),
        max_disconnect=_int("MAX_DISCONNECT_MIN", 5) * 60,
        fatal_exit_delay=_int("FATAL_EXIT_DELAY_SEC", 0),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
    )
