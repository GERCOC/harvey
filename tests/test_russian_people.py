"""Finding who decides on a Russian-language site. Every business, person and
page below is invented; domains are reserved `.test` names."""

import pytest

from harvey.collectors import people as P
from harvey.collectors.russian import (
    ru_is_owner_title, ru_owner_rank, split_russian_name, transliterate,
)
from harvey.integrations.email_finder import (
    build_email, generate_patterns, infer_pattern_from_email,
)

DOMAIN = "romashka-studio.test"
SITE = "Ромашка Студия"


# ── names ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("Сидоров Сидор Сидорович", ("Сидор", "Сидоров")),          # Фамилия Имя Отчество
    ("Сидор Сидорович Сидоров", ("Сидор", "Сидоров")),          # Имя Отчество Фамилия
    ("Кузнецова Мария Андреевна", ("Мария", "Кузнецова")),
    ("Мария Кузнецова", ("Мария", "Кузнецова")),                # a known first name
    ("Кузнецова Мария", ("Мария", "Кузнецова")),
    ("ОРЛОВ ОЛЕГ ИГОРЕВИЧ", ("Олег", "Орлов")),                # shouted in a footer
    ("Анна-Мария Белова", ("Анна-Мария", "Белова")),
])
def test_russian_names_are_split_into_first_and_last(text, expected):
    assert split_russian_name(text) == expected
    assert P.split_person_name(text) == expected                  # the shared entry point


@pytest.mark.parametrize("text", [
    "Наша Команда", "Нижний Новгород", "Контакты Компании", "Санкт-Петербург",
    "Орлов О. И.", "Олег Игоревич",                               # initials, or no surname
    "Ромашка Студия", "Заказать Звонок", "Иван", "", "Иван Орлов 2024", "ivan@ромашка.test",
])
def test_headings_initials_and_brands_are_not_people(text):
    assert split_russian_name(text) is None


def test_latin_names_still_go_through_the_english_rules():
    assert P.split_person_name("Dale Whitlock") == ("Dale", "Whitlock")
    assert P.split_person_name("Team of Experts") is None


# ── titles ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("title", [
    "Генеральный директор", "Директор", "Руководитель", "Руководитель студии", "Основатель",
    "Владелец", "Совладелец", "Соучредитель", "ИП", "Индивидуальный предприниматель",
    "Главный врач", "Директор-основатель", "Управляющий партнёр",
])
def test_these_russian_titles_are_the_person_who_decides(title):
    assert ru_is_owner_title(title)
    assert P.is_owner_title(title)


@pytest.mark.parametrize("title", [
    "Коммерческий директор", "Финансовый директор", "Директор по маркетингу",
    "Директор отдела продаж", "Руководитель отдела продаж", "Руководитель проекта",
    "Заместитель директора", "Помощник руководителя", "Менеджер", "Бывший директор",
    "Основатель (1990–2001)", "И.о. директора",
])
def test_function_heads_deputies_and_former_owners_are_not(title):
    assert not ru_is_owner_title(title)
    assert not P.is_owner_title(title)


def test_rank_orders_owners_above_function_heads():
    assert ru_owner_rank("Владелец") == 100
    assert ru_owner_rank("Генеральный директор") == 95
    assert ru_owner_rank("Исполнительный директор") == 60
    assert ru_owner_rank("Коммерческий директор") == 40
    assert ru_owner_rank("Бывший директор") == 10
    assert P.owner_rank("Владелец") > P.owner_rank("Директор") > P.owner_rank("Менеджер")
    assert ru_owner_rank("Owner") is None                          # English is left to the English rules


def test_an_icp_in_either_language_finds_a_russian_title():
    assert P.title_matches("Генеральный директор", ["владелец", "директор"])
    assert P.title_matches("Генеральный директор", ["owner"])
    assert P.title_matches("Генеральный директор", ["ceo"])
    assert P.title_matches("Основатель", ["founder"])
    assert P.title_matches("Директор", ["Директор"])
    assert not P.title_matches("Коммерческий директор", ["owner"])
    assert not P.title_matches("Бывший директор", ["директор"])
    assert not P.title_matches("Менеджер", ["owner", "владелец"])
    assert P.title_matches("Owner", ["owner"])                      # English unchanged


# ── a team page ──────────────────────────────────────────────────────────

TEAM = """
<html><head><title>Команда — Ромашка Студия</title></head><body>
<h1>Наша команда</h1>
<div class="person"><h3>Сидоров Сидор Сидорович</h3><p>Генеральный директор</p></div>
<div class="person"><h3>Кузнецова Мария Андреевна</h3><p>Коммерческий директор</p></div>
<div class="person"><h3>Орлов Олег Игоревич</h3><p>Руководитель отдела продаж</p></div>
<div class="otzyvy"><p>«Отличная студия!»</p><p>Белов Борис Петрович, директор компании «Лотос»</p></div>
</body></html>
"""


