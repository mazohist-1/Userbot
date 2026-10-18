"""Разбор ссылок на сообщения и чаты Telegram (для команды .get)."""

import re
from dataclasses import dataclass
from typing import Optional, Union
from urllib.parse import parse_qs, urlparse

HOSTS = {"t.me", "telegram.me", "telegram.dog"}
USERNAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,}$")


class LinkError(ValueError):
    """Ссылку не удалось разобрать."""


@dataclass(frozen=True)
class Link:
    chat: Union[str, int]           # @username без @ или id вида -100…
    msg_id: Optional[int] = None
    thread: Optional[int] = None    # id темы (форума) или ветки комментариев


def channel_peer_id(raw: int) -> int:
    """id из ссылки t.me/c/<id>/… → id чата, как его видит Telethon (-100…)."""
    return -(10 ** 12 + raw)


def _int(value, what: str) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise LinkError(f"{what}: ожидалось число, получено {value!r}")
    if n <= 0:
        raise LinkError(f"{what}: должно быть больше нуля")
    return n


def _query_thread(q: dict) -> Optional[int]:
    for key in ("thread", "topic"):
        if q.get(key):
            return _int(q[key][0], key)
    return None


def _username(name: str) -> str:
    if not USERNAME.match(name):
        raise LinkError(f"некорректное имя чата: {name!r}")
    return name


def parse_link(raw: str) -> Link:
    s = raw.strip().strip("<>").strip()
    if not s:
        raise LinkError("пустая ссылка")

    if s.startswith("@"):
        return Link(_username(s[1:]))

    if s.lower().startswith("tg://"):
        u = urlparse(s)
        q = parse_qs(u.query)
        action = (u.netloc or u.path.strip("/")).lower()
        post = _int(q["post"][0], "post") if q.get("post") else None
        if action == "resolve":
            if not q.get("domain"):
                raise LinkError("в ссылке tg://resolve нет domain")
            return Link(_username(q["domain"][0]), post, _query_thread(q))
        if action == "privatepost":
            if not q.get("channel"):
                raise LinkError("в ссылке tg://privatepost нет channel")
            return Link(channel_peer_id(_int(q["channel"][0], "channel")), post, _query_thread(q))
        raise LinkError(f"ссылки tg://{action} не поддерживаются")

    if not re.match(r"^https?://", s, re.I):
        s = "https://" + s
    u = urlparse(s)
    host = u.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    if host not in HOSTS:
        raise LinkError("это не ссылка t.me")

    parts = [p for p in u.path.split("/") if p]
    if parts and parts[0] == "s":  # веб-превью канала t.me/s/name
        parts = parts[1:]
    if not parts:
        raise LinkError("в ссылке нет чата")
    if parts[0].startswith("+") or parts[0].lower() == "joinchat":
        raise LinkError("ссылки-приглашения не поддерживаются, нужна ссылка на сообщение")

    if parts[0] == "c":
        if len(parts) < 2:
            raise LinkError("в ссылке t.me/c/ нет id чата")
        chat = channel_peer_id(_int(parts[1], "id чата"))
        rest = parts[2:]
    else:
        chat = _username(parts[0])
        rest = parts[1:]

    nums = [_int(x, "id сообщения") for x in rest]
    if len(nums) > 2:
        raise LinkError("слишком много чисел в ссылке")
    thread = msg_id = None
    if len(nums) == 1:
        msg_id = nums[0]
    elif len(nums) == 2:
        thread, msg_id = nums
    thread = _query_thread(parse_qs(u.query)) or thread
    return Link(chat, msg_id, thread)
