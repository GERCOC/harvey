"""Checks every messenger message passes, with no model involved.

The same idea as harvey/gate.py for email: prompts ask for the right thing,
code makes sure of it. ``problems`` blocks a message outright; ``holds``
sends one nobody approved back to you (a link that isn't yours, a passage of
Harvey's own instructions), exactly as autopilot email does.
"""

import re

from harvey.gate import BANNED_PHRASES, autopilot_review, do_not_contact_match

# Spam words and AI tells in Russian, on top of the English ones email bans.
BANNED_PHRASES_RU = (
    "надеюсь, это сообщение", "надеюсь, у вас всё хорошо", "надеюсь, у вас все хорошо",
    "в современном мире", "в современном быстро", "в наше время", "уникальное предложение",
    "только сегодня", "только сейчас", "не упустите", "спешите", "успейте",
    "100% гарантия", "гарантированный результат", "бесплатно и без обязательств",
    "как ии", "как искусственный интеллект", "как языковая модель", "я — ии", "я ии",
    "революционное решение", "выведет ваш бизнес на новый уровень",
    "хочу предложить вам сотрудничество", "предлагаем сотрудничество",
    "ваш бизнес на новый уровень", "уважаемый клиент", "добрый день, уважаемый",
)
MAX_LINKS = 1
_LINK_RE = re.compile(r"(?:https?://|www\.)\S+|\b[\w-]+\.(?:ru|рф|com|net|org|io|su|by|kz)\b(?:/\S*)?",
                      re.IGNORECASE)
_MERGE_RE = re.compile(r"\{\{?\s*\w+\s*\}?\}|\[\s*(?:имя|name|компания|company)\s*\]", re.IGNORECASE)
_HTML_RE = re.compile(r"<\s*(?:a|b|i|p|div|span|br|strong|em)\b[^>]*>", re.IGNORECASE)


def problems(body: str, config, *, kind: str = "cold", company_domain: str = "") -> list[str]:
    """Why this message must not go at all ([] when it may)."""
    cfg = config.channels.messengers
    text = (body or "").strip()
    out: list[str] = []
    if not text:
        return ["the message is empty"]
    limit = cfg.max_chars if kind == "cold" else max(cfg.max_chars, 1000)
    if len(text) > limit:
        out.append(f"longer than {limit} characters ({len(text)})")
    if _MERGE_RE.search(text):
        out.append("a placeholder wasn't filled in")
    if _HTML_RE.search(text):
        out.append("contains HTML")
    lowered = text.lower().replace("ё", "е")
    for phrase in BANNED_PHRASES + BANNED_PHRASES_RU:
        if phrase.replace("ё", "е") in lowered:
            out.append(f"contains a banned phrase: «{phrase}»")
            break
    if len(_LINK_RE.findall(text)) > MAX_LINKS:
        out.append(f"more than {MAX_LINKS} link")
    entries = config.compliance.do_not_contact
    if company_domain and entries:
        hit = do_not_contact_match(f"x@{company_domain}", entries)
        if hit:
            out.append(f"the business is on your do-not-contact list ({hit})")
    return out


def holds(body: str, config, *, company_domain: str = "") -> list[str]:
    """Why a message nobody approved waits for you ([] when it can go)."""
    recipient = f"x@{company_domain}" if company_domain else ""
    return autopilot_review("", body, config, recipient=recipient)


# ── What their message says ──

# Phrases that always stop Harvey, whatever the model makes of the message.
OPT_OUT_PATTERNS_RU = (
    "не пишите", "больше не пиши", "не надо писать", "не нужно писать", "хватит писать",
    "перестаньте писать", "не беспокойте", "отпишите", "отписаться", "отписка",
    "удалите мой", "удалите меня", "удалите номер", "уберите из рассыл", "уберите меня",
    "исключите из", "не присылайте", "не звоните и не пишите", "это спам", "спам",
    "unsubscribe", "stop messaging", "do not contact", "don't contact", "remove me",
)
# A message that says only this (punctuation aside) is an opt-out too.
BARE_OPT_OUTS_RU = {"нет", "нет спасибо", "нет, спасибо", "стоп", "stop", "неинтересно",
                    "не интересно", "не актуально", "неактуально", "не надо", "no", "no thanks"}
# A human must take over: never answered by Harvey.
# Word stems, matched at the start of a word ("жалоб" is "жалоба", "жалобу").
ESCALATION_PATTERNS_RU = (
    "юрист", "адвокат", "в суд", "претензи", "жалоб", "пожалуюсь",
    "роскомнадзор", "прокуратур", "полиц", "персональных данных", "152-фз",
    "38-фз", "закон о рекламе", "мошенн", "угрож", "угроз", "lawyer", "lawsuit", "legal action",
)
# Short words that must stand alone: "иск" is not "искать", "суд" not "судя".
ESCALATION_WORDS_RU = ("иск", "суд", "фас", "ркн")
_WORD_RE = re.compile(r"[^\w\s,-]", re.UNICODE)


def opt_out_reason(text: str) -> str:
    """The words that make this message an opt-out, or ""."""
    flat = " ".join((text or "").lower().replace("ё", "е").split())
    for p in OPT_OUT_PATTERNS_RU:
        if p in flat:
            return p
    bare = _WORD_RE.sub("", flat).strip(" ,")
    if bare in BARE_OPT_OUTS_RU:
        return bare
    return ""


def escalation_reason(text: str) -> str:
    """The words that mean a person must answer this, or ""."""
    flat = " ".join((text or "").lower().replace("ё", "е").split())
    for p in ESCALATION_PATTERNS_RU:
        if re.search(r"(?<![а-яa-z0-9])" + re.escape(p), flat):
            return p
    for w in ESCALATION_WORDS_RU:
        if re.search(r"(?<![а-яa-z0-9])" + re.escape(w) + r"(?![а-яa-z0-9])", flat):
            return w
    return ""
