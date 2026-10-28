"""Messenger contacts a business publishes on its own site, and the links
that open a chat with them. Every business, account and number here is
invented."""

import os
import sqlite3
import tempfile
from contextlib import closing

import pytest
import pytest_asyncio

from harvey.collectors.profile import ProfileCollector
from harvey.messengers.contacts import find_contacts, normalize_phone, open_link, prefills_text
from harvey.models.company import Company
from harvey.signals import SIGNAL_CATALOG, seed_signal_catalog
from harvey.state import StateManager


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmp:
        sm = StateManager(os.path.join(tmp, "t.db"))
        await sm.init_db()
        yield sm


def test_telegram_accounts_and_phone_links_count_bots_and_invites_do_not():
    html = """
      <a href="https://t.me/acme_studio">Telegram</a>
      <a href="https://t.me/acme_booking_bot">Запись через бота</a>
      <a href="https://t.me/joinchat/AbCdEf">Чат</a>
      <a href="https://t.me/+AbCdEfGh12">Группа</a>
      <a href="https://t.me/+79990001122">Написать</a>
      <a href="https://telegram.me/acme_second">old domain</a>
      <a href="tg://resolve?domain=acme_third">app link</a>
      <a href="https://t.me/share/url?url=x">Поделиться</a>
    """
    assert find_contacts(html)["telegram"] == [
        "acme_studio", "+79990001122", "acme_second", "acme_third"]


def test_whatsapp_needs_a_whatsapp_link_not_just_a_phone():
    html = """
      <p>Телефон: +7 (999) 000-11-22</p>
      <a href="https://wa.me/79990001133?text=Hi">WhatsApp</a>
      <a href="https://api.whatsapp.com/send?phone=89990001144">WA</a>
      <a href="whatsapp://send?phone=79990001155">app</a>
      <a href="https://wa.me/message/QWERTY12">Business</a>
    """
    found = find_contacts(html)["whatsapp"]
    assert found == ["79990001133", "79990001144", "79990001155", "wa.me/message/QWERTY12"]
    assert "79990001122" not in found   # a bare number is not a WhatsApp account


def test_vk_pages_and_communities_but_not_vk_itself():
    html = """
      <a href="https://vk.com/acme.studio">VK</a>
      <a href="https://vk.com/club12345">community</a>
      <a href="https://m.vk.com/public67890">mobile</a>
      <a href="https://vk.me/acme_msg">messages</a>
      <a href="https://vk.com/share.php?url=https://acme.example">share</a>
      <a href="https://vk.com/wall-12345_67">post</a>
      <a href="https://vk.com/away.php?to=x">away</a>
      <a href="https://vk.com/video-1_2">video</a>
    """
    assert find_contacts(html)["vk"] == ["acme.studio", "club12345", "club67890", "acme_msg"]


def test_a_page_with_no_messenger_links_finds_nothing():
    assert find_contacts("<p>Позвоните нам: 8 800 000-00-00</p>") == {}


def test_phone_normalisation():
    assert normalize_phone("8 (999) 000-11-22") == "79990001122"
    assert normalize_phone("+7 999 000 11 22") == "79990001122"
    assert normalize_phone("9990001122") == "79990001122"
    assert normalize_phone("123") == ""


def test_open_links_put_the_text_in_where_the_messenger_allows():
    assert open_link("whatsapp", "79990001122", "Привет") == "https://wa.me/79990001122?text=%D0%9F%D1%80%D0%B8%D0%B2%D0%B5%D1%82"
    assert open_link("telegram", "acme_studio", "a b") == "https://t.me/acme_studio?text=a%20b"
    assert open_link("telegram", "+79990001122", "x") == "https://t.me/+79990001122"
    assert open_link("vk", "club12345", "x") == "https://vk.com/write-12345"
    assert open_link("vk", "acme.studio", "x") == "https://vk.me/acme.studio"
    assert open_link("whatsapp", "wa.me/message/QWERTY12", "x") == "https://wa.me/message/QWERTY12"
    assert prefills_text("whatsapp", "79990001122") and prefills_text("telegram", "acme_studio")
    assert not prefills_text("vk", "club1") and not prefills_text("telegram", "+79990001122")


def test_a_handle_that_could_inject_a_url_gets_no_link():
    assert open_link("telegram", "evil/../../x", "") == ""
    assert open_link("vk", "a b", "") == ""
    assert open_link("whatsapp", "not a number", "") == ""


def test_messenger_signals_are_proposed_free_contact_signals():
    codes = {s["code"]: s for s in SIGNAL_CATALOG}
    for code in ("MESSENGER_TELEGRAM", "MESSENGER_WHATSAPP", "MESSENGER_VK"):
        assert codes[code]["collector"] == "profile"
        assert codes[code]["cost_note"].startswith("free")


async def _profile(state, monkeypatch, confirmed, home, contact_page=""):
    cid = await state.add_company(Company(name="Acme Studio", domain="acme.example"))

    async def fake_resolve(self, domain):
        return f"https://{domain}", home

    async def fake_first(self, site, paths):
        if "/kontakty" in paths and contact_page:
            return f"{site}/contacts", contact_page
        return "", ""

    monkeypatch.setattr(ProfileCollector, "_resolve_site", fake_resolve)
    monkeypatch.setattr(ProfileCollector, "_first_present", fake_first)
    await seed_signal_catalog(state)
    async with ProfileCollector(state, confirmed) as c:
        await c.run([{"id": cid, "name": "Acme Studio", "domain": "acme.example"}])
    with closing(sqlite3.connect(state.db_path)) as db:
        return cid, db.execute(
            "SELECT signal_code, value_text, evidence_url FROM observations "
            "WHERE signal_code LIKE 'MESSENGER_%' ORDER BY rowid").fetchall()


@pytest.mark.asyncio
async def test_profiling_records_confirmed_messenger_signals_only(state, monkeypatch):
    home = '<a href="https://t.me/acme_studio">tg</a><a href="https://wa.me/79990001122">wa</a>'
    _, rows = await _profile(state, monkeypatch, {"MESSENGER_TELEGRAM"}, home)
    assert rows == [("MESSENGER_TELEGRAM", "acme_studio", "https://acme.example")]


@pytest.mark.asyncio
async def test_profiling_reads_the_contacts_page_when_the_homepage_has_none(state, monkeypatch):
    contact = '<a href="https://vk.com/acme.studio">VK</a>'
    _, rows = await _profile(state, monkeypatch, {"MESSENGER_VK", "MESSENGER_TELEGRAM"},
                             "<p>Главная</p>", contact)
    assert rows == [("MESSENGER_VK", "acme.studio", "https://acme.example/contacts")]


@pytest.mark.asyncio
async def test_profiling_keeps_at_most_three_accounts_per_messenger(state, monkeypatch):
    home = "".join(f'<a href="https://t.me/acme_staff{i}">x</a>' for i in range(6))
    _, rows = await _profile(state, monkeypatch, {"MESSENGER_TELEGRAM"}, home)
    assert [r[1] for r in rows] == ["acme_staff0", "acme_staff1", "acme_staff2"]
