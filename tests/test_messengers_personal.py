"""Personal-account checks and approved sends (harvey/messengers/personal.py).

The browser is never started here: BrowserSession hides everything behind
read_chat/send_message, so fakes cover the whole logic. Every contact below
is invented."""

import os
import sqlite3
import tempfile
from contextlib import closing
from datetime import datetime
from types import SimpleNamespace

import pytest
import pytest_asyncio
import yaml

from harvey.config import EnvConfig, HarveyConfig
from harvey.messengers.agent import MessengerAgent
from harvey.messengers.contacts import normalize_phone
from harvey.messengers.personal import (
    REACHABLE, UNKNOWN, UNREACHABLE, AvailabilityResult, BrowserSession,
    MessengerLoginError, PersonalClient, chat_url, is_bot_handle, login,
    parse_inbound_telegram, parse_inbound_whatsapp, parse_telegram_page,
    parse_whatsapp_page)
from harvey.messengers.store import MessengerStore
from harvey.models.company import Company
from harvey.paths import PROJECT_ROOT
from harvey.signals import seed_signal_catalog
from harvey.state import StateManager


def make_config(personal=None, do_not_contact=()) -> HarveyConfig:
    data = yaml.safe_load((PROJECT_ROOT / "harvey.yaml").read_text(encoding="utf-8"))
    # The shipped harvey.yaml pins active_hours to business hours; every test
    # below runs at an arbitrary clock time, so the window opens all day.
    on = {"enabled": True, "confirm_risk": True, "active_hours": ["00:00", "23:59"],
          "action_gap_seconds": [0.0, 0.0]}
    # no argument means "the personal path is on"; {"enabled": False} turns it off
    personal = {**on, **(personal or {})}
    data["channels"]["messengers"] = {"enabled": True, "personal": personal}
    data["compliance"]["do_not_contact"] = list(do_not_contact)
    data["persona"]["email"] = "anna@bookwell.example"
    return HarveyConfig(**data)


PCFG_ON = SimpleNamespace(enabled=True, confirm_risk=True, active_hours=("00:00", "23:59"),
                          action_gap_seconds=(0.0, 0.0))


class FakeSession:
    def __init__(self, url="", text="", send_ok=True):
        self.reads, self.sends = [], []
        self.url, self.text, self.send_ok = url, text, send_ok

    async def read_chat(self, url, timeout_ms=15_000):
        self.reads.append(url)
        return self.url, self.text

    async def send_message(self, url, text, timeout_ms=15_000):
        self.sends.append((url, text))
        return self.send_ok


@pytest_asyncio.fixture()
async def state():
    with tempfile.TemporaryDirectory() as tmp:
        sm = StateManager(os.path.join(tmp, "t.db"))
        await sm.init_db()
        await seed_signal_catalog(sm)
        yield sm


# ── Reading the page: pure parsers ──

def test_telegram_page_with_an_open_chat_is_reachable():
    assert parse_telegram_page("https://web.telegram.org/a/im?chats=peer-777", "") == REACHABLE


def test_telegram_says_nobody_is_there():
    text = "If you know Ivan's username, you can message him."
    assert parse_telegram_page("https://web.telegram.org/a/", text) == UNREACHABLE


def test_telegram_still_loading_says_nothing():
    assert parse_telegram_page("https://web.telegram.org/a/", "Loading...") == UNKNOWN


def test_whatsapp_reaches_only_on_the_chat_or_handoff_page():
    assert parse_whatsapp_page("Send message") == REACHABLE
    assert parse_whatsapp_page("Continue to chat   ") == REACHABLE
    assert parse_whatsapp_page("Phone number invalid") == UNREACHABLE
    assert parse_whatsapp_page("Scan the QR code to link your device") == UNKNOWN


def test_a_bot_handle_is_never_checked_or_sent_to():
    assert is_bot_handle("@acme_helpbot")
    assert is_bot_handle("support")
    assert not is_bot_handle("acme_studio")


