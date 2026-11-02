"""Availability checks and one approved first message from your own account.

Off unless ``channels.messengers.personal.enabled`` is true and the risk is
confirmed in the config file: this drives the *web clients of your personal*
Telegram and WhatsApp through a browser (patchright Chromium), not an
official API. The platforms forbid automation of personal accounts, so the
number can be limited or banned; that trade-off is the operator's, made once
in writing in the config, never silently by Harvey.

What it does on its own: opens the contact's chat and reads whether the
service says the account exists (``check_availability``). What it never
does without you: the first message goes only after you approve that exact
draft (the dashboard or ``harvey messengers send <id>``), once per business.

The login is yours too: ``harvey messengers login telegram|whatsapp`` shows
a QR code in the terminal, you scan it with your phone, and the session
persists in a private browser profile under data/. Without a session every
check returns "unknown" and nothing else happens.
"""

import asyncio
import logging
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from harvey.messengers.contacts import normalize_phone
from harvey.paths import PROJECT_ROOT

logger = logging.getLogger("harvey.messengers")

DATA_DIR = PROJECT_ROOT / "data"
PERSONAL_CHANNELS = ("telegram", "whatsapp")
# A check answers from what the page itself says; anything else is unknown.
REACHABLE, UNREACHABLE, UNKNOWN = "reachable", "unreachable", "unknown"

TELEGRAM_URL = "https://web.telegram.org/a/"
WHATSAPP_URL = "https://web.whatsapp.com/"

# Text the services show when they say "there is nobody here".
TG_UNREACHABLE_MARKS = ("if you know", "no username", "can't find", "couldn't find")
WA_UNREACHABLE_MARKS = ("not authorized", "phone number invalid", "invalid phone")

# How long to wait for the page to decide, seconds.
CHAT_TIMEOUT_MS = 15_000

# The web client's own controls: where the message goes and what sends it.
TELEGRAM_INPUT_SELECTOR = "div[aria-label='Message'], .input-field textarea"
TELEGRAM_SEND_SELECTOR = "button[aria-label='Send'], .send-button-icon"
WHATSAPP_INPUT_SELECTOR = "div[contenteditable='true'][data-tab]"
WHATSAPP_SEND_SELECTOR = "button[aria-label='Send'], button[data-icon='send']"


@dataclass
class AvailabilityResult:
    status: str          # reachable | unreachable | unknown
    detail: str = ""     # a short reason, never the contact itself


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


# ── Reading the page: pure functions, tested without a browser ──

def parse_telegram_page(url: str, text: str) -> str:
    """What Telegram Web says about the chat it was asked to open."""
    low = url.lower()
    if "im?startattach" in low or "/im" in low:
        return REACHABLE   # the client switched into a chat with that contact
    body = (text or "").lower()
    if any(m in body for m in TG_UNREACHABLE_MARKS):
        return UNREACHABLE
    return UNKNOWN


def parse_whatsapp_page(text: str) -> str:
    """What WhatsApp Web says about the wa.me link it was given."""
    body = " ".join((text or "").lower().split())
    if any(m in body for m in WA_UNREACHABLE_MARKS):
        return UNREACHABLE
    # An existing number lands in the chat with the Send-message button; the
    # "Continue to chat" handoff page is fine too — both mean the account is
    # there. Anything else (still loading, logged out) says nothing.
    if "send message" in body or "continue to chat" in body:
        return REACHABLE
    return UNKNOWN


def chat_url(channel: str, handle: str) -> str:
    """The web-client URL that opens (or proves the absence of) a chat.

    Never raises: an unusable handle gives "", which the caller reports as
    unknown. No message text ever enters these URLs — opening a chat must
    not pre-fill anything.
    """
    handle = (handle or "").strip()
    try:
        if channel == "telegram":
            if handle.startswith("+"):
                digits = normalize_phone(handle)
                return f"{TELEGRAM_URL}im/+{digits}" if digits else ""
            return f"{TELEGRAM_URL}im/{handle}" if handle else ""
        if channel == "whatsapp":
            if handle.startswith("wa.me/message/"):
                return "https://" + handle
            digits = normalize_phone(handle)
            return f"https://web.whatsapp.com/send?phone={digits}" if digits else ""
    except Exception:
        return ""
    return ""


