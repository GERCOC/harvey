"""Telegram, VK and WhatsApp clients against scripted HTTP, and the WhatsApp
webhook. No request leaves the test; tokens and numbers are invented."""

import hashlib
import hmac
import json
import os
import sqlite3
import tempfile
from contextlib import closing
from urllib.parse import parse_qs

import httpx
import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from harvey.config import EnvConfig
from harvey.messengers.bots import NO_TEXT, TelegramBot, VkGroup, WhatsAppCloud
from harvey.messengers.whatsapp_webhook import create_app, messages_in, signature_ok
from harvey.state import StateManager


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmp:
        sm = StateManager(os.path.join(tmp, "t.db"))
        await sm.init_db()
        yield sm


def transport(handler):
    calls = []

    def wrapped(request: httpx.Request):
        calls.append(request)
        return handler(request)

    return httpx.MockTransport(wrapped), calls


# ── Telegram ──


@pytest.mark.asyncio
async def test_telegram_reads_private_chats_only_and_remembers_where_it_stopped(state):
    updates = {"ok": True, "result": [
        {"update_id": 10, "message": {"message_id": 1, "date": 1761638400, "text": "/start",
                                      "chat": {"id": 501, "type": "private"},
                                      "from": {"id": 501, "username": "ivan_client", "first_name": "Иван"}}},
        {"update_id": 11, "message": {"message_id": 2, "date": 1761638460, "text": "Сколько стоит?",
                                      "chat": {"id": 501, "type": "private"},
                                      "from": {"id": 501, "username": "ivan_client", "first_name": "Иван"}}},
        {"update_id": 12, "message": {"message_id": 3, "date": 1761638470, "text": "всем привет",
                                      "chat": {"id": -9, "type": "group"}, "from": {"id": 7}}},
        {"update_id": 13, "message": {"message_id": 4, "date": 1761638480, "text": "beep",
                                      "chat": {"id": 8, "type": "private"}, "from": {"id": 8, "is_bot": True}}},
        {"update_id": 14, "message": {"message_id": 5, "date": 1761638490, "photo": [{}],
                                      "chat": {"id": 501, "type": "private"}, "from": {"id": 501}}},
    ]}
    t, calls = transport(lambda r: httpx.Response(200, json=updates))
    bot = TelegramBot("123:ABC", transport=t)
    got = await bot.fetch(state)
    assert [(m.chat_id, m.body, m.is_start) for m in got] == [
        ("501", "/start", True), ("501", "Сколько стоит?", False), ("501", NO_TEXT, False)]
    assert got[1].handle == "ivan_client" and got[1].display_name == "Иван"
    assert await state.get_setting(TelegramBot.OFFSET_KEY) == ""   # not until stored
    await bot.commit(state)
    assert await state.get_setting(TelegramBot.OFFSET_KEY) == "15"
    assert json.loads(calls[0].content)["allowed_updates"] == ["message"]

    t2, calls2 = transport(lambda r: httpx.Response(200, json={"ok": True, "result": []}))
    bot2 = TelegramBot("123:ABC", transport=t2)
    assert await bot2.fetch(state) == []
    assert json.loads(calls2[0].content)["offset"] == 15
    await bot2.commit(state)
    assert await state.get_setting(TelegramBot.OFFSET_KEY) == "15"


@pytest.mark.asyncio
async def test_telegram_send_and_test():
    def handler(r):
        if r.url.path.endswith("/sendMessage"):
            assert json.loads(r.content) == {"chat_id": "501", "text": "Здравствуйте"}
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 77}})
        return httpx.Response(200, json={"ok": True, "result": {"username": "acme_help_bot"}})

    t, _ = transport(handler)
    bot = TelegramBot("123:ABC", transport=t)
    res = await bot.send("501", "Здравствуйте")
    assert res.ok and res.provider_msg_id == "77"
    assert await bot.test() == (True, "@acme_help_bot")


@pytest.mark.asyncio
async def test_telegram_errors_come_back_as_reasons():
    t, _ = transport(lambda r: httpx.Response(403, json={"ok": False, "description": "Forbidden: bot was blocked by the user"}))
    res = await TelegramBot("1:x", transport=t).send("501", "x")
    assert not res.ok and "blocked by the user" in res.error


# ── VK ──


@pytest.mark.asyncio
async def test_vk_reads_unanswered_conversations_with_people(state):
    def handler(r):
        method = r.url.path.rsplit("/", 1)[1]
        form = parse_qs(r.content.decode())
        assert form["access_token"] == ["vk-token"] and form["v"] == ["5.199"]
        if method == "messages.getConversations":
            assert form["filter"] == ["unanswered"]
            return httpx.Response(200, json={"response": {
                "items": [{"conversation": {"peer": {"id": 42, "type": "user"}}},
                          {"conversation": {"peer": {"id": 2000000001, "type": "chat"}}}],
                "profiles": [{"id": 42, "first_name": "Мария", "last_name": "Котова", "screen_name": "m_kotova"}]}})
        if method == "messages.getHistory":
            assert form["peer_id"] == ["42"]
            return httpx.Response(200, json={"response": {"items": [
                {"id": 3, "out": 0, "text": "А доставка есть?", "date": 1761638500},
                {"id": 2, "out": 1, "text": "Здравствуйте!", "date": 1761638450},
                {"id": 1, "out": 0, "text": "Добрый день", "date": 1761638400}]}})
        raise AssertionError(method)

    t, _ = transport(handler)
    got = await VkGroup("vk-token", transport=t).fetch(state)
    assert [(m.chat_id, m.provider_msg_id, m.body) for m in got] == [
        ("42", "1", "Добрый день"), ("42", "3", "А доставка есть?")]
    assert got[0].display_name == "Мария Котова" and got[0].handle == "m_kotova"