def test_chat_urls_carry_the_contact_and_nothing_else():
    tg = chat_url("telegram", "@ivan")
    assert tg.startswith("https://web.telegram.org/a/im/") and "ivan" in tg
    wa = chat_url("whatsapp", "+7 900 123-45-67")
    assert wa == "https://web.whatsapp.com/send?phone=" + normalize_phone("+7 900 123-45-67")
    assert chat_url("whatsapp", "wa.me/message/ABC") == "https://wa.me/message/ABC"
    assert chat_url("whatsapp", "not a phone at all") == ""
    assert chat_url("vk", "anyone") == ""


# ── The client around a fake session ──

@pytest.mark.asyncio
async def test_off_or_unconfirmed_means_no_browser_at_all():
    s = FakeSession()
    for cfg in (SimpleNamespace(enabled=False, confirm_risk=True, active_hours=("00:00", "23:59"),
                                action_gap_seconds=(0, 0)),
                SimpleNamespace(enabled=True, confirm_risk=False, active_hours=("00:00", "23:59"),
                                action_gap_seconds=(0, 0))):
        c = PersonalClient("telegram", s, cfg, pacing=False)
        assert (await c.check_availability("@ivan")).status == UNKNOWN
        assert not (await c.send_first_message("@ivan", "текст"))["ok"]
    assert s.reads == [] and s.sends == []


@pytest.mark.asyncio
async def test_check_reads_the_page_and_reports_what_it_says():
    s = FakeSession(url="https://web.telegram.org/a/im?chats=peer-1", text="")
    c = PersonalClient("telegram", s, PCFG_ON, pacing=False)
    r = await c.check_availability("@ivan")
    assert r.status == REACHABLE
    assert s.reads == [chat_url("telegram", "@ivan")]


@pytest.mark.asyncio
async def test_a_logged_out_service_freezes_the_client():
    s = FakeSession(text="Please log in by phone to use Telegram Web")
    c = PersonalClient("telegram", s, PCFG_ON, pacing=False)
    first = await c.check_availability("@ivan")
    second = await c.check_availability("@petr")
    assert first.status == second.status == UNKNOWN
    assert "blocked" in second.detail or "logged out" in second.detail
    assert len(s.reads) == 1          # no point poking a logged-out client


@pytest.mark.asyncio
async def test_checks_only_inside_active_hours():
    s = FakeSession(url="https://web.telegram.org/a/im?x=1", text="")
    cfg = SimpleNamespace(enabled=True, confirm_risk=True, active_hours=("00:00", "00:01"),
                          action_gap_seconds=(0, 0))
    c = PersonalClient("telegram", s, cfg, pacing=False)
    r = await c.check_availability("@ivan")
    assert r.status == UNKNOWN and "active hours" in r.detail
    assert s.reads == []


@pytest.mark.asyncio
async def test_send_types_exactly_the_approved_body_once():
    body = "Здравствуйте! Вопрос?\nЕсли неактуально — напишите «нет»."
    s = FakeSession(send_ok=True)
    c = PersonalClient("telegram", s, PCFG_ON, pacing=False)
    r = await c.send_first_message("@ivan", body)
    assert r["ok"]
    assert s.sends == [(chat_url("telegram", "@ivan"), body)]


@pytest.mark.asyncio
async def test_a_broken_page_is_a_failed_send_not_a_crash():
    s = FakeSession(send_ok=False)
    c = PersonalClient("whatsapp", s, PCFG_ON, pacing=False)
    r = await c.send_first_message("+79001234567", "текст")
    assert not r["ok"] and r["error"]


# ── The client's remaining guard rails ──

class RaisingSession(FakeSession):
    async def read_chat(self, url, timeout_ms=15_000):
        raise RuntimeError("the browser died mid-page")

    async def send_message(self, url, text, timeout_ms=15_000):
        raise RuntimeError("the browser died mid-type")


@pytest.mark.asyncio
async def test_a_dead_browser_gives_unknown_and_false_but_never_raises():
    c = PersonalClient("telegram", RaisingSession(), PCFG_ON, pacing=False)
    assert (await c.check_availability("@ivan")).status == UNKNOWN
    assert not (await c.send_first_message("@ivan", "текст"))["ok"]
    assert await c.read_reply("@ivan", "текст") == []


