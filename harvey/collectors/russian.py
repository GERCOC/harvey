"""Russian-language support for finding who decides at a small business.

Cost: $0. Pure functions, no network, no model. They sit beside the English
rules in ``people.py`` and only act on text that contains Cyrillic, so an
English page is read exactly as before.

What is different in Russian:

* A person is "Фамилия Имя Отчество" or "Имя Отчество Фамилия" as often as
  "Имя Фамилия", and the patronymic (-ович, -евна...) is the strongest sign
  that three capitalised words are a person and not a heading.
* Nothing like a capitalised ordinary word separates a name from a heading in
  Russian ("Наша Команда"), so a two-word name needs a first name Harvey
  knows; a three-word name needs a patronymic. Both are conservative on
  purpose: a name that fits neither is skipped, never guessed.
* "Директор" alone is the head of a small business; "коммерческий директор"
  and "директор по маркетингу" are not.
* An email address is written in Latin letters, so a Cyrillic name has to be
  transliterated before an address pattern can be built from it.

Names are not declined back to the nominative ("основана Иваном Петровым"):
the sentence is skipped instead.
"""

import re

_CYR = re.compile(r"[А-Яа-яЁё]")


def has_cyrillic(text: str) -> bool:
    return bool(_CYR.search(text or ""))


def norm_ru(text: str) -> str:
    """Lower case, ё as е: the form every comparison below uses."""
    return (text or "").lower().replace("ё", "е")


# ── transliteration, for email addresses ─────────────────────────────────

_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts",
    "ч": "ch", "ш": "sh", "щ": "shch", "ъ": "", "ы": "y", "ь": "", "э": "e",
    "ю": "yu", "я": "ya",
}


def transliterate(text: str) -> str:
    """Latin letters for a Cyrillic word, in the spelling most mailboxes use
    (Дмитрий -> dmitry, Сергей -> sergey, Петров -> petrov). Anything that is
    not Cyrillic is kept. A heuristic: addresses built from it are verified
    before they are used, so a different spelling costs a lookup, not a mail."""
    words = []
    for word in (text or "").lower().split():
        out = "".join(_TRANSLIT.get(ch, ch) for ch in word)
        out = re.sub(r"(?<=[a-z])iy$", "y", out)       # -ий: dmitriy -> dmitry
        words.append(out)
    return " ".join(words)


# ── first names ──────────────────────────────────────────────────────────

# Common given names (normalised: lower case, ё as е). A two-word name is
# accepted only when one word is in this list, because nothing else tells
# "Иван Петров" from a heading like "Нижний Новгород".
_FIRST_NAMES = frozenset("""
александр алексей анатолий андрей антон аркадий арсений артем артур борис вадим валентин
валерий василий виктор виталий владимир владислав всеволод вячеслав геннадий георгий герман
глеб григорий давид даниил данил денис дмитрий евгений егор захар иван игорь илья кирилл
константин леонид лев максим марк матвей михаил мирослав никита николай олег павел петр
платон прохор роман ростислав руслан савелий святослав семен сергей станислав степан тимофей
тимур тихон федор филипп эдуард эльдар эмиль юрий яков ярослав ян богдан макар лука
альберт арсен булат дамир ильдар ильнур ильяс марат рашид ренат ринат рустам салават
тагир шамиль азат айрат радик камиль заур расул магомед мурад али ахмед тигран армен левон
эрнест игнат клим назар трофим емельян аскольд
александра алевтина алена алина алла анастасия ангелина анжела анна антонина арина валентина
валерия варвара василиса вера вероника виктория виолетта влада галина дарья диана евгения
екатерина елена елизавета жанна зинаида злата зоя инна ирина карина кира клавдия ксения лана
лариса лидия лилия любовь людмила маргарита марина мария милана милена надежда наталья
наталия нелли нина оксана олеся ольга полина раиса регина римма роза светлана снежана софия
софья тамара татьяна ульяна юлия яна ярослава эльвира эльмира гульнара гузель альфия айгуль
динара лейла рената зарина камилла таисия эльза эвелина элина агата агния ева евдокия майя
лада лолита
""".split())