@pytest.mark.asyncio
async def test_vk_send_uses_a_random_id_and_reports_errors():
    seen = []

    def handler(r):
        form = parse_qs(r.content.decode())
        seen.append(form)
        if len(seen) == 1:
            return httpx.Response(200, json={"response": 555})
        return httpx.Response(200, json={"error": {"error_code": 901, "error_msg": "Can't send messages for users without permission"}})

    t, _ = transport(handler)
    vk = VkGroup("vk-token", transport=t)
    ok = await vk.send("42", "Да, доставка есть.")
    assert ok.ok and ok.provider_msg_id == "555" and seen[0]["random_id"][0].isdigit()
    bad = await vk.send("42", "x")
    assert not bad.ok and "without permission" in bad.error


# ── WhatsApp ──


@pytest.mark.asyncio
async def test_whatsapp_send_and_errors():
    def handler(r):
        body = json.loads(r.content)
        assert r.headers["authorization"] == "Bearer wa-token"
        assert r.url.path == "/v21.0/123456/messages"
        if body["to"] == "79990001122":
            assert body["text"] == {"body": "Здравствуйте", "preview_url": False}
            return httpx.Response(200, json={"messages": [{"id": "wamid.A"}]})
        return httpx.Response(400, json={"error": {"message": "Re-engagement message"}})

    t, _ = transport(handler)
    wa = WhatsAppCloud("wa-token", "123456", transport=t)
    ok = await wa.send("79990001122", "Здравствуйте")
    assert ok.ok and ok.provider_msg_id == "wamid.A"
    bad = await wa.send("79990003344", "x")
    assert not bad.ok and "Re-engagement" in bad.error


def _delivery(text="Сколько стоит?", mid="wamid.IN1"):
    return {"object": "whatsapp_business_account", "entry": [{"changes": [{"value": {
        "contacts": [{"wa_id": "79990001122", "profile": {"name": "Ольга"}}],
        "messages": [{"from": "79990001122", "id": mid, "timestamp": "1761638400",
                      "type": "text", "text": {"body": text}},
                     {"from": "79990001122", "id": "wamid.IMG", "timestamp": "1761638401",
                      "type": "image", "image": {}}]}}]}]}


def test_webhook_payload_parsing():
    got = messages_in(_delivery())
    assert [(m["wa_id"], m["name"], m["text"]) for m in got] == [
        ("79990001122", "Ольга", "Сколько стоит?"), ("79990001122", "Ольга", NO_TEXT)]
    assert messages_in({"entry": [{"changes": [{"value": {"statuses": [{"id": "x"}]}}]}]}) == []


def test_signature_check():
    body = b'{"a":1}'
    good = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    assert signature_ok("s3cret", body, good)
    assert not signature_ok("s3cret", body + b" ", good)
    assert not signature_ok("", body, good)
    assert not signature_ok("s3cret", body, "")
    assert not signature_ok("s3cret", body, good.replace("sha256=", "sha1="))


@pytest.mark.asyncio
async def test_webhook_stores_signed_messages_only(state):
    env = EnvConfig(whatsapp_app_secret="s3cret", whatsapp_verify_token="verify-me")
    client = TestClient(create_app(state, env))

    assert client.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "verify-me",
                                          "hub.challenge": "42"}).text == "42"
    assert client.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "nope",
                                          "hub.challenge": "42"}).status_code == 403

    body = json.dumps(_delivery()).encode()
    assert client.post("/webhook", content=body).status_code == 401
    assert client.post("/webhook", content=body,
                       headers={"X-Hub-Signature-256": "sha256=" + "0" * 64}).status_code == 401
    sig = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    r = client.post("/webhook", content=body, headers={"X-Hub-Signature-256": sig})
    assert r.status_code == 200 and r.json()["stored"] == 2
    # Meta retries deliveries: the same one again stores nothing new.
    assert client.post("/webhook", content=body, headers={"X-Hub-Signature-256": sig}).json()["stored"] == 0
    with closing(sqlite3.connect(state.db_path)) as db:
        assert db.execute("SELECT channel, transport, chat_id, display_name FROM messenger_threads").fetchall() == [
            ("whatsapp", "bot", "79990001122", "Ольга")]
        assert db.execute("SELECT COUNT(*) FROM messenger_messages WHERE direction = 'in'").fetchone() == (2,)
