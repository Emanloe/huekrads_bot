import pytest

import text_resources


def test_hyperborean_yaml_loads_utf8_text_with_html_emoji_and_multiline():
    text_resources._load_resources.cache_clear()

    text = text_resources.get_text(
        "hyperborean.other.exploded.hyperboreic",
        title="Гном Вася",
    )

    assert "Гном Вася" in text
    assert "🍆" in text
    assert "💀" in text
    assert "<b>Гном Вася</b>" in text
    assert "\n\nУ гнома уже не было хуя" in text


def test_text_resource_missing_key_and_placeholder_have_clear_errors():
    with pytest.raises(KeyError, match="Unknown text key"):
        text_resources.get_text("hyperborean.no.such.key")

    with pytest.raises(KeyError, match="Missing placeholder 'title'"):
        text_resources.get_text("hyperborean.other.exploded.arthur")


def test_text_list_preserves_duel_catalog_order_and_unicode():
    phrases = text_resources.get_text_list("duel.phrases.attack")

    assert phrases[0] == "замахивается засапожным свинорезом"
    assert phrases[-1] == "выкидывает грязный финт короткой гномьей заточкой"
    assert text_resources.get_text_list("duel.round_flavor.one")[0].startswith("⚡")


def test_text_list_reports_unknown_key_and_wrong_type():
    with pytest.raises(KeyError, match="Unknown text key"):
        text_resources.get_text_list("duel.phrases.unknown")

    with pytest.raises(TypeError, match="list of strings"):
        text_resources.get_text_list("duel.targets")


async def test_hyperborean_handler_uses_text_resource_layer(monkeypatch, fake_context):
    from handlers import hyperborean_event as event

    event.ACTIVE_HYPERBOREAN_EVENTS.clear()
    calls = []

    def get_resource(key, **_placeholders):
        calls.append(key)
        return key

    monkeypatch.setattr(event, "get_text", get_resource)
    monkeypatch.setattr(event.random, "random", lambda: 0.0)
    monkeypatch.setattr(event.random, "choice", lambda _values: "hyperboreic")

    await event._spawn_hyperboreic_huy(fake_context, -1)

    assert calls == [
        "hyperborean.spawn.hyperboreic",
        "hyperborean.buttons.self",
        "hyperborean.buttons.other",
    ]
