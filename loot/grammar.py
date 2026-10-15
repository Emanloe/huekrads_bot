"""Conservative case forms for equipment names assembled by the loot catalog."""

from functools import lru_cache

from loot.catalog import ADJECTIVES, BASES, QUALITIES, SOURCES


# Explicit inanimate noun forms. Unfamiliar/borrowed bases use a quoted name.
_NOUNS = {
    "нож": ("ножом", "нож"), "меч": ("мечом", "меч"),
    "топор": ("топором", "топор"), "копьё": ("копьём", "копьё"),
    "булава": ("булавой", "булаву"), "тесак": ("тесаком", "тесак"),
    "кирка": ("киркой", "кирку"), "палочка": ("палочкой", "палочку"),
    "дубина": ("дубиной", "дубину"), "скипетр": ("скипетром", "скипетр"),
    "звезда": ("звездой", "звезду"), "цеп": ("цепом", "цеп"),
    "молот": ("молотом", "молот"), "кувалда": ("кувалдой", "кувалду"),
    "ятаган": ("ятаганом", "ятаган"), "сабля": ("саблей", "саблю"),
    "фальшион": ("фальшионом", "фальшион"), "клеймор": ("клеймором", "клеймор"),
    "кинжал": ("кинжалом", "кинжал"), "кортик": ("кортиком", "кортик"),
    "крис": ("крисом", "крис"), "лезвие": ("лезвием", "лезвие"),
    "дротик": ("дротиком", "дротик"), "пилум": ("пилумом", "пилум"),
    "глефа": ("глефой", "глефу"), "трезубец": ("трезубцем", "трезубец"),
    "пика": ("пикой", "пику"), "бердыш": ("бердышом", "бердыш"),
    "коса": ("косой", "косу"), "полутопор": ("полутопором", "полутопор"),
    "алебарда": ("алебардой", "алебарду"), "посох": ("посохом", "посох"),
    "лук": ("луком", "лук"), "арбалет": ("арбалетом", "арбалет"),
    "топорик": ("топориком", "топорик"), "дубинка": ("дубинкой", "дубинку"),
    "гарпун": ("гарпуном", "гарпун"), "кадуцей": ("кадуцеем", "кадуцей"),
    "клюв": ("клювом", "клюв"), "рондель": ("ронделем", "рондель"),
    "томахавк": ("томахавком", "томахавк"),
    "полумесяц": ("полумесяцем", "полумесяц"),
    "молотилка": ("молотилкой", "молотилку"), "фалката": ("фалкатой", "фалкату"),
}
_WEAPON_BASES = tuple(base for base in BASES if base["kind"] == "оружие") + (
    {"name": "нож", "list": "m"},
)
_ARMOR_NOUNS = {
    "шлем": ("шлемом", "шлем", "шлеме"),
    "нагрудник": ("нагрудником", "нагрудник", "нагруднике"),
    "жилетка": ("жилеткой", "жилетку", "жилетке"),
    "жилет": ("жилетом", "жилет", "жилете"),
    "рубаха": ("рубахой", "рубаху", "рубахе"),
    "броня": ("бронёй", "броню", "броне"),
    "шапка": ("шапкой", "шапку", "шапке"),
    "каска": ("каской", "каску", "каске"),
    "шляпа": ("шляпой", "шляпу", "шляпе"),
    "колпак": ("колпаком", "колпак", "колпаке"),
    "корона": ("короной", "корону", "короне"),
    "маска": ("маской", "маску", "маске"),
    "гульфик": ("гульфиком", "гульфик", "гульфике"),
    "гульфище": ("гульфищем", "гульфище", "гульфище"),
    "щит": ("щитом", "щит", "щите"),
    "накладка": ("накладкой", "накладку", "накладке"),
    "панцирь": ("панцирем", "панцирь", "панцире"),
    "раковина": ("раковиной", "раковину", "раковине"),
    "забрало": ("забралом", "забрало", "забрале"),
    "футляр": ("футляром", "футляр", "футляре"),
    "купол": ("куполом", "купол", "куполе"),
    "протектор": ("протектором", "протектор", "протекторе"),
    "суспензорий": ("суспензорием", "суспензорий", "суспензории"),
    "латы": ("латами", "латы", "латах"),
    "трусы": ("трусами", "трусы", "трусах"),
    "плавки": ("плавками", "плавки", "плавках"),
    "кальсоны": ("кальсонами", "кальсоны", "кальсонах"),
    "подгузники": ("подгузниками", "подгузники", "подгузниках"),
    "рога": ("рогами", "рога", "рогах"),
    "перья": ("перьями", "перья", "перьях"),
}
# Include legacy clothing names as well as current generator bases. This only
# supplies wording; the equipped slot, never the catalog kind, grants protection.
_ARMOR_BASES = tuple(base for base in BASES if base["kind"] in (
    "головной убор", "одежда", "верхняя одежда", "пах",
)) + tuple({"name": name, "list": form} for name, form in (
    ("нагрудник", "m"), ("жилетка", "f"), ("промасленная жилетка", "f"),
    ("жилет", "m"), ("рубаха", "f"), ("шапка", "f"), ("гульфище", "n"),
))