def test_a_team_page_gives_the_head_first_and_skips_headings_and_customers():
    found = P.find_people([("https://romashka-studio.test/komanda", TEAM)], DOMAIN, SITE)
    names = [(p["first_name"], p["last_name"]) for p in found]
    assert names[0] == ("Сидор", "Сидоров")
    assert found[0]["owner"] and "директор" in found[0]["title"].lower()
    assert ("Борис", "Белов") not in names          # a customer in the reviews block
    ranks = {p["last_name"]: p["rank"] for p in found}
    assert ranks["Сидоров"] > ranks["Кузнецова"] > 0
    assert "Орлов" in ranks and ranks["Орлов"] < ranks["Сидоров"]


def test_a_card_with_the_title_on_the_same_line():
    html = "<ul><li>Мария Кузнецова — владелец</li><li>Наши услуги — Цены</li></ul>"
    found = P.find_people([("https://romashka-studio.test/", html)], DOMAIN, SITE)
    assert [(p["first_name"], p["last_name"], p["owner"]) for p in found] == [("Мария", "Кузнецова", True)]


# ── sentences and footers ────────────────────────────────────────────────

def _first(html, **kw):
    found = P.people_from_prose(html, DOMAIN, SITE, **kw)
    return [(p["first_name"], p["last_name"], p["title"]) for p in found]


def test_owner_named_in_a_requisites_footer():
    html = "<footer><p>ООО «Ромашка Студия». Генеральный директор: Сидоров Сидор Сидорович.</p></footer>"
    assert _first(html) == [("Сидор", "Сидоров", "Генеральный директор")]


def test_a_sole_proprietor_is_the_owner():
    html = "<footer>ИП Кузнецова Мария Андреевна, ИНН 000000000000</footer>"
    [(first, last, title)] = _first(html)
    assert (first, last) == ("Мария", "Кузнецова") and "предприниматель" in title.lower()
    assert P.is_owner_title(title)


def test_name_then_title_with_a_dash_or_comma():
    assert _first("<p>Орлов Олег Игоревич — основатель компании</p>")[0][:2] == ("Олег", "Орлов")
    assert _first("<p>Мария Кузнецова, владелец студии.</p>")[0][:2] == ("Мария", "Кузнецова")


@pytest.mark.parametrize("html", [
    "<p>Коммерческий директор Орлов Олег Игоревич</p>",
    "<p>Орлов Олег Игоревич — директор по маркетингу</p>",
    "<p>Заместитель директора Орлов Олег</p>",
    "<p>Наша Команда — Руководство</p>",
])
def test_function_heads_and_headings_in_sentences_are_ignored(html):
    assert _first(html) == []


def test_a_person_the_site_remembers_is_dropped():
    html = ("<p>Генеральный директор: Сидоров Сидор Сидорович</p>"
            "<p>Светлая память основателю Орлову Олегу Игоревичу, ушёл из жизни в 2019 году.</p>"
            "<p>Основатель: Орлов Олег Игоревич</p>")
    found = P.find_people([("https://romashka-studio.test/o-nas", html)], DOMAIN, SITE)
    assert [p["last_name"] for p in found] == ["Сидоров"]


def test_schema_org_founder_in_cyrillic():
    html = ('<script type="application/ld+json">{"@type":"LocalBusiness","name":"Ромашка Студия",'
            '"founder":{"@type":"Person","name":"Сидоров Сидор Сидорович"}}</script>')
    found = P.people_from_schema(html)
    assert [(p["first_name"], p["last_name"], p["owner"]) for p in found] == [("Сидор", "Сидоров", True)]


# ── whose business is it ─────────────────────────────────────────────────

def test_a_cyrillic_name_is_matched_to_a_latin_domain():
    assert P.names_business("Ромашка Студия", "romashka-studio.test")
    assert P.names_business("ООО «Ромашка»", "romashka.test")
    assert not P.names_business("ООО «Лотос»", "romashka.test")
    assert P.surname_on_the_door("Ромашкин", "romashkin-law.test")


def test_second_level_ru_domains_keep_the_right_label():
    assert P._domain_label("studio.spb.ru") == "studio"
    assert P._domain_label("studio.com.ru") == "studio"
    assert P._domain_label("studio.ru") == "studio"
    assert P._domain_label("ромашка.рф".encode("idna").decode()) == "romashka"


# ── pages worth reading ──────────────────────────────────────────────────

MENU = """
<nav>
 <a href="/o-kompanii">О компании</a>
 <a href="/komanda">Наша команда</a>
 <a href="/uslugi">Услуги</a>
 <a href="/kontakty">Контакты</a>
 <a href="/novosti/2024/akciya">Новости</a>
 <a href="/blog/kak-vybrat">Блог</a>
 <a href="/%D0%B2%D1%80%D0%B0%D1%87%D0%B8">Врачи</a>
 <a href="/otzyvy/">Отзывы</a>
</nav>
"""


