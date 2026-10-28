"""Messenger contacts a business publishes on its own site.

Only links and attributes the site itself carries count: a ``t.me/name``
link, a ``wa.me/79990001122`` button, a ``vk.com/club123`` icon. A bare
phone number is never taken to be a WhatsApp or Telegram account, since
nothing on the page says it is one.
"""

import re
from html import unescape
from urllib.parse import parse_qs, quote, unquote, urlparse

CHANNELS = ("telegram", "whatsapp", "vk")

# One signal per messenger, in harvey.signals.SIGNAL_CATALOG.
SIGNAL_FOR = {
    "telegram": "MESSENGER_TELEGRAM",
    "whatsapp": "MESSENGER_WHATSAPP",
    "vk": "MESSENGER_VK",
}
CHANNEL_FOR = {code: ch for ch, code in SIGNAL_FOR.items()}

CHANNEL_LABELS = {"telegram": "Telegram", "whatsapp": "WhatsApp", "vk": "ВКонтакте"}

_URL_RE = re.compile(
    r"""(?:https?:)?//[^\s"'<>()]+|\b(?:t\.me|telegram\.me|wa\.me|vk\.me|vk\.com|vk\.ru|"""
    r"""api\.whatsapp\.com|web\.whatsapp\.com)/[^\s"'<>()]*|"""
    r"""\b(?:tg|whatsapp)://[^\s"'<>()]+""",
    re.IGNORECASE)

# t.me paths that aren't a person or a business account.
_TG_RESERVED = {
    "joinchat", "addstickers", "addemoji", "share", "proxy", "socks", "s", "c",
    "iv", "login", "setlanguage", "addtheme", "confirmphone", "bg", "invoice",
    "contact", "boost", "m", "telegram", "durov", "username",
}
_TG_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")

# vk.com paths that are pages of VK itself, not someone's page.
_VK_RESERVED = {
    "share.php", "away.php", "js", "images", "css", "doc", "docs", "video",
    "audio", "music", "feed", "im", "login", "search", "apps", "app", "friends",
    "groups", "dev", "support", "terms", "privacy", "about", "blog", "jobs",
    "widget_community.php", "widget_comments.php", "widgets", "legal", "help",
    "settings", "write", "mail", "wall", "photo", "album", "topic", "market",
    "services", "clips", "vkpay", "pay", "id", "public", "club", "event",
}
_VK_NAME_RE = re.compile(r"^[A-Za-z0-9_.]{2,64}$")
_VK_NUMERIC_RE = re.compile(r"^(?:club|public|event)(\d{1,12})$", re.IGNORECASE)


