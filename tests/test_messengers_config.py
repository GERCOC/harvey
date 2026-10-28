"""channels.messengers settings and the fixed message rules."""

import pytest
import yaml
from pydantic import ValidationError

from harvey.config import HarveyConfig, MessengersConfig
from harvey.messengers import gate
from harvey.paths import PROJECT_ROOT


def config(**messengers):
    data = yaml.safe_load((PROJECT_ROOT / "harvey.yaml").read_text(encoding="utf-8"))
    data["channels"]["messengers"] = messengers
    return HarveyConfig(**data)


def test_messengers_are_off_and_answers_wait_for_you_by_default():
    cfg = config().channels.messengers
    assert cfg.enabled is False and cfg.require_approval is True
    assert not cfg.telegram.enabled and not cfg.vk.enabled and not cfg.whatsapp.enabled


def test_the_shipped_harvey_yaml_keeps_messengers_off():
    data = yaml.safe_load((PROJECT_ROOT / "harvey.yaml").read_text(encoding="utf-8"))
    assert HarveyConfig(**data).channels.messengers.enabled is False


def test_channel_order_takes_known_messengers_only():
    assert MessengersConfig(channel_order=["WhatsApp", "vk", "vk"]).channel_order == ["whatsapp", "vk"]
    with pytest.raises(ValidationError):
        MessengersConfig(channel_order=["icq"])


def test_the_opt_out_line_must_ask_for_a_reply_harvey_recognises():
    assert MessengersConfig(opt_out_line="Не актуально? Напишите «стоп».").opt_out_line
    assert MessengersConfig(opt_out_line="").opt_out_line == ""
    with pytest.raises(ValidationError):
        MessengersConfig(opt_out_line="Хорошего дня")
    with pytest.raises(ValidationError):
        MessengersConfig(opt_out_line="Отписаться: https://acme.example/unsub")


def test_the_default_opt_out_line_is_itself_an_opt_out_harvey_honours():
    # What it asks them to write is what the rules catch.
    assert gate.opt_out_reason("нет") == "нет"


@pytest.mark.parametrize("text", ["Здравствуйте! Сколько стоит?", "Спасибо, а можно подробнее?",
                                  "Нет проблем, давайте созвонимся", "Поиск идёт, судя по всему"])
def test_ordinary_messages_are_not_opt_outs_or_escalations(text):
    assert gate.opt_out_reason(text) == "" and gate.escalation_reason(text) == ""


def test_message_rules():
    cfg = config(enabled=True, max_chars=100)
    assert gate.problems("Здравствуйте, это Анна. Вопрос?", cfg) == []
    assert gate.problems("", cfg) == ["the message is empty"]
    assert "longer than 100" in gate.problems("а" * 101, cfg)[0]
    assert gate.problems("а" * 101, cfg, kind="reply") == []   # answers may run longer
    assert "placeholder" in gate.problems("Здравствуйте, {{name}}", cfg)[0]
    assert "placeholder" in gate.problems("Здравствуйте, [Имя]", cfg)[0]
    assert "HTML" in gate.problems("<b>Скидка</b>", cfg)[0]
    cfg.compliance.do_not_contact = ["rival.example"]
    assert "do-not-contact" in gate.problems("Вопрос?", cfg, company_domain="shop.rival.example")[0]
