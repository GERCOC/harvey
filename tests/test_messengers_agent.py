"""The messenger agent end to end, with a scripted model and fake bots.
Every business, person and account here is invented."""

import os
import sqlite3
import tempfile
from contextlib import closing
from datetime import datetime, timedelta

import pytest
import pytest_asyncio
import yaml

from harvey.brain import Brain
from harvey.config import EnvConfig, HarveyConfig
from harvey.messengers import store as store_mod
from harvey.messengers.agent import MessengerAgent
from harvey.messengers.bots import Inbound, SendResult
from harvey.messengers.store import MessengerStore
from harvey.models.company import Company
from harvey.paths import PROJECT_ROOT
from harvey.signals import seed_signal_catalog
from harvey.state import StateManager


def make_config(**messengers) -> HarveyConfig:
    data = yaml.safe_load((PROJECT_ROOT / "harvey.yaml").read_text(encoding="utf-8"))
    data["channels"]["messengers"] = {"enabled": True, **messengers}
    data["persona"]["email"] = "anna@bookwell.example"
    return HarveyConfig(**data)


class FakeBrain:
    """Answers think_json from a script; prompts and skills are the real files."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.prompts = []
        self.paused_reason = ""

    async def think_json(self, prompt, **kw):
        self.prompts.append(prompt)
        return self.answers.pop(0) if self.answers else None

    def load_prompt(self, name, **kw):
        return Brain.load_prompt(self, name, **kw)

    def load_skill(self, name):
        return Brain.load_skill(self, name)


class FakeBot:
    def __init__(self, channel, inbound=(), ok=True):
        self.channel = channel
        self.inbound = list(inbound)
        self.sent = []
        self.ok = ok
        self.committed = 0

    async def fetch(self, state):
        out, self.inbound = self.inbound, []
        return out

    async def commit(self, state):
        self.committed += 1

    async def send(self, chat_id, text):
        self.sent.append((chat_id, text))
        return SendResult(True, f"m{len(self.sent)}") if self.ok else SendResult(False, error="boom")


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmp:
        sm = StateManager(os.path.join(tmp, "t.db"))
        await sm.init_db()
        await seed_signal_catalog(sm)
        yield sm


def agent(state, brain, config=None, bots=None):
    a = MessengerAgent(brain, state, config or make_config(), EnvConfig())
    a.bots = bots or {}
    a.send_pacing = False
    return a


async def company_with(state, channel_code="MESSENGER_TELEGRAM", handle="acme_studio",
                       domain="acme.example", name="Acme Studio"):
    cid = await state.add_company(Company(name=name, domain=domain, website=f"https://{domain}"))
    await state.add_observations([
        {"company_id": cid, "signal_code": channel_code, "collector": "profile",
         "value_text": handle, "evidence_url": f"https://{domain}"},
        {"company_id": cid, "signal_code": "NO_ONLINE_BOOKING", "collector": "profile",
         "value_num": 1, "evidence_url": f"https://{domain}"},
    ], run_id="r1")
    return cid


def rows(state, sql, params=()):
    with closing(sqlite3.connect(state.db_path)) as db:
        return db.execute(sql, params).fetchall()


# ── First messages ──


@pytest.mark.asyncio
async def test_a_first_message_and_follow_up_are_drafted_with_the_opt_out_line(state):
    cid = await company_with(state)
    brain = FakeBrain({"message": "Здравствуйте, это Анна из Bookwell. Вы записываете клиентов только по телефону?",
                       "followup": "Анна снова. Если запись по телефону устраивает, просто скажите."})
    assert await agent(state, brain).draft_first_messages() == 1
    got = rows(state, "SELECT step, channel, handle, status, body FROM messenger_outbox "
                      "WHERE company_id = ? ORDER BY step", (cid,))
    assert [(r[0], r[1], r[2], r[3]) for r in got] == [
        (1, "telegram", "acme_studio", "pending_review"), (2, "telegram", "acme_studio", "waiting")]
    assert got[0][4].endswith("напишите «нет», и я больше не побеспокою.")
    # What the model knew: the business's own facts, fenced as data.
    assert "Acme Studio" in brain.prompts[0] and '<untrusted source="the business">' in brain.prompts[0]
    assert "acme_studio" not in brain.prompts[0]   # contact details aren't an opener


@pytest.mark.asyncio
async def test_each_business_gets_one_first_message_only(state):
    await company_with(state)
    a = agent(state, FakeBrain({"message": "Здравствуйте, это Анна. Как вы сейчас записываете клиентов?",
                                "followup": ""}))
    assert await a.draft_first_messages() == 1
    assert await a.draft_first_messages() == 0
    assert rows(state, "SELECT COUNT(*) FROM messenger_outbox")[0][0] == 1   # no follow-up without text


@pytest.mark.asyncio
async def test_channel_order_picks_which_messenger(state):
    cid = await company_with(state, "MESSENGER_TELEGRAM", "acme_studio")
    await state.add_observations([{"company_id": cid, "signal_code": "MESSENGER_WHATSAPP",
                                   "value_text": "79990001122"}], run_id="r1")
    cfg = make_config(channel_order=["whatsapp", "telegram"])
    await agent(state, FakeBrain({"message": "Здравствуйте, это Анна. Вопрос?", "followup": ""}),
                cfg).draft_first_messages()
    assert rows(state, "SELECT channel, handle FROM messenger_outbox") == [("whatsapp", "79990001122")]


@pytest.mark.asyncio
async def test_do_not_contact_and_already_emailed_businesses_are_left_out(state):
    blocked = await company_with(state, domain="rival.example", name="Rival")
    emailed = await company_with(state, domain="emailed.example", name="Emailed", handle="emailed_co")
    with closing(sqlite3.connect(state.db_path)) as db:
        db.execute("INSERT INTO prospects (id, company_id, email, status) VALUES ('p1', ?, 'o@emailed.example', 'contacted')",
                   (emailed,))
        db.commit()
    cfg = make_config()
    cfg.compliance.do_not_contact = ["rival.example"]
    brain = FakeBrain()
    assert await agent(state, brain, cfg).draft_first_messages() == 0
    assert brain.prompts == []   # no model call spent on either
    assert rows(state, "SELECT company_id, status, error FROM messenger_outbox") == [
        (blocked, "skipped", "в списке do-not-contact")]


@pytest.mark.asyncio
async def test_the_daily_draft_limit_holds(state):
    for i in range(3):
        await company_with(state, domain=f"biz{i}.example", name=f"Biz {i}", handle=f"biz_studio{i}")
    answers = [{"message": f"Здравствуйте, это Анна. Вопрос {i}?", "followup": ""} for i in range(3)]
    assert await agent(state, FakeBrain(*answers), make_config(max_daily_drafts=2)).draft_first_messages() == 2


@pytest.mark.asyncio
async def test_an_unavailable_model_drafts_nothing_and_retries_later(state):
    await company_with(state)
    brain = FakeBrain()
    brain.paused_reason = "usage limit"
    assert await agent(state, brain).draft_first_messages() == 0
    assert rows(state, "SELECT COUNT(*) FROM messenger_outbox")[0][0] == 0


@pytest.mark.asyncio
async def test_a_draft_that_breaks_the_rules_is_flagged(state):
    await company_with(state)
    await agent(state, FakeBrain({"message": "Уникальное предложение! Не упустите: https://a.example https://b.example",
                                  "followup": ""})).draft_first_messages()
    [(error,)] = rows(state, "SELECT error FROM messenger_outbox")
    assert "banned phrase" in error and "more than 1 link" in error


@pytest.mark.asyncio
async def test_sending_by_hand_schedules_the_follow_up_and_an_answer_cancels_it(state, monkeypatch):
    cid = await company_with(state)
    await agent(state, FakeBrain({"message": "Здравствуйте, это Анна. Вопрос?",
                                  "followup": "Анна снова. Вопрос?"})).draft_first_messages()
    store = MessengerStore(state)
    first = rows(state, "SELECT id FROM messenger_outbox WHERE step = 1")[0][0]
    assert await store.mark_cold_sent(first, followup_days=3)
    assert not await store.mark_cold_sent(first, followup_days=3)   # once
    [(status, send_at)] = rows(state, "SELECT status, send_at FROM messenger_outbox WHERE step = 2")
    assert status == "scheduled"
    assert await store.release_due() == 0      # not yet
    later = datetime.fromisoformat(send_at) + timedelta(minutes=1)
    monkeypatch.setattr(store_mod, "_now", lambda: later)
    assert await store.release_due() == 1
    assert rows(state, "SELECT status FROM messenger_outbox WHERE step = 2") == [("pending_review",)]
    assert await store.cancel_cold_for(cid) == 1
    assert rows(state, "SELECT status FROM messenger_outbox WHERE step = 2") == [("cancelled",)]


@pytest.mark.asyncio
async def test_skipping_a_first_message_drops_its_follow_up(state):
    await company_with(state)
    await agent(state, FakeBrain({"message": "Здравствуйте, это Анна. Вопрос?",
                                  "followup": "Анна снова."})).draft_first_messages()
    first = rows(state, "SELECT id FROM messenger_outbox WHERE step = 1")[0][0]
    assert await MessengerStore(state).skip_cold(first)
    assert rows(state, "SELECT step, status FROM messenger_outbox ORDER BY step") == [
        (1, "skipped"), (2, "cancelled")]


# ── Conversations ──


def tg(text, mid="1", chat="501", **kw):
    return Inbound(chat_id=chat, body=text, provider_msg_id=mid, at=datetime(2026, 10, 28, 9, 0),
                   handle="ivan_client", display_name="Иван", **kw)


@pytest.mark.asyncio
async def test_a_question_gets_a_draft_that_waits_for_approval(state):
    bot = FakeBot("telegram", [tg("Здравствуйте, сколько стоит?")])
    brain = FakeBrain({"intent": "question", "reply": "Здравствуйте. От 2 900 ₽ в месяц. Удобно созвониться завтра?"})
    a = agent(state, brain, bots={"telegram": bot})
    report = await a.run()
    assert report["received"] == 1 and report["answered"] == 1 and report["sent"] == 0
    assert bot.committed == 1 and bot.sent == []
    [(status, body)] = rows(state, "SELECT status, body FROM messenger_messages WHERE direction = 'out'")
    assert status == "pending_review" and body.startswith("Здравствуйте. От 2 900")
    assert '<untrusted source="the conversation">' in brain.prompts[0]
    # Approved by you: it goes on the next pass.
    [(mid,)] = rows(state, "SELECT id FROM messenger_messages WHERE direction = 'out'")
    assert await MessengerStore(state).approve_message(mid)
    await a.run()
    assert bot.sent == [("501", body)]
    assert rows(state, "SELECT status, provider_msg_id FROM messenger_messages WHERE id = ?", (mid,)) == [("sent", "m1")]


@pytest.mark.asyncio
async def test_the_same_message_twice_is_answered_once(state):
    bot = FakeBot("telegram", [tg("Сколько стоит?"), tg("Сколько стоит?")])
    brain = FakeBrain({"intent": "question", "reply": "От 2 900 ₽ в месяц. Подходит?"})
    report = await agent(state, brain, bots={"telegram": bot}).run()
    assert report["received"] == 1 and len(brain.prompts) == 1


@pytest.mark.asyncio
async def test_autopilot_sends_on_its_own_but_holds_a_foreign_link(state):
    bot = FakeBot("telegram", [tg("Сколько стоит?", chat="1", mid="1"), tg("Где почитать?", chat="2", mid="2")])
    brain = FakeBrain({"intent": "question", "reply": "От 2 900 ₽ в месяц. Подходит?"},
                      {"intent": "question", "reply": "Вот тут: https://evil.example/promo"})
    await agent(state, brain, make_config(require_approval=False), {"telegram": bot}).run()
    assert bot.sent == [("1", "От 2 900 ₽ в месяц. Подходит?")]
    [(status, error)] = rows(state, "SELECT status, error FROM messenger_messages "
                                    "WHERE direction = 'out' AND status != 'sent'")
    assert status == "held" and "evil.example" in error


@pytest.mark.asyncio
async def test_switching_approval_back_on_returns_autopilot_answers_to_you(state):
    bot = FakeBot("telegram", [tg("Сколько стоит?")])
    a = agent(state, FakeBrain({"intent": "question", "reply": "От 2 900 ₽. Подходит?"}),
              make_config(require_approval=False), {"telegram": bot})
    a.send_approved = _no_send
    await a.run()
    assert rows(state, "SELECT status, approved_by FROM messenger_messages WHERE direction = 'out'") == [
        ("approved", "autopilot")]
    a.config = make_config(require_approval=True)
    await a.run()
    assert rows(state, "SELECT status FROM messenger_messages WHERE direction = 'out'") == [("pending_review",)]


async def _no_send():
    return 0


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["Нет, спасибо", "Пожалуйста, не пишите мне больше", "СТОП", "Это спам"])
async def test_an_opt_out_is_honoured_without_asking_the_model(state, text):
    cid = await company_with(state, handle="ivan_client")
    await agent(state, FakeBrain({"message": "Здравствуйте, это Анна. Вопрос?", "followup": "Ещё вопрос."})
                ).draft_first_messages()
    bot = FakeBot("telegram", [tg(text)])
    brain = FakeBrain()
    await agent(state, brain, bots={"telegram": bot}).run()
    assert brain.prompts == [] and bot.sent == []
    assert rows(state, "SELECT status, company_id FROM messenger_threads") == [("opted_out", cid)]
    # Their business gets nothing more from you either.
    assert {s for (s,) in rows(state, "SELECT status FROM messenger_outbox")} == {"cancelled"}
    # And a later message is kept for you, never answered.
    bot.inbound = [tg("Хотя подождите, а сколько стоит?", mid="9")]
    await agent(state, brain, bots={"telegram": bot}).run()
    assert brain.prompts == [] and bot.sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["Я напишу жалобу в Роскомнадзор", "Передам юристу", "Подам иск"])
async def test_threats_and_complaints_go_to_a_person(state, text):
    brain = FakeBrain()
    await agent(state, brain, bots={"telegram": FakeBot("telegram", [tg(text)])}).run()
    assert brain.prompts == []
    assert rows(state, "SELECT status FROM messenger_threads") == [("needs_human",)]


@pytest.mark.asyncio
async def test_a_photo_without_text_goes_to_a_person(state):
    from harvey.messengers.bots import NO_TEXT
    brain = FakeBrain()
    await agent(state, brain, bots={"telegram": FakeBot("telegram", [tg(NO_TEXT, has_text=False)])}).run()
    assert brain.prompts == [] and rows(state, "SELECT status FROM messenger_threads") == [("needs_human",)]


@pytest.mark.asyncio
@pytest.mark.parametrize("intent,status", [("not_interested", "closed"), ("unsubscribe", "opted_out"),
                                           ("escalate", "needs_human"), ("gibberish", "needs_human")])
async def test_what_the_model_decides_is_final_for_no_reply_intents(state, intent, status):
    bot = FakeBot("telegram", [tg("Посмотрим")])
    await agent(state, FakeBrain({"intent": intent, "reply": "Жаль!"}), bots={"telegram": bot}).run()
    assert bot.sent == [] and rows(state, "SELECT status FROM messenger_threads") == [(status,)]
    assert rows(state, "SELECT COUNT(*) FROM messenger_messages WHERE direction = 'out'") == [(0,)]


@pytest.mark.asyncio
async def test_start_sends_the_welcome_once_and_isnt_answered(state):
    bot = FakeBot("telegram", [tg("/start", mid="1", is_start=True), tg("/start", mid="2", is_start=True)])
    brain = FakeBrain()
    await agent(state, brain, bots={"telegram": bot}).run()
    assert len(bot.sent) == 1 and bot.sent[0][1].startswith("Здравствуйте!")
    assert brain.prompts == []


@pytest.mark.asyncio
async def test_the_kill_switch_stops_messenger_sends_too(state):
    bot = FakeBot("telegram", [tg("Сколько стоит?")])
    a = agent(state, FakeBrain({"intent": "question", "reply": "От 2 900 ₽. Подходит?"}),
              make_config(require_approval=False), {"telegram": bot})
    await state.set_setting("sending_paused", "paused from dashboard")
    await a.run()
    assert bot.sent == []


@pytest.mark.asyncio
async def test_a_failed_send_is_kept_with_the_reason(state):
    bot = FakeBot("telegram", [tg("Сколько стоит?")], ok=False)
    await agent(state, FakeBrain({"intent": "question", "reply": "От 2 900 ₽. Подходит?"}),
                make_config(require_approval=False), {"telegram": bot}).run()
    assert rows(state, "SELECT status, error FROM messenger_messages WHERE direction = 'out'") == [("failed", "boom")]


@pytest.mark.asyncio
async def test_whatsapp_answers_only_within_24_hours_of_their_message(state, monkeypatch):
    from harvey.messengers.bots import WhatsAppCloud

    class FakeWa(WhatsAppCloud):
        def __init__(self):
            super().__init__("token", "123")
            self.sent = []

        async def send(self, chat_id, text):
            self.sent.append(chat_id)
            return SendResult(True, "wamid.1")

    store = MessengerStore(state)
    wa = FakeWa()
    a = agent(state, FakeBrain(), make_config(require_approval=False), {"whatsapp": wa})
    fresh = await store.thread_for("whatsapp", "bot", "79990001122")
    stale = await store.thread_for("whatsapp", "bot", "79990003344")
    now = datetime(2026, 10, 28, 12, 0)
    monkeypatch.setattr(store_mod, "_now", lambda: now)
    monkeypatch.setattr("harvey.messengers.agent._now", lambda: now)
    await store.add_inbound(fresh["id"], "Сколько стоит?", "w1", now - timedelta(hours=2))
    await store.add_inbound(stale["id"], "Сколько стоит?", "w2", now - timedelta(hours=30))
    for t in (fresh, stale):
        mid = await store.add_outbound(t["id"], "От 2 900 ₽. Подходит?", "approved", "you")
    assert await a.send_approved() == 1
    assert wa.sent == ["79990001122"]
    [(err,)] = rows(state, "SELECT error FROM messenger_messages WHERE id = ?", (mid,))
    assert "24 часов" in err


@pytest.mark.asyncio
async def test_a_pasted_answer_in_your_own_account_gets_a_draft_never_sent_by_harvey(state):
    store = MessengerStore(state)
    thread = await store.thread_for("whatsapp", "manual", "whatsapp:79990001122", handle="79990001122")
    await store.add_inbound(thread["id"], "А что по цене?")
    bot = FakeBot("whatsapp")
    await agent(state, FakeBrain({"intent": "question", "reply": "От 2 900 ₽. Подходит?"}),
                make_config(require_approval=False), {"whatsapp": bot}).run()
    assert bot.sent == []
    assert rows(state, "SELECT status FROM messenger_messages WHERE direction = 'out'") == [("pending_review",)]


@pytest.mark.asyncio
async def test_messengers_off_does_nothing(state):
    bot = FakeBot("telegram", [tg("Привет")])
    cfg = make_config()
    cfg.channels.messengers.enabled = False
    assert await agent(state, FakeBrain(), cfg, {"telegram": bot}).run() == {}
    assert bot.inbound   # not even read


@pytest.mark.asyncio
async def test_a_conversation_handed_to_you_stays_yours_until_handed_back(state):
    bot = FakeBot("telegram", [tg("Передам юристу")])
    brain = FakeBrain({"intent": "question", "reply": "От 2 900 ₽. Подходит?"})
    a = agent(state, brain, bots={"telegram": bot})
    await a.run()
    bot.inbound = [tg("Ладно, а сколько стоит?", mid="2")]
    await a.run()
    assert brain.prompts == []
    [(tid,)] = rows(state, "SELECT id FROM messenger_threads")
    await MessengerStore(state).set_thread(tid, status="open")
    bot.inbound = [tg("Так сколько?", mid="3")]
    await a.run()
    assert len(brain.prompts) == 1