def normalize_phone(raw: str) -> str:
    """Digits only, in international form: Russian 8XXXXXXXXXX becomes 7XXXXXXXXXX.
    "" when it can't be a phone number."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    elif len(digits) == 10 and digits.startswith("9"):
        digits = "7" + digits   # a Russian mobile written without the country code
    return digits if 10 <= len(digits) <= 15 else ""


def _telegram(url) -> str:
    host = url.netloc.lower().removeprefix("www.")
    if url.scheme == "tg":
        query = parse_qs(url.query)
        if url.netloc.lower() == "resolve" and query.get("domain"):
            name = query["domain"][0]
            return name if _TG_USERNAME_RE.match(name) and not name.lower().endswith("bot") else ""
        if query.get("phone"):
            phone = normalize_phone(query["phone"][0])
            return f"+{phone}" if phone else ""
        return ""
    if host not in ("t.me", "telegram.me", "telegram.dog"):
        return ""
    parts = [p for p in unquote(url.path).split("/") if p]
    if not parts:
        return ""
    first = parts[0]
    if first.startswith("+"):
        phone = normalize_phone(first)
        # A phone link (t.me/+79990001122) opens a chat; an invite link
        # (t.me/+AbCdEf) joins a group, which isn't a contact.
        return f"+{phone}" if phone and re.fullmatch(r"\+\d+", first) else ""
    if first.lower() in _TG_RESERVED or not _TG_USERNAME_RE.match(first):
        return ""
    # A bot answers by script and a channel can't be written to; a bot's
    # username has to end in "bot", so that much can be told from the link.
    if first.lower().endswith("bot"):
        return ""
    return first


def _whatsapp(url) -> str:
    host = url.netloc.lower().removeprefix("www.")
    query = parse_qs(url.query)
    if url.scheme == "whatsapp" or host in ("api.whatsapp.com", "web.whatsapp.com"):
        phone = normalize_phone((query.get("phone") or [""])[0])
        return phone
    if host != "wa.me":
        return ""
    parts = [p for p in url.path.split("/") if p]
    if not parts:
        return ""
    if parts[0] == "message" and len(parts) > 1 and re.fullmatch(r"[A-Za-z0-9]{6,32}", parts[1]):
        # A WhatsApp Business short link: no number, but it opens the chat.
        return f"wa.me/message/{parts[1]}"
    return normalize_phone(parts[0]) if re.fullmatch(r"\+?\d{10,15}", parts[0]) else ""


def _vk(url) -> str:
    host = url.netloc.lower().removeprefix("www.").removeprefix("m.")
    if host not in ("vk.com", "vk.ru", "vk.me"):
        return ""
    parts = [p for p in url.path.split("/") if p]
    if not parts:
        return ""
    name = parts[0]
    if host == "vk.com" and name.lower() == "write" and len(parts) == 1:
        return ""
    numeric = _VK_NUMERIC_RE.match(name)
    if numeric:
        return f"club{numeric.group(1)}"
    if re.fullmatch(r"write-\d{1,12}", name):
        return f"club{name[len('write-'):]}"
    if (name.lower() in _VK_RESERVED or not _VK_NAME_RE.match(name)
            or re.match(r"^(?:wall|photo|video|album|topic|doc|audio|market|app|id)-?\d", name)
            or "." in name.rstrip(".") and name.lower().endswith(".php")):
        return ""
    return name


def _parse(raw: str):
    raw = unescape(raw).strip().rstrip(".,;:!?")
    if raw.startswith("//"):
        raw = "https:" + raw
    elif not re.match(r"^[a-z][a-z0-9+.-]*:", raw, re.IGNORECASE):
        raw = "https://" + raw
    try:
        return urlparse(raw)
    except ValueError:
        return None


def find_contacts(html: str) -> dict[str, list[str]]:
    """{channel: [handle, ...]} for every messenger contact the page links to.

    Telegram handles are a username ("acme_studio") or a phone ("+79990001122");
    WhatsApp a phone in digits ("79990001122") or a business short link
    ("wa.me/message/ABC123"); VK a page name ("acme") or a community
    ("club123"). Order is the page's, duplicates dropped.
    """
    found: dict[str, list[str]] = {ch: [] for ch in CHANNELS}
    for match in _URL_RE.finditer(html or ""):
        url = _parse(match.group(0))
        if url is None:
            continue
        for channel, read in (("telegram", _telegram), ("whatsapp", _whatsapp), ("vk", _vk)):
            handle = read(url)
            if handle and handle not in found[channel]:
                found[channel].append(handle)
    return {ch: handles for ch, handles in found.items() if handles}


def open_link(channel: str, handle: str, text: str = "") -> str:
    """The link that opens a chat with ``handle`` in the messenger's own app,
    with ``text`` typed in where the messenger allows it (WhatsApp, and
    Telegram usernames). Elsewhere the dashboard copies the text first."""
    handle = (handle or "").strip()
    q = quote(text or "", safe="")
    if channel == "whatsapp":
        if handle.startswith("wa.me/message/"):
            return "https://" + handle
        digits = normalize_phone(handle)
        if not digits:
            return ""
        return f"https://wa.me/{digits}" + (f"?text={q}" if text else "")
    if channel == "telegram":
        if handle.startswith("+"):
            digits = normalize_phone(handle)
            return f"https://t.me/+{digits}" if digits else ""
        if not _TG_USERNAME_RE.match(handle):
            return ""
        return f"https://t.me/{handle}" + (f"?text={q}" if text else "")
    if channel == "vk":
        numeric = re.fullmatch(r"club(\d+)", handle)
        if numeric:
            return f"https://vk.com/write-{numeric.group(1)}"
        return f"https://vk.me/{handle}" if _VK_NAME_RE.match(handle) else ""
    return ""


def prefills_text(channel: str, handle: str) -> bool:
    """Whether open_link puts the message in the box by itself."""
    if channel == "whatsapp":
        return not (handle or "").startswith("wa.me/message/")
    if channel == "telegram":
        return not (handle or "").startswith("+")
    return False
