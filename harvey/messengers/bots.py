"""Conversations people start: a Telegram bot, a VK community, a WhatsApp
Business number.

Each platform's own API, with the platform's own rule that the person writes
first: a Telegram bot can't open a chat, a VK community can write only to
people who wrote to it or allowed its messages, and WhatsApp allows free
text only within 24 hours of their last message. Nothing here can start a
conversation with a stranger.

Every client has the same three calls: ``fetch`` (new messages), ``send``
(an answer) and ``test`` (is the token good).
"""

import logging
import random
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

logger = logging.getLogger("harvey.messengers")

TIMEOUT = httpx.Timeout(20.0, connect=10.0)
# Sent in place of a message with no text (a photo, a voice note, a file):
# Harvey can't read it, so a person answers.
NO_TEXT = "[сообщение без текста: фото, голосовое или файл]"


@dataclass
class Inbound:
    chat_id: str
    body: str
    provider_msg_id: str
    at: datetime
    handle: str = ""
    display_name: str = ""
    is_start: bool = False      # Telegram's /start: they pressed Start
    has_text: bool = True


@dataclass
class SendResult:
    ok: bool
    provider_msg_id: str = ""
    error: str = ""


def _utc(ts) -> datetime:
    try:
        return datetime.fromtimestamp(int(ts), timezone.utc).replace(tzinfo=None)
    except (TypeError, ValueError, OverflowError):
        return datetime.now(timezone.utc).replace(tzinfo=None)


class _Client:
    channel = ""

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None):
        self._transport = transport

    def _http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=TIMEOUT, transport=self._transport)

    async def fetch(self, state) -> list[Inbound]:
        return []

    async def commit(self, state) -> None:
        """Called once fetched messages are stored: the platform may forget them."""


class TelegramBot(_Client):
    """Telegram Bot API, by long polling (getUpdates): no public address needed."""

    channel = "telegram"
    OFFSET_KEY = "messenger_telegram_offset"

    def __init__(self, token: str, transport=None):
        super().__init__(transport)
        self.token = token
        self._next_offset: int | None = None

    def _url(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self.token}/{method}"

    async def _call(self, method: str, payload: dict) -> dict:
        async with self._http() as http:
            r = await http.post(self._url(method), json=payload)
        try:
            data = r.json()
        except ValueError:
            raise RuntimeError(f"Telegram {method}: HTTP {r.status_code}")
        if not data.get("ok"):
            raise RuntimeError(f"Telegram {method}: {data.get('description') or r.status_code}")
        return data

    async def fetch(self, state) -> list[Inbound]:
        offset = int(await state.get_setting(self.OFFSET_KEY, "0") or 0)
        payload = {"timeout": 0, "allowed_updates": ["message"], "limit": 100}
        if offset:
            payload["offset"] = offset
        data = await self._call("getUpdates", payload)
        out: list[Inbound] = []
        top = offset - 1
        for upd in data.get("result") or []:
            top = max(top, int(upd.get("update_id", 0)))
            msg = upd.get("message") or {}
            chat, sender = msg.get("chat") or {}, msg.get("from") or {}
            # Private chats with people only: not groups, not other bots.
            if chat.get("type") != "private" or sender.get("is_bot"):
                continue
            text = msg.get("text") or msg.get("caption") or ""
            name = " ".join(p for p in (sender.get("first_name"), sender.get("last_name")) if p)
            out.append(Inbound(
                chat_id=str(chat.get("id")),
                body=text or NO_TEXT,
                provider_msg_id=str(msg.get("message_id", "")),
                at=_utc(msg.get("date")),
                handle=sender.get("username") or "",
                display_name=name,
                is_start=text.strip().split(" ")[0] == "/start" if text else False,
                has_text=bool(text),
            ))
        self._next_offset = top + 1 if top >= offset else None
        return out

    async def commit(self, state) -> None:
        if self._next_offset:
            await state.set_setting(self.OFFSET_KEY, str(self._next_offset))
            self._next_offset = None

    async def send(self, chat_id: str, text: str) -> SendResult:
        try:
            data = await self._call("sendMessage", {"chat_id": chat_id, "text": text})
        except (RuntimeError, httpx.HTTPError) as e:
            return SendResult(False, error=str(e))
        return SendResult(True, str((data.get("result") or {}).get("message_id", "")))

    async def test(self) -> tuple[bool, str]:
        try:
            data = await self._call("getMe", {})
        except (RuntimeError, httpx.HTTPError) as e:
            return False, str(e)
        return True, "@" + str((data.get("result") or {}).get("username", "?"))


