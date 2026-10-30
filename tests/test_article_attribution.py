"""Новые лицензии обязательны; прежний NASA footer и хешируемый текст неизменны."""
from types import SimpleNamespace

import pytest

from max_postiz_agent.article_attribution import CC_BY_URL, article_footer
from max_postiz_agent.config import AgentError
from max_postiz_agent.human_review import candidate_body


def test_legacy_nasa_footer_is_byte_for_byte_unchanged():
    url = "https://svs.gsfc.nasa.gov/5682/"
    assert article_footer(url) == "Источник: " + url + "\nПеревод и редактура с помощью нейросетей."


@pytest.mark.parametrize("url,credit", [
    ("https://noirlab.edu/public/images/iotw2642a/", "NOIRLab"),
    ("https://esahubble.org/images/potw2552a/", "ESA/Hubble"),
])
@pytest.mark.parametrize("separator", ["\n", "\n\n"])
def test_exact_license_and_translation_notice_are_outside_reviewed_candidate(url, credit, separator):
    footer = article_footer(url)
    assert "Текст: " + credit + "." in footer and CC_BY_URL in footer
    assert "Машинный перевод на русский и автоматическая редактура." in footer
    assert "Архивный научный материал." in footer
    variant = SimpleNamespace(title="Научное открытие", body="Научное открытие" + separator
                              + "Полный проверенный перевод." + separator + footer)
    assert candidate_body(variant, {"url": url}) == "Полный проверенный перевод."


@pytest.mark.parametrize("tamper", ["missing", "legacy", "credit", "license", "changes"])
def test_missing_or_altered_attribution_never_uses_legacy_fallback(tamper):
    url = "https://esahubble.org/images/potw2552a/"
    footer = article_footer(url)
    if tamper == "missing":
        footer = ""
    elif tamper == "legacy":
        footer = "Источник: " + url + "\nПеревод и редактура с помощью нейросетей."
    elif tamper == "credit":
        footer = footer.replace("ESA/Hubble", "NASA")
    elif tamper == "license":
        footer = footer.replace(CC_BY_URL, "https://example.invalid/license")
    else:
        footer = footer.replace("Машинный перевод на русский и автоматическая редактура.", "")
    variant = SimpleNamespace(title="Заголовок", body="Заголовок\n\nПеревод\n\n" + footer)
    with pytest.raises(AgentError, match="ARTICLE_ATTRIBUTION_REQUIRED"):
        candidate_body(variant, {"url": url})
