"""Checks every messenger message passes, with no model involved.

The same idea as harvey/gate.py for email: prompts ask for the right thing,
code makes sure of it. ``problems`` blocks a message outright; ``holds``
sends one nobody approved back to you (a link that isn't yours, a passage of
Harvey's own instructions), exactly as autopilot email does.
"""

import re

from harvey.gate import BANNED_PHRASES, autopilot_review, do_not_contact_match
from harvey.ru_text import (  # noqa: F401  (re-exported: the names the messengers always used)
    BANNED_PHRASES_RU, BARE_OPT_OUTS_RU, ESCALATION_PATTERNS_RU, ESCALATION_WORDS_RU,
    OPT_OUT_PATTERNS_RU, escalation_reason, opt_out_reason,
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