# ── patronymics ──────────────────────────────────────────────────────────

_PATRONYMIC = re.compile(r"(?:[ое]вич|ьич|[ое]вна|ьевна|ична|инична|оглы|кызы|гызы)$")
_SURNAME_END = re.compile(
    r"(?:ов|ев|ин|ын|ский|цкий|ской|ская|цкая|ова|ева|ина|ына|ых|их|енко|ук|юк|чук|ян|дзе|швили|"
    r"ский|ий|ый|ой|ая|ик|ко)$")
_NAME_TOKEN = re.compile(r"^[А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)*$")


def _is_patronymic(token: str) -> bool:
    return len(token) >= 5 and bool(_PATRONYMIC.search(token))


def _is_first_name(token: str) -> bool:
    return any(part in _FIRST_NAMES for part in token.split("-"))


# ── words that make a "name" not a name ──────────────────────────────────

RU_COMMON_WORDS = frozenset("""
наша наш наши наше команда команды контакты контакт компания компании компанию руководство
руководитель руководители сотрудники сотрудник специалисты специалист врачи врач юристы юрист
адвокаты адвокат отзывы отзыв вакансии новости услуги цены главная статьи акции мы нас вас вам
клиника клиники центр студия салон магазин агентство группа фирма партнеры партнер история
миссия ценности преимущества галерея портфолио проекты проект лицензии документы реквизиты
политика конфиденциальность обратная связь заказать звонок записаться запись прием консультация
главный генеральный исполнительный технический коммерческий финансовый директор основатель
владелец учредитель президент менеджер заведующий управляющий собственник москва санкт петербург
россия россии город области область район улица дом офис часы работы режим адрес телефон почта
образование опыт стаж лет год года школа курсы курс цена стоимость заказ доставка оплата
гарантия гарантии сертификаты награды достижения клиенты клиент наши работы
""".split())


def _common_word(token: str) -> bool:
    return any(part in RU_COMMON_WORDS for part in norm_ru(token).split("-"))


def split_russian_name(text: str) -> tuple[str, str] | None:
    """(first, last) for a real-looking Russian personal name, else None.

    "Иванов Иван Петрович", "Иван Петрович Иванов" and "Иван Иванов" are all
    accepted when the words fit the rules in the module docstring; initials
    ("Иванов И. П.") are not, because nothing usable is left for a greeting.
    """
    text = re.sub(r"\s+", " ", (text or "").replace("\xa0", " ")).strip(" ,.-|–—:;«»\"'")
    if not text or len(text) > 60 or any(ch.isdigit() for ch in text) or "@" in text:
        return None
    if text.isupper():           # "ИВАНОВ ИВАН ПЕТРОВИЧ" in a footer
        text = " ".join("-".join(p.capitalize() for p in t.split("-")) for t in text.split())
    tokens = text.split()
    if not 2 <= len(tokens) <= 3 or not all(_NAME_TOKEN.match(t) for t in tokens):
        return None
    if any(_common_word(t) for t in tokens):
        return None
    low = [norm_ru(t) for t in tokens]
    if len(tokens) == 3:
        if _is_patronymic(low[2]) and not _is_patronymic(low[1]):       # Иванов Иван Петрович
            return tokens[1], tokens[0]
        if _is_patronymic(low[1]) and not _is_patronymic(low[2]):       # Иван Петрович Иванов
            return tokens[0], tokens[2]
        return None
    a, b = tokens
    first_a, first_b = _is_first_name(low[0]), _is_first_name(low[1])
    if _is_patronymic(low[1]) or _is_patronymic(low[0]):
        return None                                                     # "Иван Петрович": no surname
    if first_a and not first_b:
        return a, b
    if first_b and not first_a:
        return b, a
    if first_a and first_b:
        if _SURNAME_END.search(low[1]) and not _SURNAME_END.search(low[0]):
            return a, b
        if _SURNAME_END.search(low[0]) and not _SURNAME_END.search(low[1]):
            return b, a
    return None


# ── job titles ───────────────────────────────────────────────────────────