def _adjective(word: str, form: str) -> tuple[str, str, str] | None:
    """Instrumental, accusative, prepositional of a known inanimate modifier."""
    if word.endswith("ся"):
        result = _adjective(word[:-2], form)
        return tuple(value + "ся" for value in result) if result else None
    if form == "m":
        possessives = {"лисий": ("лисьим", "лисьем"),
                       "охотничий": ("охотничьим", "охотничьем"),
                       "олений": ("оленьим", "оленьем"), "девичий": ("девичьим", "девичьем")}
        if word in possessives:
            instrumental, prepositional = possessives[word]
            return instrumental, word, prepositional
        if word.endswith(("ый", "ой")):
            ending = "им" if word[-3] in "гкхжчшщ" else "ым"
            return word[:-2] + ending, word, word[:-2] + "ом"
        if word.endswith("ий"):
            prepositional = "ом" if word[-3] in "гкх" else "ем"
            return word[:-2] + "им", word, word[:-2] + prepositional
    elif form == "f":
        for ending, instrumental, accusative in (
            ("ая", "ой", "ую"), ("яя", "ей", "юю"), ("ья", "ьей", "ью"),
        ):
            if word.endswith(ending):
                if ending == "ая" and word[-3] in "жчшщ" and word not in ("большая", "чужая"):
                    instrumental = "ей"
                stem = word[:-len(ending)]
                return stem + instrumental, stem + accusative, stem + instrumental
    elif form == "n":
        for ending, instrumental in (("ое", "ым"), ("ее", "им")):
            if word.endswith(ending):
                stem = word[:-2]
                if stem[-1:] in "гкхжчшщ":
                    instrumental = "им"
                return stem + instrumental, word, stem + ("ом" if ending == "ое" else "ем")
        if word.endswith("ье"):
            return word[:-2] + "ьим", word, word[:-2] + "ьем"
    elif form == "pl":
        for ending, instrumental, prepositional in (("ые", "ыми", "ых"), ("ие", "ими", "их")):
            if word.endswith(ending):
                return word[:-2] + instrumental, word, word[:-2] + prepositional
        if word.endswith("ьи"):
            return word[:-1] + "ими", word, word[:-1] + "их"
    return None


def _modifier(item: dict, form: str) -> tuple[str, str, str] | None:
    # Gender variants identify the head of a phrase such as
    # "бережно хранимый с 90-х"; its dependent words must stay unchanged.
    words = item[form].lower().split()
    masculine = item["m"].lower().split()
    feminine = item["f"].lower().split()
    if len(words) != len(masculine) or len(words) != len(feminine):
        return None
    heads = [index for index, pair in enumerate(zip(masculine, feminine))
             if pair[0] != pair[1]]
    if len(heads) != 1:
        return None
    index = heads[0]
    forms = _adjective(words[index], form)
    if forms is None:
        return None
    return tuple(" ".join(words[:index] + [value] + words[index + 1:]) for value in forms)


