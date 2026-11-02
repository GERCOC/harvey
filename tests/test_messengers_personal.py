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
    REACHABLE, UNKNOWN, UNREACHABLE, AvailabilityResult, PersonalClient,
    chat_url, is_bot_handle, parse_telegram_page, parse_whatsapp_page)
from harvey.messengers.store import MessengerStore
from harvey.models.company import Company
from harvey.paths import PROJECT_ROOT
from harvey.signals import seed_signal_catalog
from harvey.state import StateManager


def make_config(personal=None, do_not_contact=()) -> HarveyConfig:
    data = yaml.safe_load((PROJECT_ROOT / "harvey.yaml").read_text(encoding="utf-8"))
    on = {"enabled": True, "confirm_risk": True, "active_hours": ["00:00", "23:59"]}
    # personal=None means "the personal path is off"; a dict tweaks the on-config
    if personal is None:
        personal = {"enabled": False, "confirm_risk": False}
    elif set(personal) != {"enabled"}:
        personal = {**on, **personal}
    else:
        personal = {**on, **personal}   # {"enabled": True} still needs confirm_risk from `on`
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
    await _seed_draft(state, availability=REACHABLE)
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