def test_menu_links_find_russian_team_about_and_contact_pages():
    links = P.menu_links(MENU, "https://romashka-studio.test/", limit=6)
    paths = [l.removeprefix("https://romashka-studio.test") for l in links]
    assert "/komanda" in paths and "/o-kompanii" in paths and "/kontakty" in paths
    assert any("%D0%B2%D1%80" in p for p in paths)                 # the Cyrillic /врачи path
    assert paths.index("/komanda") < paths.index("/o-kompanii") < paths.index("/kontakty")
    assert not any("novosti" in p or "blog" in p or "otzyvy" in p or "uslugi" in p for p in paths)


def test_a_word_inside_a_longer_russian_word_is_not_a_menu_match():
    html = '<a href="/x">Командировки</a><a href="/y">Компаниям</a>'
    assert P.menu_links(html, "https://romashka-studio.test/") == []


# ── email addresses from Cyrillic names ──────────────────────────────────

def test_transliteration_matches_how_mailboxes_are_spelled():
    assert transliterate("Дмитрий") == "dmitry"
    assert transliterate("Сергей") == "sergey"
    assert transliterate("Щербаков") == "shcherbakov"
    assert transliterate("Хохлов") == "khokhlov"
    assert transliterate("ivan") == "ivan"


def test_addresses_are_built_from_a_cyrillic_name():
    assert build_email("{f}.{l}", "Сидор", "Сидоров", "romashka.test") == "sidor.sidorov@romashka.test"
    assert build_email("{fi}{l}", "Мария", "Кузнецова", "romashka.test") == "mkuznetsova@romashka.test"
    assert "sidorov@romashka.test" in generate_patterns("Сидор", "Сидоров", "romashka.test")
    assert build_email("{f}.{l}", "Dale", "Whitlock", "acme.test") == "dale.whitlock@acme.test"


def test_a_published_address_reveals_the_pattern_for_a_cyrillic_name():
    assert infer_pattern_from_email("s.sidorov@romashka.test", "Сидор", "Сидоров") == "{fi}.{l}"
    assert infer_pattern_from_email("sidorov@romashka.test", "Сидор", "Сидоров") == "{l}"


# ── Scout end to end, on a fake Russian site ─────────────────────────────

from tests.test_scout_decision_maker import no_network, site_scout  # noqa: E402,F401

RU_HOME = ('<html><body><h1>Ромашка Студия — студия дизайна</h1>'
           '<nav><a href="/komanda">Наша команда</a><a href="/uslugi">Услуги</a></nav></body></html>')
RU_ICP = ["Владелец", "Директор", "Основатель"]


@pytest.mark.asyncio
async def test_scout_follows_a_russian_menu_to_the_owner(no_network):
    pages = {
        "https://romashka-studio.test": RU_HOME,
        "https://romashka-studio.test/komanda": TEAM,
    }
    members = await site_scout(pages, titles=RU_ICP)._scrape_team_page("romashka-studio.test")
    assert (members[0]["first_name"], members[0]["last_name"]) == ("Сидор", "Сидоров")
    assert members[0]["owner"] is True


@pytest.mark.asyncio
async def test_an_icp_written_in_english_also_finds_the_russian_owner(no_network):
    pages = {
        "https://romashka-studio.test": RU_HOME,
        "https://romashka-studio.test/komanda": TEAM,
    }
    members = await site_scout(pages, titles=["Owner", "CEO"])._scrape_team_page("romashka-studio.test")
    assert (members[0]["first_name"], members[0]["last_name"]) == ("Сидор", "Сидоров")


@pytest.mark.asyncio
async def test_a_russian_site_without_a_menu_is_tried_under_russian_page_names(no_network):
    home = "<html><body><h1>Ромашка Студия</h1><p>Мы делаем сайты.</p></body></html>"
    pages = {
        "https://romashka-studio.test": home,
        "https://romashka-studio.test/komanda": TEAM,
    }
    scout = site_scout(pages, titles=RU_ICP)
    members = await scout._scrape_team_page("romashka-studio.test")
    assert members and members[0]["last_name"] == "Сидоров"
    assert "https://romashka-studio.test/komanda" in scout.fetched


@pytest.mark.asyncio
async def test_an_english_site_never_spends_requests_on_russian_page_names(no_network):
    pages = {"https://acme-roofing.test": "<html><body><h1>Acme Roofing</h1><p>Call us.</p></body></html>"}
    scout = site_scout(pages)
    await scout._read_site("acme-roofing.test")
    assert not any(any(seg in url for seg in ("komanda", "o-kompanii", "kontakty", "rukovodstvo", "o-nas"))
                   for url in scout.fetched)
    assert any(url.endswith("/about") for url in scout.fetched)