# Words before "директор" that make it a function head, not the head of the
# business ("коммерческий директор"), and words after it that do the same
# ("директор по маркетингу", "директор отдела продаж").
_DIRECTOR_QUALIFIERS = frozenset("""
коммерческий финансовый технический креативный арт операционный региональный медицинский
производственный исполнительный творческий маркетинговый административный информационный музыкальный
спортивный учебный художественный заместитель зам помощник ассистент
""".split())
_FUNCTION_TAIL = r"(?:по|отдела|департамента|направления|филиала|магазина)"
_SUBORDINATE_RU = re.compile(
    r"(?i)\b(?:заместитель|зам|помощник|помощница|ассистент|ассистентка|секретарь|референт)\b\.?")
_ACTING_RU = re.compile(r"(?i)\bи\.\s?о\.?(?=\s)|\bвр\.?\s?и\.?\s?о\b|\bисполняющий\s+обязанности\b")
_FORMER_RU = re.compile(
    r"(?i)\b(?:бывш(?:ий|ая|ие)|экс|ранее|прежн(?:ий|яя)|покойн\w+|в\s+память|светлой\s+памяти)\b"
    r"|(?:19|20)\d{2}\s*[-–—]\s*(?:19|20)\d{2}|\bэкс-")

# The owner-type titles: who, in a small business, is the one who decides.
_OWNER_WORD_RU = (r"(?:владел(?:ец|ица)|собственни(?:к|ца)|совладел(?:ец|ица)|основател(?:ь|ьница)|"
                  r"соосновател(?:ь|ьница)|со-?учредител(?:ь|ьница)|учредител(?:ь|ьница)|"
                  r"индивидуальный\s+предприниматель|предприниматель)")
_HEAD_WORD_RU = (r"(?:генеральный\s+директор|гендиректор|управляющий\s+партнер|главный\s+врач|"
                 r"президент|председатель\s+правления|управляющий\s+директор|"
                 rf"директор(?!\s+{_FUNCTION_TAIL}\b)|"
                 r"руководитель(?:\s+(?:компании|организации|клиники|студии|агентства|центра|фирмы|"
                 r"предприятия|бюро|салона|бизнеса))?(?=\s*$|\s*[,.;:()])|\bип\b)")
_OWNER_TITLE_RU = re.compile(rf"(?i)(?<![а-я]){_OWNER_WORD_RU}(?![а-я])")
_HEAD_TITLE_RU = re.compile(rf"(?i)(?<![а-я]){_HEAD_WORD_RU}(?![а-я])")
_DIRECTOR_RU = re.compile(r"(?i)(?<![а-я])директор(?![а-я])")


def _word_before(text: str, start: int) -> str:
    m = re.search(r"([а-яё]+)[\s\-]*$", norm_ru(text[:start]))
    return m.group(1) if m else ""


def _is_function_director(title: str) -> bool:
    """Every 'директор' in the title is qualified as a function head."""
    t = norm_ru(title)
    for m in _DIRECTOR_RU.finditer(t):
        tail_function = re.match(rf"\s+{_FUNCTION_TAIL}\b", t[m.end():])
        if not (tail_function or _word_before(t, m.start()) in _DIRECTOR_QUALIFIERS):
            return False
    return bool(_DIRECTOR_RU.search(t))


def ru_is_former(title: str) -> bool:
    return bool(_FORMER_RU.search(title or ""))


def ru_is_subordinate(title: str) -> bool:
    return bool(_SUBORDINATE_RU.search(title or ""))


def ru_is_owner_title(title: str) -> bool:
    """The title of the person who decides, in Russian."""
    t = norm_ru(title).strip()
    if not t or ru_is_former(t) or ru_is_subordinate(t) or _ACTING_RU.search(t):
        return False
    if _OWNER_TITLE_RU.search(t):
        return True
    if "директор" in t and _is_function_director(t):
        return False
    return bool(_HEAD_TITLE_RU.search(t))


