"""Russian-language rules shared by email and messengers.

Opt-outs, complaints and spam wording are decided by fixed rules, never by
the model, so the same lists must stand behind a reply to a cold email and a
reply in a chat. They live here, with no dependency on the rest of Harvey, so
``harvey.gate``, the Handler and ``harvey.messengers.gate`` can all import
them.

Each check is a no-op on English text: the patterns are Russian words, so an
English reply trips none of them.
"""

import re

# ── what their message says ──────────────────────────────────────────────

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
# For email, a bare English "no" is left to the English rules.
BARE_OPT_OUTS_EMAIL_RU = BARE_OPT_OUTS_RU - {"no"}
# "Нет, спасибо" on a line of its own reply, as the opt-out line asks for it.
# Not "да или нет, спасибо" (an answer, then thanks).
NO_THANKS_RU_RE = re.compile(r"(?<!или )(?<!\bда )\bнет,?[ \t]+спасибо\b")

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


def _flat(text: str) -> str:
    return " ".join((text or "").lower().replace("ё", "е").split())


def opt_out_reason(text: str) -> str:
    """The words that make this message an opt-out, or ""."""
    flat = _flat(text)
    for p in OPT_OUT_PATTERNS_RU:
        if p in flat:
            return p
    bare = _WORD_RE.sub("", flat).strip(" ,")
    if bare in BARE_OPT_OUTS_RU:
        return bare
    return ""


def escalation_reason(text: str) -> str:
    """The words that mean a person must answer this, or ""."""
    flat = _flat(text)
    for p in ESCALATION_PATTERNS_RU:
        if re.search(r"(?<![а-яa-z0-9])" + re.escape(p), flat):
            return p
    for w in ESCALATION_WORDS_RU:
        if re.search(r"(?<![а-яa-z0-9])" + re.escape(w) + r"(?![а-яa-z0-9])", flat):
            return w
    return ""


# ── wording that is never sent ───────────────────────────────────────────

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
# Extra for email: what Russian spam filters and readers punish.
BANNED_EMAIL_PHRASES_RU = (
    "специальное предложение", "эксклюзивное предложение", "супер выгодно", "суперпредложение",
    "не пропустите", "жмите", "нажмите здесь", "перейдите по ссылке", "акция только",
    "только до конца", "лучшее предложение на рынке", "лучшая цена на рынке",
    "заработайте", "пассивный доход", "гарантированный доход", "без риска",
    "100% результат", "100% эффективн", "бесплатная консультация и скидка",
    "по специальной цене", "ограниченное предложение",
)


def banned_phrase_ru(text: str) -> str:
    """The first banned Russian phrase in ``text``, or ""."""
    lowered = _flat(text)
    for phrase in BANNED_PHRASES_RU + BANNED_EMAIL_PHRASES_RU:
        if phrase.replace("ё", "е") in lowered:
            return phrase
    return ""


def skills_for(brain, agent: str, language: str = "en") -> str:
    """The agent's skills, in Russian when asked. English is the call it
    always was, so a brain that knows nothing of languages is unaffected."""
    if language == "ru":
        return brain.load_skills_for_agent(agent, language="ru")
    return brain.load_skills_for_agent(agent)


# ── company names in a greeting ──────────────────────────────────────────

_LEGAL_FORM = (r"(?:ооо|зао|оао|пао|ао|нко|оу|ип|индивидуальный\s+предприниматель|"
               r"общество\s+с\s+ограниченной\s+ответственностью)")
_CYR = re.compile(r"[А-Яа-яЁё]")


def clean_company_name(name: str) -> str:
    """"ООО «Ромашка»" -> "Ромашка": the legal form and the quotation marks
    are for a contract, not a sentence. A name with no Cyrillic is untouched."""
    original = (name or "").strip()
    if not _CYR.search(original):
        return name or ""
    cleaned = re.sub(rf"(?i)^\s*{_LEGAL_FORM}\b\.?\s*", "", original)
    cleaned = re.sub(rf"(?i)[,\s]+{_LEGAL_FORM}\.?\s*$", "", cleaned)
    cleaned = cleaned.strip(" «»\"'“”„‹›")
    return cleaned or original
