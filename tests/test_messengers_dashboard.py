"""The Messengers tab's API: sending first messages by hand, answers in
your own account, and approving bot answers. Invented data throughout."""

import asyncio
import re
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

import harvey.dashboard as dash
from harvey.messengers.store import MessengerStore
from harvey.models.company import Company
from harvey.paths import PROJECT_ROOT
from harvey.state import StateManager
from tests.test_dashboard_signals import client  # noqa: F401  (fixture)

WEB = PROJECT_ROOT / "harvey" / "web"


@pytest.fixture
def mclient(client, monkeypatch, tmp_path):
    data = yaml.safe_load((PROJECT_ROOT / "harvey.yaml").read_text(encoding="utf-8"))
    data["channels"]["messengers"] = {"enabled": True, "max_daily_manual_sends": 2, "followup_days": 3}
    cfg = tmp_path / "harvey.local.yaml"
    cfg.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    monkeypatch.setattr(dash, "_resolve_config_file", lambda: cfg)
    return client


def seed(db_path, n=1, body="Здравствуйте, это Анна из Bookwell. Вы записываете клиентов по телефону?"):
    async def go():
        state = StateManager(str(db_path))
        await state.init_db()
        store = MessengerStore(state)
        ids = []
        for i in range(n):
            cid = await state.add_company(Company(name=f"Acme {i}", domain=f"acme{i}.example"))
            ids.append(await store.add_cold(cid, "whatsapp", f"7999000112{i}", body, "Анна снова. Вопрос?"))
        return ids
    return asyncio.run(go())


def q(db_path, sql, params=()):
    with closing(sqlite3.connect(db_path)) as db:
        return db.execute(sql, params).fetchall()


def test_overview_lists_the_queue(mclient):
    [item] = seed(mclient.db_path)
    data = mclient.get("/api/messengers").json()
    assert data["enabled"] is True and data["daily_cap"] == 2 and data["sent_today"] == 0
    [row] = data["queue"]
    assert row["id"] == item and row["channel_label"] == "WhatsApp" and row["prefills"] is True
    assert row["company_name"] == "Acme 0"


def test_open_gives_the_chat_link_and_sent_schedules_the_follow_up(mclient):
    [item] = seed(mclient.db_path)
    r = mclient.post(f"/api/messengers/cold/{item}/open").json()
    assert r["success"] and r["link"].startswith("https://wa.me/79990001120?text=")
    assert q(mclient.db_path, "SELECT status FROM messenger_outbox WHERE id = ?", (item,)) == [("pending_review",)]
    assert mclient.post(f"/api/messengers/cold/{item}/sent").json()["success"]
    assert mclient.post(f"/api/messengers/cold/{item}/sent").status_code == 409
    assert q(mclient.db_path, "SELECT step, status FROM messenger_outbox ORDER BY step") == [
        (1, "sent"), (2, "scheduled")]


def test_the_daily_limit_stops_opening_more(mclient):
    ids = seed(mclient.db_path, n=3)
    for item in ids[:2]:
        mclient.post(f"/api/messengers/cold/{item}/sent")
    r = mclient.post(f"/api/messengers/cold/{ids[2]}/open")
    assert r.status_code == 409 and "limit of 2" in r.json()["message"]


def test_a_message_breaking_the_rules_wont_open_until_fixed(mclient):
    [item] = seed(mclient.db_path, body="Уникальное предложение, не упустите!")
    r = mclient.post(f"/api/messengers/cold/{item}/open")
    assert r.status_code == 409 and "banned phrase" in r.json()["message"]
    assert mclient.post(f"/api/messengers/cold/{item}/edit",
                        json={"body": "Здравствуйте, это Анна. Как вы сейчас ведёте запись?"}).json()["success"]
    assert mclient.post(f"/api/messengers/cold/{item}/open").json()["success"]


def test_they_answered_cancels_the_follow_up_and_starts_a_thread(mclient):
    [item] = seed(mclient.db_path)
    mclient.post(f"/api/messengers/cold/{item}/sent")
    r = mclient.post(f"/api/messengers/cold/{item}/replied", json={"text": "А сколько стоит?"}).json()
    assert r["success"]
    assert q(mclient.db_path, "SELECT step, status FROM messenger_outbox ORDER BY step") == [
        (1, "sent"), (2, "cancelled")]
    [thread] = mclient.get("/api/messengers").json()["threads"]
    assert thread["transport"] == "manual" and thread["id"] == r["thread_id"]
    assert [(m["direction"], m["body"][:12]) for m in thread["messages"]] == [
        ("out", "Здравствуйте"), ("in", "А сколько ст")]


def test_drafts_in_your_own_account_are_copied_not_approved(mclient):
    async def go():
        store = MessengerStore(StateManager(str(mclient.db_path)))
        await store.state.init_db()
        manual = await store.thread_for("telegram", "manual", "telegram:acme_studio")
        bot = await store.thread_for("telegram", "bot", "501")
        return (await store.add_outbound(manual["id"], "От 2 900 ₽.", "pending_review"),
                await store.add_outbound(bot["id"], "От 2 900 ₽.", "pending_review"))
    mine, theirs = asyncio.run(go())
    assert mclient.post(f"/api/messengers/messages/{mine}/approve").status_code == 409
    assert mclient.post(f"/api/messengers/messages/{mine}/copied").json()["success"]
    assert mclient.post(f"/api/messengers/messages/{theirs}/copied").status_code == 409
    assert mclient.post(f"/api/messengers/messages/{theirs}/edit", json={"body": "От 2 900 ₽ в месяц."}).json()["success"]
    assert mclient.post(f"/api/messengers/messages/{theirs}/approve").json()["success"]
    assert q(mclient.db_path, "SELECT body, status, approved_by FROM messenger_messages WHERE id = ?", (theirs,)) == [
        ("От 2 900 ₽ в месяц.", "approved", "you")]


def test_marking_a_conversation_do_not_write_stops_everything(mclient):
    [item] = seed(mclient.db_path)
    tid = mclient.post(f"/api/messengers/cold/{item}/replied", json={}).json()["thread_id"]
    assert mclient.post(f"/api/messengers/threads/{tid}/status", json={"status": "opted_out"}).json()["success"]
    assert mclient.post(f"/api/messengers/threads/{tid}/status", json={"status": "nonsense"}).status_code == 400
    assert q(mclient.db_path, "SELECT status FROM messenger_threads") == [("opted_out",)]


def test_the_api_needs_the_token(mclient):
    anon = TestClient(dash.app, base_url="http://127.0.0.1:5555")
    assert anon.get("/api/messengers").status_code == 401
    assert anon.post("/api/messengers/cold/x/sent").status_code == 401


def test_the_tab_is_served_and_talks_only_through_the_authed_api(mclient):
    html = mclient.get("/").text
    assert html.index("/static/app.js") < html.index("/static/messengers.js")
    assert 'id="messengers"' in html and "showTab('messengers'" in html
    js = (WEB / "messengers.js").read_text(encoding="utf-8")
    assert re.findall(r"(?<![A-Za-z])fetch\(", js) == []
    assert mclient.get("/static/messengers.js").status_code == 200