@pytest.mark.asyncio
async def test_a_bot_or_an_unusable_handle_stops_before_the_browser():
    s = FakeSession()
    c = PersonalClient("telegram", s, PCFG_ON, pacing=False)
    assert (await c.check_availability("@acme_supportbot")).status == UNKNOWN
    assert (await c.check_availability("")).status == UNKNOWN   # no chat link
    assert not (await c.send_first_message("", "текст"))["ok"]
    assert await c.read_reply("", "текст") == []
    assert s.reads == [] and s.sends == []                      # never opened


@pytest.mark.asyncio
async def test_nobody_writes_into_a_blocked_client():
    s = FakeSession(text="Please log in by phone to use Telegram Web")
    c = PersonalClient("telegram", s, PCFG_ON, pacing=False)
    await c.check_availability("@ivan")                        # notices the logout
    assert not (await c.send_first_message("@ivan", "текст"))["ok"]
    assert await c.read_reply("@ivan", "текст") == []
    assert len(s.reads) == 1 and s.sends == []


@pytest.mark.asyncio
async def test_sends_and_reads_only_inside_active_hours():
    night = SimpleNamespace(enabled=True, confirm_risk=True, active_hours=("00:00", "00:00"),
                            action_gap_seconds=(0, 0))
    s = FakeSession(send_ok=True)
    c = PersonalClient("telegram", s, night, pacing=False)
    assert not (await c.send_first_message("@ivan", "текст"))["ok"]
    assert await c.read_reply("@ivan", "Здравствуйте!") == []
    assert s.sends == [] and s.reads == []


def test_a_midnight_crossing_window_stays_open_at_night():
    c = PersonalClient("telegram", FakeSession(),
                       SimpleNamespace(active_hours=("22:00", "06:00")), pacing=False)
    # A window crossing midnight is open late evening and early morning only.
    with frozen_clock(hour=23):
        assert c._within_hours()
    with frozen_clock(hour=12):
        assert not c._within_hours()          # midday sits outside 22:00–06:00


@contextmanager
def frozen_clock(hour, tz_name="UTC"):
    """Freeze personal.py's clock at `hour`:00 in `tz_name` (its own import)."""
    import harvey.messengers.personal as pmod

    class Fixed(datetime):
        @classmethod
        def now(cls, tz=None):
            base = datetime.now(_timezone.utc).replace(
                hour=hour, minute=0, second=0, microsecond=0, tzinfo=_timezone.utc)
            return base.astimezone(tz) if tz else base.replace(tzinfo=None)

    old = pmod.datetime
    pmod.datetime = Fixed
    try:
        c_holder = None  # noqa: F841 - keep the helper free of client state
        yield
    finally:
        pmod.datetime = old


def test_an_operator_timezone_drives_the_active_hours():
    # Moscow is UTC+3 fixed: at 12:00Z it is 15:00 there, so a 14:00-16:00
    # window must be open by the operator's clock and closed by UTC's.
    c = PersonalClient("telegram", FakeSession(), PCFG_ON, pacing=False, tz_name="Europe/Moscow")
    with frozen_clock(hour=12):
        c.cfg = SimpleNamespace(active_hours=("14:00", "16:00"))
        assert c._within_hours()              # 15:00 in Moscow
        c.tz_name = ""
        assert not c._within_hours()          # 12:00 UTC is outside the window


@pytest.mark.asyncio
async def test_pacing_sleeps_between_actions_when_switched_on():
    cfg = SimpleNamespace(enabled=True, confirm_risk=True, active_hours=("00:00", "23:59"),
                          action_gap_seconds=(0.01, 0.02))
    s = FakeSession(url="https://web.telegram.org/a/im?chats=peer-1")
    c = PersonalClient("telegram", s, cfg, pacing=True)
    import time as _t
    t0 = _t.monotonic()
    await c.check_availability("@ivan")
    assert _t.monotonic() - t0 >= 0.01      # one human-ish gap between page visits


# ── Store bookkeeping ──

