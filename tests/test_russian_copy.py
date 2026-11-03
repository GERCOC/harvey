"""Cold email and replies for the Russian market: the fixed rules (opt-outs,
complaints, spam wording, the footer) and the language Harvey writes in.
Every business, person and address is invented."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from harvey.agents.handler import Handler, _escalates, _flat, _opt_out_reason
from harvey.agents.sender import Sender
from harvey.agents.writer import Writer
from harvey.brain import Brain
from harvey.config import (
    DEFAULT_OPT_OUT_LINE, DEFAULT_OPT_OUT_LINE_RU, ComplianceConfig, PersonaConfig,
    compliance_footer_lines, compliance_notice, language_of, opt_out_line_of,
)
from harvey.gate import autopilot_review, pre_send_check
from harvey.humanize import humanize
from harvey.models.conversation import Conversation, Message
from harvey.models.prospect import Prospect
from harvey.paths import PROJECT_ROOT
from harvey.ru_text import (
    banned_phrase_ru, clean_company_name, escalation_reason, opt_out_reason, skills_for,
)
from harvey.state import StateManager
from tests.test_outbox_native import Cfg, Env


# ── what their reply says ────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "Не пишите мне больше", "Пожалуйста, не пишите нам", "Отпишите меня от рассылки",
    "Удалите меня из вашей базы", "Это спам", "Хватит писать", "Уберите из рассылки",
    "нет", "Нет, спасибо", "Стоп", "Неинтересно", "Не актуально",
])
def test_these_russian_replies_are_always_an_opt_out(text):
    assert _opt_out_reason(text, text) is not None
    assert opt_out_reason(text)


def test_no_thanks_on_a_line_of_a_longer_email_reply_is_an_opt_out_too():
    text = "Спасибо за письмо, нам это не интересно.\nНет, спасибо."
    assert _opt_out_reason(text, text)["rule"] == "no_thanks"


@pytest.mark.parametrize("text", [
    "Интересно, расскажите подробнее", "Сколько это стоит?", "Да или нет, спасибо за письмо",
    "Давайте созвонимся во вторник", "Пока нет, вернитесь в сентябре",
    "Hello, how much does it cost?",
])
def test_ordinary_russian_replies_are_not(text):
    assert _opt_out_reason(text, text) is None


@pytest.mark.parametrize("text", [
    "Передам юристу", "Я подам иск", "Пожалуюсь в Роскомнадзор", "Это нарушение 152-ФЗ",
    "Напишу жалобу в ФАС", "Пришлите претензию", "Это мошенничество", "Позову адвоката",
    "I will call my lawyer",
])
def test_threats_and_complaints_go_to_a_person(text):
    assert _escalates(_flat(text))
    assert escalation_reason(text)


@pytest.mark.parametrize("text", [
    "Мы будем искать подрядчика", "Судя по сайту, всё понятно", "Расскажите про цены",
    "Нужно поискать другой вариант", "What does it cost?",
])
def test_everyday_words_do_not_trip_the_escalation(text):
    assert not _escalates(_flat(text))


# ── a whole reply, end to end ────────────────────────────────────────────

@pytest_asyncio.fixture
async def state(tmp_path):
    s = StateManager(str(tmp_path / "r.db"))
    await s.init_db()
    return s


def legacy_config():
    return SimpleNamespace(
        channels=SimpleNamespace(email=SimpleNamespace(
            enabled=True, provider="instantly", require_approval=True,
            max_daily_sends=50, send_to_risky=False)),
        persona=SimpleNamespace(name="Анна", role="менеджер", company="Бюро", tone="спокойный"),
        product=SimpleNamespace(name="Запись", description="Онлайн-запись", objection_responses={}),
    )


async def _add(state, email="ivan@romashka.test"):
    return await state.add_prospect(Prospect(first_name="Иван", last_name="Сидоров", title="Директор",
                                             company="Ромашка", email=email))


@pytest.mark.asyncio
async def test_a_russian_refusal_stops_everything_without_asking_the_model(state):
    from harvey.config import EnvConfig
    pid = await _add(state)
    handler = Handler(None, state, legacy_config(), EnvConfig())
    handler.instantly = AsyncMock()
    handler._classify_intent = AsyncMock(side_effect=AssertionError("the model must not be asked"))
    await handler._process_reply("ivan@romashka.test", "Не пишите мне больше, это спам", "u1")
    assert (await state.get_prospect(pid)).status == "opted_out"
    handler.instantly.send_reply.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_russian_legal_threat_is_never_answered(state):
    from harvey.config import EnvConfig
    pid = await _add(state)
    handler = Handler(None, state, legacy_config(), EnvConfig())
    handler.instantly = AsyncMock()
    handler._classify_intent = AsyncMock(side_effect=AssertionError("the model must not be asked"))
    handler._generate_response = AsyncMock(side_effect=AssertionError("no reply to a threat"))
    await handler._process_reply("ivan@romashka.test", "Передам вопрос юристу, будет жалоба", "u2")
    handler.instantly.send_reply.assert_not_awaited()
    [convo] = await state.get_conversations_by_status("needs_human")
    assert convo.prospect_id == pid and convo.intent == "escalate"


# ── the language of the reply ────────────────────────────────────────────

class PromptBrain:
    def __init__(self, text="Ответ."):
        self.text, self.prompts, self.loaded = text, [], []

    def load_skills_for_agent(self, name, language="en"):
        return f"SKILLS[{name},{language}]"

    def load_prompt(self, name, **kw):
        self.loaded.append(name)
        return f"PROMPT[{name}]"

    async def think(self, prompt, **kw):
        self.prompts.append(prompt)
        return self.text


async def _reply_prompt(state, reply_text, language=None):
    pid = await _add(state)
    prospect = await state.get_prospect(pid)
    cfg = Cfg()
    if language:
        cfg.persona.language = language
    brain = PromptBrain()
    handler = Handler(brain=brain, state=state, config=cfg, env=Env())
    convo = Conversation(id="c", prospect_id=pid, thread=[Message(sender="prospect", content=reply_text)])
    await handler._generate_response("question", reply_text, prospect, convo)
    return brain


@pytest.mark.asyncio
async def test_a_reply_written_in_russian_is_answered_from_the_russian_prompt(state):
    brain = await _reply_prompt(state, "Сколько это стоит?")
    assert brain.loaded == ["handler_ru"]
    assert "SKILLS[handler,ru]" in brain.prompts[0] and "по-русски" in brain.prompts[0]


@pytest.mark.asyncio
async def test_an_english_reply_keeps_the_english_prompt(state):
    brain = await _reply_prompt(state, "How much does it cost?")
    assert brain.loaded == ["handler"] and "по-русски" not in brain.prompts[0]


@pytest.mark.asyncio
async def test_harvey_set_to_russian_answers_in_russian(state):
    brain = await _reply_prompt(state, "How much does it cost?", language="ru")
    assert brain.loaded == ["handler_ru"]


# ── the footer and the opt-out line ──────────────────────────────────────

def _cfg(language="en", **compliance):
    return SimpleNamespace(persona=SimpleNamespace(language=language),
                           compliance=ComplianceConfig(**compliance),
                           channels=SimpleNamespace(email=SimpleNamespace(enabled=True)))


def test_persona_language_is_en_or_ru_only():
    base = dict(name="A", company="B", role="C", email="a@b.test", linkedin="", tone="t")
    assert PersonaConfig(**base).language == "en"
    assert PersonaConfig(**base, language=" RU ").language == "ru"
    with pytest.raises(ValueError):
        PersonaConfig(**base, language="de")


@pytest.mark.parametrize("line", [
    DEFAULT_OPT_OUT_LINE_RU, "Ответьте «стоп», и я больше не напишу.",
    'Напишите "нет, спасибо", и всё.', "Если неинтересно, ответьте «не пишите».",
])
def test_a_russian_opt_out_line_is_accepted(line):
    assert ComplianceConfig(opt_out_line=line).opt_out_line == line


@pytest.mark.parametrize("line", [
    "Если неактуально, просто проигнорируйте письмо.", "Отпишитесь: https://x.test/off",
    "Напишите на info@x.test, если не хотите письма.",
])
def test_an_opt_out_line_nobody_can_act_on_is_refused(line):
    with pytest.raises(ValueError):
        ComplianceConfig(opt_out_line=line)


def test_the_default_footer_follows_the_language():
    ru = _cfg("ru", postal_address="ООО «Ромашка», ИНН 0000000000, г. Тест, ул. Пример, 1")
    assert opt_out_line_of(ru) == DEFAULT_OPT_OUT_LINE_RU
    assert compliance_footer_lines(ru)[-1] == DEFAULT_OPT_OUT_LINE_RU
    en = _cfg("en", postal_address="1 Example Street")
    assert compliance_footer_lines(en)[-1] == DEFAULT_OPT_OUT_LINE
    custom = _cfg("ru", opt_out_line='Reply "stop" to opt out.')
    assert opt_out_line_of(custom) == 'Reply "stop" to opt out.'      # the operator's own line wins


def test_the_missing_address_warning_is_in_russian_for_a_russian_setup():
    note = compliance_notice(_cfg("ru"))
    assert "реквизитов" in note and "CAN-SPAM" not in note
    assert "CAN-SPAM" in compliance_notice(_cfg("en"))
    assert language_of(_cfg("ru")) == "ru" and language_of(object()) == "en"


# ── the pre-send gate ────────────────────────────────────────────────────

def _gate(subject, body):
    return pre_send_check("ivan@romashka.test", subject, body, kind="reply")


@pytest.mark.parametrize("body", [
    "Иван, добрый день. Только сегодня специальная цена.",
    "Иван, добрый день. Рады предложить уникальное предложение для вашего бизнеса.",
    "Заработайте больше: выведем ваш бизнес на новый уровень.",
])
def test_russian_spam_wording_is_blocked_before_sending(body):
    result = _gate("Вопрос про запись", body)
    assert not result.ok and any("banned phrases" in r for r in result.reasons)
    assert banned_phrase_ru(body)


def test_a_plain_russian_email_passes_the_gate():
    result = _gate("Запись клиентов", "Иван, добрый день. Как у вас сейчас устроена запись клиентов?")
    assert result.ok, result.reasons


def test_the_leak_check_now_reads_russian_instruction_text():
    cfg = SimpleNamespace(persona=SimpleNamespace(email="anna@mysend.test"),
                          product=SimpleNamespace(offer=SimpleNamespace(booking_url="")),
                          channels=SimpleNamespace(email=SimpleNamespace(link_domains=[])))
    prompt = (PROJECT_ROOT / "prompts" / "writer_ru.md").read_text(encoding="utf-8")
    quoted = " ".join(prompt.split()[60:90])           # a stretch of Harvey's own rules
    assert any("writer_ru.md" in r for r in autopilot_review("", quoted, cfg))
    assert autopilot_review("", "Иван, добрый день. Как у вас устроена запись клиентов?", cfg) == []


# ── the company name in a greeting ───────────────────────────────────────

@pytest.mark.parametrize("raw,clean", [
    ("ООО «Ромашка»", "Ромашка"), ("ИП Сидоров", "Сидоров"), ("АО Север-Строй", "Север-Строй"),
    ("Ромашка, ООО", "Ромашка"), ("Студия «Лотос»", "Студия «Лотос»".strip(" «»\"'")),
    ("Acme Roofing", "Acme Roofing"), ("", ""),
])
def test_the_legal_form_and_quotes_are_left_out(raw, clean):
    assert clean_company_name(raw) == clean


def _render(language, **p):
    sender = Sender.__new__(Sender)
    sender.config = SimpleNamespace(persona=SimpleNamespace(language=language))
    fields = dict(first_name="", last_name="", company="", title="", personalization_notes="",
                  is_company_inbox=False)
    fields.update(p)
    return sender._render("{{first_name}}, добрый день. Как дела у {{company}}?", SimpleNamespace(**fields))


def test_the_greeting_for_a_company_inbox_and_the_company_name_are_rendered_in_russian():
    assert _render("ru", company="ООО «Ромашка»", is_company_inbox=False) == ", добрый день. Как дела у Ромашка?"
    sender = Sender.__new__(Sender)
    sender.config = SimpleNamespace(persona=SimpleNamespace(language="ru"))
    inbox = SimpleNamespace(first_name="", last_name="", company="Ромашка", title="",
                            personalization_notes="", is_company_inbox=True)
    assert sender._render("{{first_name}}, добрый день.", inbox) == "коллеги, добрый день."
    sender.config = SimpleNamespace(persona=SimpleNamespace(language="en"))
    assert sender._render("Hi {{first_name}},", inbox) == "Hi there,"


# ── what the writer is told ──────────────────────────────────────────────

class WriterBrain:
    def __init__(self, reply):
        self.reply, self.prompts, self.loaded, self.langs = reply, [], [], []

    def load_skills_for_agent(self, name, language="en"):
        self.langs.append(language)
        return ""

    def load_prompt(self, name, **kw):
        self.loaded.append(name)
        return f"PROMPT[{name}]"

    async def think_json(self, prompt, **kw):
        self.prompts.append(prompt)
        return self.reply


def _writer_config(language):
    return SimpleNamespace(
        channels=SimpleNamespace(email=SimpleNamespace(provider="smtp", require_approval=True)),
        product=SimpleNamespace(name="Запись", description="Онлайн-запись", key_benefits=["быстро"], pricing="от 2 900 ₽"),
        persona=SimpleNamespace(name="Анна", company="Бюро", role="менеджер", tone="спокойный",
                                language=language),
    )


async def _prospect(state, **kw):
    fields = dict(first_name="Иван", last_name="Сидоров", title="Директор", company="ООО «Ромашка»",
                  industry="Студии", email="ivan@romashka.test", email_status="verified")
    fields.update(kw)
    pid = await state.add_prospect(Prospect(**fields))
    return await state.get_prospect(pid)


@pytest.mark.asyncio
async def test_a_russian_writer_uses_the_russian_prompt_and_asks_for_russian_rules(state):
    brain = WriterBrain({"subject": "Запись клиентов", "body": "Иван, добрый день."})
    writer = Writer(brain, state, _writer_config("ru"))
    writer.skills = ""
    draft = await writer._write_personal_email(await _prospect(state))
    assert draft == {"subject": "Запись клиентов", "body": "Иван, добрый день."}
    assert brain.loaded == ["writer_ru"]
    prompt = brain.prompts[0]
    assert "Напиши ОДНО холодное письмо" in prompt and "До 70 слов" in prompt
    assert "Ромашка" in prompt and "ООО" not in prompt                  # the legal form is left out
    assert "Write ONE cold email" not in prompt


@pytest.mark.asyncio
async def test_an_english_writer_is_unchanged(state):
    brain = WriterBrain({"subject": "quick one", "body": "Hi Ivan."})
    writer = Writer(brain, state, _writer_config("en"))
    writer.skills = ""
    await writer._write_personal_email(await _prospect(state))
    assert brain.loaded == ["writer"]
    assert "Write ONE cold email" in brain.prompts[0] and "Напиши" not in brain.prompts[0]


@pytest.mark.asyncio
async def test_the_russian_sequence_prompt_and_inbox_rule(state):
    seq = [{"step": 1, "subject": "Запись", "body": "{{first_name}}, добрый день.", "delay_days": 0}]
    brain = WriterBrain(seq)
    writer = Writer(brain, state, _writer_config("ru"))
    writer.skills = ""
    await writer._write_sequence([await _prospect(state)])
    prompt = brain.prompts[0]
    assert "Напиши цепочку из 3 холодных писем" in prompt and "{{first_name}}, {{company}}, {{title}}" in prompt
    assert "ООО" not in prompt.split("людей вроде этих:")[1].split("</untrusted>")[0]


def test_english_skills_are_asked_for_exactly_as_before():
    """A brain that knows nothing of languages (every older caller) still works."""
    calls = []

    class Brain_:
        def load_skills_for_agent(self, *args, **kwargs):
            calls.append((args, kwargs))
            return "skills"

    assert skills_for(Brain_(), "writer") == "skills"
    assert skills_for(Brain_(), "writer", "en") == "skills"
    assert skills_for(Brain_(), "writer", "ru") == "skills"
    assert calls == [(("writer",), {}), (("writer",), {}), (("writer",), {"language": "ru"})]


# ── skills and prompts on disk ───────────────────────────────────────────

class _Skills:
    load_skill = Brain.load_skill
    load_skills_for_agent = Brain.load_skills_for_agent


def test_russian_skills_replace_the_english_ones_where_they_exist():
    ru = _Skills().load_skills_for_agent("writer", language="ru")
    assert "Холодные письма по-русски" in ru and "Cold Email Copywriting" not in ru
    en = _Skills().load_skills_for_agent("writer")
    assert "Cold Email Copywriting" in en and "Холодные письма по-русски" not in en
    handler_ru = _Skills().load_skills_for_agent("handler", language="ru")
    assert "Возражения: как отвечать по-русски" in handler_ru


def test_the_russian_prompts_fill_every_variable_they_are_given():
    brain = SimpleNamespace(load_prompt=lambda name, **kw: Brain.load_prompt(SimpleNamespace(), name, **kw))
    values = dict(product_name="P", product_description="D", product_benefits="- b", product_pricing="$",
                  persona_name="N", persona_company="C", persona_role="R", persona_tone="T")
    prompt = brain.load_prompt("writer_ru", **values)
    assert "C" in prompt and "{{persona_company}}" not in prompt and "{{product_name}}" not in prompt
    handler = brain.load_prompt("handler_ru", stage="qualifying")
    assert "qualifying" in handler and "{{stage}}" not in handler


@pytest.mark.asyncio
async def test_the_english_only_humanizer_leaves_a_russian_draft_alone():
    brain = AsyncMock()
    drafts = [{"subject": "Запись", "body": "Иван, добрый день. Как у вас устроена запись?"}]
    assert await humanize(brain, drafts, agent="writer", enabled=True) is drafts
    brain.think.assert_not_called()
    brain.think_json.assert_not_called()


# ── send windows in the recipient's own time ─────────────────────────────

from harvey import send_window as sw  # noqa: E402


@pytest.mark.parametrize("location,zone", [
    ("Москва", "Europe/Moscow"), ("Москва, Россия", "Europe/Moscow"),
    ("Санкт-Петербург, РФ", "Europe/Moscow"), ("Калининград", "Europe/Kaliningrad"),
    ("Самара", "Europe/Samara"), ("Екатеринбург", "Asia/Yekaterinburg"),
    ("Омск", "Asia/Omsk"), ("Новосибирск, Россия", "Asia/Novosibirsk"),
    ("Красноярск", "Asia/Krasnoyarsk"), ("Иркутск", "Asia/Irkutsk"),
    ("Хабаровск", "Asia/Vladivostok"), ("Томск, Российская Федерация", "Asia/Tomsk"),
    ("Москва, ул. Тверская, 1", "Europe/Moscow"),
    ("Vladivostok, Russia", "Asia/Vladivostok"), ("Novosibirsk, Russia", "Asia/Novosibirsk"),
    ("Минск, Беларусь", "Europe/Minsk"), ("Алматы, Казахстан", "Asia/Almaty"),
])
def test_a_russian_address_is_placed_in_its_own_time_zone(location, zone):
    assert sw.zone_for(location) == zone
    resolved, guessed = sw._resolve(location)
    assert resolved == zone and not guessed


def test_a_city_in_russia_is_not_the_same_as_russia():
    assert sw._resolve("Russia") == ("Europe/Moscow", True)             # a guess, as before
    assert sw._resolve("Йошкар-Ола, Россия") == ("Europe/Moscow", True)  # unknown city: the guess
    assert sw.country_for("Новосибирск") == ("RU", True)


def test_a_latin_name_needs_russia_beside_it():
    assert sw.zone_for("Moscow, Idaho") == "America/Boise"
    assert sw.zone_for("Moscow") == ""                                   # nothing says where
    assert sw.zone_for("Omsk") == ""
    assert sw.zone_for("Omsk, Russia") == "Asia/Omsk"