def ru_owner_rank(title: str, owner: bool = False) -> int | None:
    """Rank for a Russian title (same scale as people.owner_rank), None when
    the title has nothing Russian to say about rank."""
    t = norm_ru(title).strip()
    if not has_cyrillic(t):
        return None
    if ru_is_former(t) or ru_is_subordinate(t):
        return 10
    if re.search(rf"(?<![а-я])(?:владел\w+|собственни\w+|совладел\w+|индивидуальный\s+предприниматель|"
                 r"предприниматель)(?![а-я])|(?<![а-я])ип(?![а-я])", t):
        return 100
    if ru_is_owner_title(t):
        return 95
    if _ACTING_RU.search(t) or re.search(r"(?<![а-я])(?:исполнительный\s+директор|управляющий)(?![а-я])", t):
        return 60
    if re.search(r"(?<![а-я])(?:директор|руководитель|начальник|заведующ\w+|менеджер|врач)(?![а-я])", t):
        return 40
    return 10


# English aliases, so an ICP written in English ("owner", "ceo") also finds
# a Russian title, and the other way round for the common words.
_ALIASES = (
    (r"владел|собственни|совладел|индивидуальный\s+предприниматель|(?<![а-я])ип(?![а-я])", "owner"),
    (r"основател|учредител", "founder"),
    (r"генеральный\s+директор|гендиректор|(?<![а-я])президент", "ceo president"),
    (r"(?<![а-я])директор", "director"),
    (r"руководител", "head manager"),
    (r"управляющий\s+партнер", "managing partner partner"),
    (r"партнер", "partner"),
    (r"(?<![а-я])менеджер", "manager"),
    (r"главный\s+врач", "chief physician director"),
)


def ru_title_aliases(title: str) -> str:
    t = norm_ru(title)
    if not has_cyrillic(t):
        return ""
    return " ".join(alias for pattern, alias in _ALIASES if re.search(pattern, t))


RU_OWNER_FAMILY = frozenset(norm_ru(w) for w in (
    "владелец", "владелица", "собственник", "собственница", "совладелец", "основатель",
    "соучредитель", "учредитель", "генеральный директор", "гендиректор", "директор",
    "руководитель", "президент", "ип", "индивидуальный предприниматель", "управляющий партнер",
    "главный врач", "предприниматель"))

# Words a Russian job title is recognised by (for the profile's card reader).
ROLE_HINT_RU = (r"владел\w+|собственни\w+|совладел\w+|основател\w+|соосновател\w+|учредител\w+|"
                r"соучредител\w+|директор\w*|руководител\w+|президент\w*|управляющ\w+|менеджер\w*|"
                r"заведующ\w+|врач\w*|юрист\w*|адвокат\w*|специалист\w*|партн[её]р\w*|"
                r"предпринимател\w+|бухгалтер\w*|администратор\w*|консультант\w*|координатор\w*|"
                r"представител\w+|начальник\w*|председател\w+|ип")
STRONG_ROLE_RU = (r"владел\w+|собственни\w+|совладел\w+|основател\w+|соосновател\w+|учредител\w+|"
                  r"соучредител\w+|директор\w*|руководител\w+|президент\w*|управляющ\w+|врач\w*|"
                  r"юрист\w*|адвокат\w*|предпринимател\w+|начальник\w*|председател\w+|ип")

# ── sentences that name an owner ─────────────────────────────────────────

_NAME = r"[А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)*"
_NAME_PHRASE = rf"{_NAME}(?:\s+{_NAME}){{1,2}}"
_COMPANY_WORD = r"(?i:компании|организации|клиники|студии|агентства|центра|фирмы|предприятия|бюро|салона)"
_TITLE_PHRASE = (
    r"(?i:генеральный\s+директор|гендиректор|исполнительный\s+директор|управляющий\s+партн[её]р|"
    r"главный\s+врач|директор|руководитель|владел(?:ец|ица)|собственни(?:к|ца)|"
    r"основател(?:ь|ьница)|совладел(?:ец|ица)|со-?учредител(?:ь|ьница)|учредител(?:ь|ьница)|"
    r"президент)(?![а-яёА-ЯЁ])")