class VkGroup(_Client):
    """A VK community's messages through the VK API (a community token with
    messages access). Reads conversations VK marks unanswered."""

    channel = "vk"

    def __init__(self, token: str, api_version: str = "5.199", transport=None):
        super().__init__(transport)
        self.token = token
        self.v = api_version

    async def _call(self, method: str, params: dict) -> dict | list:
        async with self._http() as http:
            r = await http.post(f"https://api.vk.com/method/{method}",
                                data={**params, "access_token": self.token, "v": self.v})
        try:
            data = r.json()
        except ValueError:
            raise RuntimeError(f"VK {method}: HTTP {r.status_code}")
        if "error" in data:
            err = data["error"] or {}
            raise RuntimeError(f"VK {method}: {err.get('error_msg') or err.get('error_code')}")
        return data.get("response")

    async def fetch(self, state) -> list[Inbound]:
        convs = await self._call("messages.getConversations",
                                 {"filter": "unanswered", "count": 50, "extended": 1})
        convs = convs or {}
        names = {p.get("id"): " ".join(x for x in (p.get("first_name"), p.get("last_name")) if x)
                 for p in convs.get("profiles") or []}
        screen = {p.get("id"): p.get("screen_name") or "" for p in convs.get("profiles") or []}
        out: list[Inbound] = []
        for item in convs.get("items") or []:
            peer = ((item.get("conversation") or {}).get("peer") or {})
            if peer.get("type") != "user":
                continue
            peer_id = peer.get("id")
            history = await self._call("messages.getHistory", {"peer_id": peer_id, "count": 20})
            for msg in reversed((history or {}).get("items") or []):
                if msg.get("out"):
                    continue
                text = msg.get("text") or ""
                out.append(Inbound(
                    chat_id=str(peer_id), body=text or NO_TEXT,
                    provider_msg_id=str(msg.get("id", "")), at=_utc(msg.get("date")),
                    handle=screen.get(peer_id, ""), display_name=names.get(peer_id, ""),
                    has_text=bool(text),
                ))
        return out

    async def send(self, chat_id: str, text: str) -> SendResult:
        try:
            mid = await self._call("messages.send", {
                "peer_id": chat_id, "message": text,
                # VK drops a second send with the same random_id: a retry
                # can't double-send.
                "random_id": random.randint(1, 2**31 - 1)})
        except (RuntimeError, httpx.HTTPError) as e:
            return SendResult(False, error=str(e))
        return SendResult(True, str(mid if not isinstance(mid, (list, dict)) else ""))

    async def test(self) -> tuple[bool, str]:
        try:
            resp = await self._call("groups.getById", {})
        except (RuntimeError, httpx.HTTPError) as e:
            return False, str(e)
        groups = resp.get("groups") if isinstance(resp, dict) else resp
        name = (groups or [{}])[0].get("name", "?") if groups else "?"
        return True, name


class WhatsAppCloud(_Client):
    """WhatsApp Business through Meta's Cloud API. Messages arrive at the
    webhook (harvey/messengers/whatsapp_webhook.py), not here."""

    channel = "whatsapp"
    # Free text is allowed this long after their last message; after that
    # only a template Meta approved, which Harvey doesn't send.
    WINDOW_HOURS = 24

    def __init__(self, token: str, phone_number_id: str, api_version: str = "v21.0",
                 transport=None):
        super().__init__(transport)
        self.token = token
        self.phone_number_id = phone_number_id
        self.v = api_version

    def _base(self) -> str:
        return f"https://graph.facebook.com/{self.v}/{self.phone_number_id}"

    async def send(self, chat_id: str, text: str) -> SendResult:
        try:
            async with self._http() as http:
                r = await http.post(
                    self._base() + "/messages",
                    headers={"Authorization": f"Bearer {self.token}"},
                    json={"messaging_product": "whatsapp", "recipient_type": "individual",
                          "to": chat_id, "type": "text",
                          "text": {"body": text, "preview_url": False}})
            data = r.json()
        except (httpx.HTTPError, ValueError) as e:
            return SendResult(False, error=f"WhatsApp: {e}")
        if r.status_code >= 400 or "error" in data:
            err = data.get("error") or {}
            return SendResult(False, error=f"WhatsApp: {err.get('message') or r.status_code}")
        ids = [m.get("id", "") for m in data.get("messages") or []]
        return SendResult(True, ids[0] if ids else "")

    async def test(self) -> tuple[bool, str]:
        try:
            async with self._http() as http:
                r = await http.get(self._base(), params={"fields": "display_phone_number,verified_name"},
                                   headers={"Authorization": f"Bearer {self.token}"})
            data = r.json()
        except (httpx.HTTPError, ValueError) as e:
            return False, f"WhatsApp: {e}"
        if r.status_code >= 400 or "error" in data:
            return False, f"WhatsApp: {(data.get('error') or {}).get('message') or r.status_code}"
        return True, f"{data.get('verified_name', '?')} {data.get('display_phone_number', '')}".strip()


def configured_bots(config, env, transport=None) -> dict[str, _Client]:
    """The clients turned on in channels.messengers that have their keys."""
    cfg = config.channels.messengers
    bots: dict[str, _Client] = {}
    if cfg.telegram.enabled and env.telegram_bot_token:
        bots["telegram"] = TelegramBot(env.telegram_bot_token, transport=transport)
    if cfg.vk.enabled and env.vk_group_token:
        bots["vk"] = VkGroup(env.vk_group_token, cfg.vk.api_version, transport=transport)
    if cfg.whatsapp.enabled and env.whatsapp_token and cfg.whatsapp.phone_number_id:
        bots["whatsapp"] = WhatsAppCloud(env.whatsapp_token, cfg.whatsapp.phone_number_id,
                                         cfg.whatsapp.api_version, transport=transport)
    return bots


def missing_keys(config, env) -> list[str]:
    """What a turned-on messenger still needs, in words."""
    cfg = config.channels.messengers
    out = []
    if cfg.telegram.enabled and not env.telegram_bot_token:
        out.append("Telegram: TELEGRAM_BOT_TOKEN is not set in .env")
    if cfg.vk.enabled and not env.vk_group_token:
        out.append("VK: VK_GROUP_TOKEN is not set in .env")
    if cfg.whatsapp.enabled:
        if not env.whatsapp_token:
            out.append("WhatsApp: WHATSAPP_TOKEN is not set in .env")
        if not cfg.whatsapp.phone_number_id:
            out.append("WhatsApp: channels.messengers.whatsapp.phone_number_id is not set")
        if not env.whatsapp_app_secret or not env.whatsapp_verify_token:
            out.append("WhatsApp: WHATSAPP_APP_SECRET and WHATSAPP_VERIFY_TOKEN are needed "
                       "for the webhook that receives messages")
    return out