@pytest.mark.asyncio
async def test_availability_limits_stale_rechecks_and_once_only():
    with tempfile.TemporaryDirectory() as tmp:
        sm = StateManager(os.path.join(tmp, "t.db"))
        await sm.init_db()
        store = MessengerStore(sm)
        cid = await sm.add_company(Company(name="Acme Studio", domain="acme.example",
                                           website="https://acme.example"))
        oid = await store.add_cold(cid, channel="telegram", handle="@ivan",
                                   body="Здравствуйте! Вопрос?")
        item = await store.get_cold(oid)
        assert item["availability"] in ("", UNKNOWN)      # v16 column defaults empty

        assert [i["id"] for i in await store.unchecked_cold()] == [oid]
        await store.set_availability(oid, REACHABLE)
        assert await store.unchecked_cold() == []         # checked once, stays checked
        assert await store.checked_since(datetime(2000, 1, 1)) == 1

        # unreachable + a stale check goes back to unknown; a fresh one stays
        await store.set_availability(oid, UNREACHABLE)
        assert await store.recheck_stale_availability(older_than_days=7) == 0
        with closing(sqlite3.connect(sm.db_path)) as db:
            db.execute("UPDATE messenger_outbox SET availability_checked_at = '2026-01-01 00:00:00'"
                       " WHERE id = ?", (oid,))
            db.commit()
        assert await store.recheck_stale_availability(older_than_days=7) == 1
        assert (await store.get_cold(oid))["availability"] == UNKNOWN

        # personal sends count only messages that actually went out
        assert await store.personal_sent_since(datetime(2000, 1, 1)) == 0


# ── The agent's two personal steps ──

def _fake_agent(state, clients, config=None):
    a = MessengerAgent(SimpleNamespace(), state, config or make_config(), EnvConfig())
    a.send_pacing = False
    a.personal_clients = clients          # channel -> PersonalClient with FakeSession
    return a


async def _seed_draft(state, availability=""):
    cid = await state.add_company(Company(name="Acme Studio", domain="acme.example",
                                          website="https://acme.example"))
    store = MessengerStore(state)
    oid = await store.add_cold(cid, channel="telegram", handle="@ivan",
                               body="Здравствуйте! Вопрос?",
                               followup="Анна снова. Вопрос?")
    if availability:
        await store.set_availability(oid, availability)
    return cid, oid


@pytest.mark.asyncio
async def test_a_reachable_contact_gets_its_draft_checked_once(state):
    await _seed_draft(state)
    s = FakeSession(url="https://web.telegram.org/a/im?chats=peer-1")
    n = await _fake_agent(state, {"telegram": PersonalClient("telegram", s, PCFG_ON,
                                                              pacing=False)}
                          ).check_personal_availability()
    assert n == 1
    assert (await MessengerStore(state).unchecked_cold()) == []
    again = await _fake_agent(state, {}).check_personal_availability()
    assert again == 0                      # nothing left to look at


@pytest.mark.asyncio
async def test_no_login_means_the_queue_is_left_alone(state):
    await _seed_draft(state)
    # _personal_client finds no logged_in account and the injected map is empty
    assert await _fake_agent(state, {}).check_personal_availability() == 0
    assert len(await MessengerStore(state).unchecked_cold()) == 1


@pytest.mark.asyncio
async def test_only_approved_and_reachable_drafts_are_sent_exactly_once(state):
    cid, oid = await _seed_draft(state, availability=REACHABLE)
    store = MessengerStore(state)
    a = _fake_agent(state, {})
    # approved but not reachable yet: send_approved_personal must not touch it
    await store.approve_cold(oid)
    assert await a.send_approved_personal() == 0
    await store.set_availability(oid, REACHABLE)
    s = FakeSession(send_ok=True)
    a.personal_clients = {"telegram": PersonalClient("telegram", s, PCFG_ON, pacing=False)}
    assert await a.send_approved_personal() == 1
    assert s.sends[0][1].startswith("Здравствуйте!")
    item = await store.get_cold(oid)
    assert item["status"] == "sent"
    # the follow-up became due later, and nothing sends twice
    assert rows_of(state, "SELECT status FROM messenger_outbox WHERE step = 2") == ["scheduled"]
    assert await a.send_approved_personal() == 0
    assert len(s.sends) == 1