@lru_cache(maxsize=4)
def _modifiers(form: str) -> dict[str, tuple[str, str, str]]:
    return {item[form].lower(): forms for item in (*QUALITIES, *ADJECTIVES)
            if (forms := _modifier(item, form)) is not None}


def _prefix_forms(prefix: str, form: str, remaining: int = 3) -> set[tuple[str, str, str]]:
    if not prefix:
        return {("", "", "")}
    if remaining == 0:
        return set()
    results = set()
    for phrase, forms in _modifiers(form).items():
        if prefix == phrase:
            results.add(forms)
        elif prefix.startswith(phrase + " "):
            for rest in _prefix_forms(prefix[len(phrase) + 1:], form, remaining - 1):
                results.add(tuple(left + " " + right for left, right in zip(forms, rest)))
    return results


def _base_forms(base: dict, nouns: dict) -> tuple[str, ...] | None:
    head, separator, label = base["name"].partition(" «")
    words = head.split()
    noun = nouns.get(words[-1])
    if noun is None:
        return None
    modifiers = [_adjective(word, base["list"]) for word in words[:-1]]
    if any(value is None for value in modifiers):
        return None
    return tuple(" ".join([value[index] for value in modifiers] + [noun[index]]) + separator + label
                 for index in range(len(noun)))


def _item_case_forms(name: str, bases: tuple, nouns: dict) -> tuple[str, ...] | None:
    """Parse whole catalog components, leaving dependent sources and labels intact."""
    lower = name.lower()
    if lower in nouns:
        return nouns[lower]
    results = set()
    # Sources are genitive dependencies, not part of the declinable noun phrase.
    suffixes = [""] + [" " + source["name"] for source in SOURCES
                       if lower.endswith(" " + source["name"].lower())]
    for suffix in suffixes:
        without_source = lower[:-len(suffix)] if suffix else lower
        original_suffix = name[-len(suffix):] if suffix else ""
        for base in bases:
            base_name = base["name"].lower()
            if without_source == base_name:
                prefix = ""
            elif without_source.endswith(" " + base_name):
                prefix = without_source[:-len(base_name) - 1]
            else:
                continue
            base_forms = _base_forms(base, nouns)
            if base_forms is None:
                continue
            for modifiers in _prefix_forms(prefix, base["list"]):
                results.add(tuple((modifier + " " if modifier else "") + head + original_suffix
                                  for modifier, head in zip(modifiers, base_forms)))
    if len(results) == 1:
        return results.pop()
    return None


@lru_cache(maxsize=1024)
def weapon_case_forms(name: str | None) -> dict[str, str]:
    """Inflect a fully recognized weapon or preserve it in neutral quotes."""
    name = name or "нож"
    forms = _item_case_forms(name, _WEAPON_BASES, _NOUNS)
    if forms is None:
        instrumental, accusative = f"оружием «{name}»", f"оружие «{name}»"
    else:
        instrumental, accusative = forms
    return {"weapon_instrumental": instrumental, "weapon_accusative": accusative}


@lru_cache(maxsize=1024)
def armor_case_forms(name: str | None) -> dict[str, str]:
    """Armor wording only: the default vest never supplies combat protection."""
    name = name or "промасленная жилетка"
    forms = _item_case_forms(name, _ARMOR_BASES, _ARMOR_NOUNS)
    if forms is None:
        nominative = accusative = f"предмет «{name}»"
        instrumental, prepositional = f"предметом «{name}»", f"предмете «{name}»"
    else:
        nominative = name
        instrumental, accusative, prepositional = forms
    return {"armor_nominative": nominative, "armor_instrumental": instrumental,
            "armor_accusative": accusative, "armor_prepositional": prepositional}
