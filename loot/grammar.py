"""Conservative case forms for weapon names assembled by the loot catalog."""

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


def _adjective(word: str, form: str) -> tuple[str, str] | None:
    """Instrumental and accusative for a known inanimate nominal modifier."""
    if word.endswith("ся"):
        result = _adjective(word[:-2], form)
        return tuple(value + "ся" for value in result) if result else None
    if form == "m":
        possessives = {"лисий": "лисьим", "охотничий": "охотничьим",
                       "олений": "оленьим", "девичий": "девичьим"}
        if word in possessives:
            return possessives[word], word
        if word.endswith(("ый", "ой")):
            ending = "им" if word[-3] in "гкхжчшщ" else "ым"
            return word[:-2] + ending, word
        if word.endswith("ий"):
            return word[:-2] + "им", word
    elif form == "f":
        for ending, instrumental, accusative in (
            ("ая", "ой", "ую"), ("яя", "ей", "юю"), ("ья", "ьей", "ью"),
        ):
            if word.endswith(ending):
                if ending == "ая" and word[-3] in "жчшщ" and word not in ("большая", "чужая"):
                    instrumental = "ей"
                return word[:-len(ending)] + instrumental, word[:-len(ending)] + accusative
    elif form == "n":
        for ending, instrumental in (("ое", "ым"), ("ее", "им")):
            if word.endswith(ending):
                stem = word[:-2]
                if stem[-1:] in "гкхжчшщ":
                    instrumental = "им"
                return stem + instrumental, word
        if word.endswith("ье"):
            return word[:-2] + "ьим", word
    return None


def _modifier(item: dict, form: str) -> tuple[str, str] | None:
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
def _modifiers(form: str) -> dict[str, tuple[str, str]]:
    return {item[form].lower(): forms for item in (*QUALITIES, *ADJECTIVES)
            if (forms := _modifier(item, form)) is not None}


def _prefix_forms(prefix: str, form: str, remaining: int = 3) -> set[tuple[str, str]]:
    if not prefix:
        return {("", "")}
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


def _base_forms(base: dict) -> tuple[str, str] | None:
    words = base["name"].split()
    noun = _NOUNS.get(words[-1])
    if noun is None:
        return None
    modifiers = [_adjective(word, base["list"]) for word in words[:-1]]
    if any(value is None for value in modifiers):
        return None
    return tuple(" ".join([value[index] for value in modifiers] + [noun[index]])
                 for index in range(2))


@lru_cache(maxsize=1024)
def weapon_case_forms(name: str | None) -> dict[str, str]:
    """Inflect a fully recognized name; otherwise preserve it in neutral quotes."""
    if not name:
        name = "нож"
    lower = name.lower()
    if lower in _NOUNS:
        instrumental, accusative = _NOUNS[lower]
        return {"weapon_instrumental": instrumental, "weapon_accusative": accusative}
    results = set()
    # Sources are genitive dependencies, not part of the declinable noun phrase.
    suffixes = [""] + [" " + source["name"] for source in SOURCES
                       if lower.endswith(" " + source["name"].lower())]
    for suffix in suffixes:
        without_source = lower[:-len(suffix)] if suffix else lower
        original_suffix = name[-len(suffix):] if suffix else ""
        for base in _WEAPON_BASES:
            base_name = base["name"]
            if without_source == base_name:
                prefix = ""
            elif without_source.endswith(" " + base_name):
                prefix = without_source[:-len(base_name) - 1]
            else:
                continue
            base_forms = _base_forms(base)
            if base_forms is None:
                continue
            for modifiers in _prefix_forms(prefix, base["list"]):
                results.add(tuple((modifier + " " if modifier else "") + head + original_suffix
                                  for modifier, head in zip(modifiers, base_forms)))
    if len(results) == 1:
        instrumental, accusative = results.pop()
    else:
        instrumental, accusative = f"оружием «{name}»", f"оружие «{name}»"
    return {"weapon_instrumental": instrumental, "weapon_accusative": accusative}