@pytest.mark.asyncio
async def test_the_kill_switch_stops_checks_and_sends(state):
    cid, oid = await _seed_draft(state, availability=REACHABLE)
    await MessengerStore(state).approve_cold(oid)
    s = FakeSession(url="https://web.telegram.org/a/im?chats=peer-1", send_ok=True)
    a = _fake_agent(state, {"telegram": PersonalClient("telegram", s, PCFG_ON, pacing=False)})
    await a.state.set_setting("sending_paused", "paused by hand")
    assert await a.check_personal_availability() == 0
    assert await a.send_approved_personal() == 0
    assert s.reads == [] and s.sends == []


@pytest.mark.asyncio
async def test_a_gate_problem_blocks_the_send_and_records_why(state):
    # do_not_contact is the one rule that needs no model: the gate rechecks
    # it right before the browser would type anything.
    cid, oid = await _seed_draft(state, availability=REACHABLE)
    store = MessengerStore(state)
    await store.approve_cold(oid)
    s = FakeSession(send_ok=True)
    a = _fake_agent(state, {"telegram": PersonalClient("telegram", s, PCFG_ON, pacing=False)},
                    config=make_config(do_not_contact=["acme.example"]))
    assert await a.send_approved_personal() == 0
    assert s.sends == []
    assert "do-not-contact" in (await store.get_cold(oid))["error"]


def rows_of(state, sql):
    with closing(sqlite3.connect(state.db_path)) as db:
        return [r[0] for r in db.execute(sql)]


# ── Reading replies out of a personal chat (agent + parser) ──

WA_PAGE = """WhatsApp
Ольга Иванова
Да, давайте созвонимся завтра
Кофейня Пример
Иван Петров
в сети
Здравствуйте! Вопрос?
12:05
А сколько стоит?
12:07
Введите сообщение"""

TG_PAGE = """Telegram
Ольга Иванова
Привет!
Иван Петров
online
Здравствуйте!
Вопрос?
А сколько стоит?
Сообщение"""


def test_parse_inbound_keeps_only_this_chat_after_our_own_message():
    # The page carries the whole sidebar; only bubbles below our own last
    # message count — other people's names and previews must never leak in.
    assert parse_inbound_whatsapp(WA_PAGE, "Здравствуйте! Вопрос?") == ["А сколько стоит?"]
    # Our draft is rendered with different line breaks than we stored it.
    assert parse_inbound_telegram(TG_PAGE, "Здравствуйте! Вопрос?") == ["А сколько стоит?"]


def test_parse_inbound_without_our_message_reads_nothing_invented():
    assert parse_inbound_whatsapp("", "Что-то") == []
    # No own message on the page: the chat body isn't delimited, so nothing
    # is claimed as a reply rather than guessing across the sidebar.
    assert parse_inbound_whatsapp("Ольга Иванова\nПривет!", "Здравствуйте! Вопрос?") == []


@pytest.mark.asyncio
async def test_personal_replies_store_once_and_stop_after_a_no(state):
    cid, oid = await _seed_draft(state, availability=REACHABLE)
    store = MessengerStore(state)
    await store.approve_cold(oid)
    s = FakeSession(send_ok=True)
    s.text = WA_PAGE
    a = _fake_agent(state, {"telegram": PersonalClient("telegram", s, PCFG_ON, pacing=False)})
    assert await a.send_approved_personal() == 1
    thread_id = (await store.get_cold(oid))["thread_id"]
    s.text = WA_PAGE.replace("А сколько стоит?", "Нет, спасибо")
    assert await a.read_personal_replies() == 1
    assert await a.read_personal_replies() == 0   # the same text is not new twice
    msgs = await store._rows("""SELECT body FROM messenger_messages
                                WHERE thread_id = ? AND direction = 'in'""", (thread_id,))
    assert [m["body"] for m in msgs] == ["Нет, спасибо"]


@pytest.mark.asyncio
async def test_personal_reply_read_respects_the_kill_switch(state):
    cid, oid = await _seed_draft(state, availability=REACHABLE)
    store = MessengerStore(state)
    await store.approve_cold(oid)
    s = FakeSession(send_ok=True, url="https://web.telegram.org/a/im?chats=peer-1", text=WA_PAGE)
    a = _fake_agent(state, {"telegram": PersonalClient("telegram", s, PCFG_ON, pacing=False)})
    assert await a.send_approved_personal() == 1
    reads_after_send = len(s.reads)
    await state.set_setting("sending_paused", "paused by hand")
    assert await a.read_personal_replies() == 0
    assert len(s.reads) == reads_after_send   # no new page visits while paused