# ── The browser behind a small interface, so tests need no browser ──

class BrowserSession:
    """One persistent browser profile per channel (login lives there)."""

    def __init__(self, channel: str, headless: bool = True):
        self.channel = channel
        self.headless = headless
        self.profile_dir = DATA_DIR / "personal-sessions" / channel
        self._pw = None
        self._ctx = None

    async def start(self):
        from patchright.async_api import async_playwright

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.profile_dir.chmod(0o700)   # the directory holds your session
        except OSError:
            pass
        self._pw = await async_playwright().start()
        # launch_persistent_context keeps cookies + localStorage between runs,
        # so one QR scan lasts until the platform logs the session out.
        self._ctx = await self._pw.chromium.launch_persistent_context(
            str(self.profile_dir), headless=self.headless, viewport={"width": 1280, "height": 800})
        return self._ctx

    async def stop(self):
        for closer in (getattr(self._ctx, "close", None), getattr(self._pw, "stop", None)):
            try:
                if closer:
                    r = closer()
                    if asyncio.iscoroutine(r):
                        await r
            except Exception as e:
                logger.debug(f"Personal {self.channel}: browser shutdown: {e}")
        self._ctx = None
        self._pw = None

    async def read_chat(self, url: str, timeout_ms: int = CHAT_TIMEOUT_MS) -> tuple[str, str]:
        """Open ``url``, wait for the page to settle, return (final_url, body)."""
        ctx = self._ctx or await self.start()
        page = await ctx.new_page()
        try:
            await page.goto(url, timeout=45_000, wait_until="domcontentloaded")
            try:
                await page.wait_for_load_state("networkidle", timeout=timeout_ms)
            except Exception:
                pass   # chat apps keep connections open; never fully idle
            await asyncio.sleep(timeout_ms / 1000.0)
            return page.url, await page.inner_text("body")
        finally:
            try:
                await page.close()
            except Exception:
                pass

    async def send_message(self, url: str, text: str,
                           timeout_ms: int = CHAT_TIMEOUT_MS) -> bool:
        """Open the chat, type ``text`` into its box and press send."""
        ctx = self._ctx or await self.start()
        input_sel, send_sel = ((TELEGRAM_INPUT_SELECTOR, TELEGRAM_SEND_SELECTOR)
                               if self.channel == "telegram"
                               else (WHATSAPP_INPUT_SELECTOR, WHATSAPP_SEND_SELECTOR))
        page = await ctx.new_page()
        try:
            await page.goto(url, timeout=45_000, wait_until="domcontentloaded")
            box = await page.wait_for_selector(input_sel, timeout=timeout_ms)
            if box is None:
                return False
            await box.click()
            await box.fill(text)
            button = await page.wait_for_selector(send_sel, timeout=timeout_ms)
            if button is None:
                return False
            await button.click()
            await asyncio.sleep(1.0)
            return True
        except Exception as e:
            logger.warning(f"Personal {self.channel}: send failed on the page: {type(e).__name__}")
            return False
        finally:
            try:
                await page.close()
            except Exception:
                pass

    async def qr_login(self, timeout_s: int = 240) -> bool:
        """Show the login QR in the terminal until it is scanned."""
        ctx = self._ctx or await self.start()
        page = await ctx.new_page()
        try:
            if self.channel == "telegram":
                await page.goto(TELEGRAM_URL, timeout=60_000, wait_until="domcontentloaded")
                sel = "canvas[aria-label*='QR' i], img[alt*='QR' i]"
                logged_marker = ".sidebar-wrapper, .tm-dialog-desktop-left"
            else:
                await page.goto(WHATSAPP_URL, timeout=90_000, wait_until="domcontentloaded")
                sel = "canvas, div[data-ref]"
                logged_marker = "#app div[aria-label='Chat list'], #side"
            deadline = _now() + timedelta(seconds=timeout_s)
            while _now() < deadline:
                if await page.query_selector(logged_marker):
                    return True
                el = await page.query_selector(sel)
                if el:
                    box = await el.bounding_box()
                    if box:
                        shot = await page.screenshot(clip=box)
                        path = DATA_DIR / f"login-{self.channel}.png"
                        path.write_bytes(shot)
                        print(f"\n  Open {path} on this machine and scan it with your phone."
                              f" Waiting up to {timeout_s}s...")
                await asyncio.sleep(3)
            return False
        finally:
            try:
                await page.close()
            except Exception:
                pass