_RU_PROSE = (
    # "Генеральный директор: Иванов Иван Петрович", "Основатель компании Иван Петров"
    re.compile(rf"(?P<title>{_TITLE_PHRASE})(?:\s+{_COMPANY_WORD})?\s*[:\-–—]?\s*(?P<name>{_NAME_PHRASE})"),
    # "Иванов Иван Петрович — генеральный директор", "Иван Петров, основатель компании"
    re.compile(rf"(?P<name>{_NAME_PHRASE})\s*[,\-–—]\s*(?:(?i:наш|единственный|действующий)\s+)?"
               rf"(?P<title>{_TITLE_PHRASE})"),
    # "ИП Иванов Иван Петрович", "Индивидуальный предприниматель Петров Пётр Иванович"
    re.compile(rf"(?P<title>\bИП|(?i:индивидуальный\s+предприниматель))\s+(?P<name>{_NAME_PHRASE})"),
)


def ru_prose_people(sentence: str) -> list[tuple[str, str]]:
    """(raw name, title) pairs named in one Russian sentence."""
    out = []
    for regex in _RU_PROSE:
        for m in regex.finditer(sentence):
            title = re.sub(r"\s+", " ", m.group("title")).strip()
            low = norm_ru(title)
            # "коммерческий директор Иванов", "директор по маркетингу Иванов"
            if "директор" in low or "руководитель" in low:
                before = _word_before(sentence, m.start("title"))
                if before in _DIRECTOR_QUALIFIERS:
                    continue
            after = sentence[m.end("title"):m.end("title") + 12] if m.start("title") > m.start("name") else ""
            if re.match(rf"\s+{_FUNCTION_TAIL}\b", norm_ru(after)):
                continue
            title = "Индивидуальный предприниматель" if low in ("ип",) else title
            out.append((m.group("name"), title[:1].upper() + title[1:]))
    return out


# ── "no longer with us" ──────────────────────────────────────────────────

_DEPARTED_RU = (r"(?:ушел\s+из\s+жизни|ушла\s+из\s+жизни|скончал(?:ся|ась)|умер|умерла|"
                r"светлая\s+память|вечная\s+память|на\s+заслуженном\s+отдыхе|вышел\s+на\s+пенсию|"
                r"вышла\s+на\s+пенсию)")


def ru_departed(first: str, last: str, text: str) -> bool:
    """The page remembers this person rather than employs them. ``text`` has
    one sentence per line, so a phrase about one person never reaches the next.
    Names are matched with a short case ending ("Орлову", "Олегом") and never
    inside a longer word ("Сидор" in "Сидорович")."""
    first, last = re.escape(norm_ru(first)), re.escape(norm_ru(last.split()[-1]))
    name = rf"(?<![а-я])(?:{first}|{last})[а-я]{{0,2}}(?![а-я])"
    t = norm_ru(text)
    return bool(re.search(rf"(?:памяти|память)\s+(?:основател\w+\s+)?(?:\w+\s+){{0,2}}?{name}|"
                          rf"{name}[^.\n]{{0,60}}?{_DEPARTED_RU}", t))


# ── which pages to read ──────────────────────────────────────────────────

# (weight, pattern): the same scale as people._LINK_WEIGHTS. Latin spellings
# are for URLs ("/komanda", "/o-kompanii"), Cyrillic for menu labels and
# percent-encoded paths.
LINK_WEIGHTS_RU = (
    (3, r"komanda|kollektiv|rukovodstvo|rukovoditel\w*|sotrudniki|specialisty|vrachi|"
        r"yuristy|advokaty|uchrediteli|nashi-?vrachi|nasha-?komanda|"
        r"команда|коллектив|руководств\w+|руководител\w+|сотрудник\w+|специалист\w+|врач\w+|"
        r"юрист\w+|адвокат\w+|учредител\w+|основател\w+"),
    (2, r"o-?kompanii|o-?nas|o-?firme|o-?centre|o-?studii|kompaniya|about-?company|istoriya|"
        r"о\s+компании|о\s+нас|о\s+фирме|о\s+центре|о\s+студии|компания|история|о\s+клинике"),
    (1, r"kontakty|kontakt|контакты|контакт\w*"),
)
RU_FALLBACK_PATHS = ("/o-kompanii", "/o-nas", "/komanda", "/rukovodstvo", "/kontakty")
RU_SKIP_LINK = r"novosti|stati|vakansii|otzyvy|akcii|aktsii|blog|publikacii"