@pytest.mark.asyncio
async def test_an_opt_out_after_approval_beats_the_queue(state):
    # You approved the send; then they said no. The next pass must not type
    # anything into the browser: the opt-out cancels the approved draft.
    cid, oid = await _seed_draft(state, availability=REACHABLE)
    store = MessengerStore(state)
    await store.approve_cold(oid)
    thread = await store.thread_for("telegram", "manual", "telegram:@ivan",
                                    handle="@ivan", company_id=cid)
    await store.add_inbound(thread["id"], "нет, не пишите")
    s = FakeSession(send_ok=True)
    a = _fake_agent(state, {"telegram": PersonalClient("telegram", s, PCFG_ON, pacing=False)},
                    config=make_config())
    a.brain = SimpleNamespace(load_prompt=lambda *a, **k: "", load_skill=lambda n: "",
                              think_json=None, paused_reason="")
    await a.answer_threads()   # fixed rule sees the "no" before any model call
    assert (await store.get_cold(oid))["status"] == "cancelled"
    assert await a.send_approved_personal() == 0
    assert s.sends == []


@pytest.mark.asyncio
async def test_give_up_after_the_last_attempt_cancels_the_followup(state):
    cid, oid = await _seed_draft(state, availability=REACHABLE)
    store = MessengerStore(state)
    await store.approve_cold(oid)
    await store.personal_send_failed(oid, "the chat wasn't there", max_attempts=1)
    assert (await store.get_cold(oid))["status"] == "cancelled"
    assert rows_of(state, "SELECT status FROM messenger_outbox WHERE step = 2") == ["cancelled"]


# ── The browser behind the interface: driven with a fake page, no real browser ──

class FakeElement:
    def __init__(self, box=None):
        self.box = box
        self.clicked, self.filled = False, None

    async def bounding_box(self):
        return self.box

    async def click(self):
        self.clicked = True

    async def fill(self, text):
        self.filled = text


class FakePage:
    """One scripted web client: goto/selector/screenshot/inner_text."""

    def __init__(self, url="https://web.telegram.org/a/", body="",
                 selectors=None, closed_flag=False):
        self.url = url
        self.body = body
        self.selectors = selectors or {}
        self.goto_url = ""
        self.closed = closed_flag
        self.shots = []
        self.clip_none = False

    async def goto(self, url, **kw):
        self.goto_url = url

    async def wait_for_load_state(self, state, timeout=0):
        pass

    async def inner_text(self, sel):
        return self.body

    async def query_selector(self, sel):
        return self.selectors.get(sel)

    async def wait_for_selector(self, sel, timeout=0):
        return self.selectors.get(sel)

    async def screenshot(self, clip=None):
        if clip is None:
            self.clip_none = True
        self.shots.append(clip)
        return b"png-bytes"

    async def close(self):
        self.closed = True


class FakeContext:
    def __init__(self, pages):
        self._pages = list(pages)
        self.closed = False

    async def new_page(self):
        # The web client keeps showing one page: the scan happens in it.
        return self._pages[0] if len(self._pages) == 1 else self._pages.pop(0)

    async def close(self):
        self.closed = True


def make_session(page_or_pages):
    s = BrowserSession("telegram")
    pages = page_or_pages if isinstance(page_or_pages, list) else [page_or_pages]
    s._ctx = FakeContext(pages)
    return s


@pytest.mark.asyncio
async def test_read_chat_returns_the_final_url_and_body():
    page = FakePage(url="https://web.telegram.org/a/im?chats=peer-9", body="Привет!")
    s = make_session(page)
    url, body = await s.read_chat("https://start", timeout_ms=0)
    assert url == page.url and body == "Привет!"
    assert page.goto_url == "https://start"
    assert page.closed                       # every opened page gets shut again


@pytest.mark.asyncio
async def test_read_chat_survives_a_never_idle_network():
    class Hanging(FakePage):
        async def wait_for_load_state(self, state, timeout=0):
            raise TimeoutError("network never idles")

    s = make_session(Hanging(body="x"))
    _, body = await s.read_chat("u", timeout_ms=0)
    assert body == "x"                        # settle-wait failure is tolerated