class PersonalClient:
    """Checks availability and sends exactly one approved first message.

    ``session`` is anything with ``read_chat``/``qr_login`` (BrowserSession
    in production, a fake in tests); nothing here touches Playwright types.
    """

    def __init__(self, channel: str, session, cfg, pacing: bool = True):
        self.channel = channel
        self.session = session
        self.cfg = cfg
        self.pacing = pacing
        self.blocked_reason = ""   # set when the service pushes us away

    @property
    def usable(self) -> bool:
        return bool(self.cfg.enabled and self.cfg.confirm_risk)

    def _within_hours(self) -> bool:
        start, end = self.cfg.active_hours
        now_hm = _now().strftime("%H:%M")
        if start <= end:
            return start <= now_hm < end
        return now_hm >= start or now_hm < end   # a window crossing midnight

    async def _sleep_between_actions(self):
        if self.pacing:
            lo, hi = self.cfg.action_gap_seconds
            await asyncio.sleep(random.uniform(lo, hi))

    async def check_availability(self, handle: str) -> AvailabilityResult:
        if not self.usable:
            return AvailabilityResult(UNKNOWN, "personal sending is off")
        url = chat_url(self.channel, handle)
        if not url:
            return AvailabilityResult(UNKNOWN, "Harvey can't build a chat link for this contact")
        if self.blocked_reason:
            return AvailabilityResult(UNKNOWN, self.blocked_reason)
        if not self._within_hours():
            return AvailabilityResult(UNKNOWN, "outside your active hours")
        try:
            page_url, text = await self.session.read_chat(url)
        except Exception as e:
            logger.warning(f"Personal {self.channel}: check failed: {type(e).__name__}")
            return AvailabilityResult(UNKNOWN, "the web client didn't answer")
        await self._sleep_between_actions()
        if self._looks_blocked(text):
            self.blocked_reason = "logged out or blocked by the service"
            return AvailabilityResult(UNKNOWN, self.blocked_reason)
        if self.channel == "telegram":
            status = parse_telegram_page(page_url, text)
        else:
            status = parse_whatsapp_page(text)
        return AvailabilityResult(status, "")

    def _looks_blocked(self, text: str) -> bool:
        body = " ".join((text or "").lower().split())
        marks = ("log in by phone", "scan the qr", "get telegram for",
                 "link your device", "loading more messages") if self.channel == "telegram" \
            else ("scan the qr code", "whatsapp web allows you", "keep whatsapp")
        return any(m in body for m in marks)

    async def send_first_message(self, chat_ref: str, body: str) -> dict:
        """Type ``body`` into an already-open chat and press send.

        ``chat_ref`` is the same handle the draft carries; the chat is
        opened again here because the check's page is gone. Returns
        {"ok", "error"} — never raises.
        """
        if not self.usable:
            return {"ok": False, "error": "personal sending is off"}
        if self.blocked_reason:
            return {"ok": False, "error": self.blocked_reason}
        if not self._within_hours():
            return {"ok": False, "error": "outside your active hours"}
        url = chat_url(self.channel, chat_ref)
        if not url:
            return {"ok": False, "error": "no chat link for this contact"}
        try:
            sent = await self.session.send_message(url, body)
        except Exception as e:
            logger.warning(f"Personal {self.channel}: send failed: {type(e).__name__}")
            sent = False
        await self._sleep_between_actions()
        if not sent:
            return {"ok": False, "error": "the message didn't go: the chat or the input wasn't there"}
        return {"ok": True, "error": ""}


class MessengerLoginError(Exception):
    pass


async def login(channel: str, headless: bool = False) -> bool:
    """Interactive QR login for your own account; True once it is linked."""
    if channel not in PERSONAL_CHANNELS:
        raise MessengerLoginError(f"No personal login for {channel!r}: use telegram or whatsapp")
    session = BrowserSession(channel, headless=headless)
    try:
        return await session.qr_login()
    finally:
        await session.stop()