@pytest.mark.asyncio
async def test_send_message_types_into_the_box_and_presses_send():
    box, button = FakeElement(), FakeElement()
    page = FakePage(selectors={"div[aria-label='Message'], .input-field textarea": box,
                               "button[aria-label='Send'], .send-button-icon": button})
    s = make_session(page)
    assert await s.send_message("https://start", "текст", timeout_ms=0)
    assert box.filled == "текст" and button.clicked
    assert page.closed


@pytest.mark.asyncio
async def test_send_without_an_input_box_is_a_clean_false():
    s = make_session(FakePage(selectors={}))   # the redesigned page: no box found
    assert not await s.send_message("u", "текст", timeout_ms=0)


@pytest.mark.asyncio
async def test_send_when_the_page_throws_is_a_false_not_a_crash():
    class Exploding(FakePage):
        async def goto(self, url, **kw):
            raise RuntimeError("navigation failed")

    s = make_session(Exploding())
    assert not await s.send_message("u", "текст", timeout_ms=0)


@pytest.mark.asyncio
async def test_qr_login_saves_the_code_and_waits_for_the_scan():
    qr, logged = FakeElement(box={"x": 0, "y": 0, "width": 200, "height": 200}), FakeElement()

    class Scanning(FakePage):
        """The same page the loop polls: no QR at first, then it is scanned."""

        async def query_selector(self, sel):
            if sel == ".sidebar-wrapper, .tm-dialog-desktop-left":
                return logged if self.shots else None
            return qr

    page = Scanning()
    s = make_session(page)
    assert await s.qr_login(timeout_s=30)
    assert page.shots and not page.clip_none      # the QR was clipped and saved
    assert page.closed                            # and the login page shut again


@pytest.mark.asyncio
async def test_qr_login_times_out_without_a_scan():
    s = make_session(FakePage(selectors={}))      # neither QR nor chat list appears
    assert not await s.qr_login(timeout_s=0)


@pytest.mark.asyncio
async def test_whatsapp_qr_login_uses_the_whatsapp_page():
    qr, logged = FakeElement(box={"x": 0, "y": 0}), FakeElement()

    class Scanning(FakePage):
        async def query_selector(self, sel):
            if sel == "#app div[aria-label='Chat list'], #side":
                return logged if self.shots else None
            return qr

    page = Scanning()
    s = BrowserSession("whatsapp")
    s._ctx = FakeContext([page])
    assert await s.qr_login(timeout_s=30)
    assert page.goto_url.endswith("web.whatsapp.com/")   # the right login screen


@pytest.mark.asyncio
async def test_stop_shuts_the_context_down_once():
    ctx = FakeContext([])
    s = make_session(FakePage())
    s._ctx = ctx
    await s.stop()
    assert ctx.closed and s._ctx is None and s._pw is None


@pytest.mark.asyncio
async def test_stop_tolerates_a_browser_that_already_died():
    class Dead:
        async def close(self):
            raise RuntimeError("already gone")

    s = BrowserSession("whatsapp")
    s._ctx, s._pw = Dead(), None
    await s.stop()                                # never raises on shutdown
    assert s._ctx is None


# ── login(): the CLI entry point ──

class FakeLoginSession:
    def __init__(self, ok=True):
        self.ok, self.stopped = ok, False

    async def qr_login(self):
        return self.ok

    async def stop(self):
        self.stopped = True


@pytest.mark.asyncio
async def test_login_rejects_a_channel_without_a_personal_client():
    with pytest.raises(MessengerLoginError):
        await login("vk")


@pytest.mark.asyncio
async def test_login_stops_only_the_session_it_started():
    own = FakeLoginSession(ok=True)
    assert await login("telegram", session=own)
    assert not own.stopped                        # the caller's session stays open

    borrowed = FakeLoginSession(ok=False)
    s = make_session(FakePage(selectors={}))
    s.qr_login = lambda *a, **k: borrowed.qr_login()
    assert not await login("telegram", session=s)
    # the real BrowserSession we injected was created by us, so it is stopped
