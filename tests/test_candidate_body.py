"""Only exact staged attribution boundaries are excluded from review segments."""
from types import SimpleNamespace

import pytest

from max_postiz_agent.editorial import numbers, translation_gate
from max_postiz_agent.human_review import candidate_body

URL = "https://svs.gsfc.nasa.gov/5591/"
TITLE = "Изменение высоты ледяного покрова"
BODY = "Спутник ICESat-2 измеряет высоту поверхности Земли с 2020 года."
FOOTER = "Источник: " + URL + "\nПеревод и редактура с помощью нейросетей."


def extract(body, title=TITLE):
    return candidate_body(SimpleNamespace(title=title, body=body), {"url": URL})


@pytest.mark.parametrize("title_separator", ["\n", "\n\n"])
@pytest.mark.parametrize("footer_separator", ["\n", "\n\n"])
def test_postiz_and_original_paragraph_boundaries_extract_the_same_segment(title_separator, footer_separator):
    staged = TITLE + title_separator + BODY + footer_separator + FOOTER
    assert extract(staged) == BODY
    # The source attribution ID is excluded; article facts keep the same strict gate.
    assert numbers(extract(staged)) == {"2", "2020"}
    translation_gate("ICESat-2 has measured Earth's surface since 2020.", extract(staged))


@pytest.mark.parametrize("separator", ["\n", "\n\n"])
def test_footer_does_not_require_a_repeated_title(separator):
    assert extract(BODY + separator + FOOTER) == BODY


def test_existing_two_paragraph_legacy_footer_remains_supported():
    assert extract(BODY + "\n\nИсточник: " + URL) == BODY


@pytest.mark.parametrize("footer", [
    "Источник: " + URL,
    "Источник: https://svs.gsfc.nasa.gov/5663/\nПеревод и редактура с помощью нейросетей.",
    "Источник: " + URL + "\nПеревод и редактура с помощью человека.",
    FOOTER + "\n",
    FOOTER + "\nЭто часть исходного рассказа.",
    "\n\n" + FOOTER,
])
def test_noncanonical_or_nonterminal_attribution_is_not_removed(footer):
    staged = BODY + "\n" + footer
    assert extract(staged) == staged


def test_service_attribution_in_the_middle_is_preserved():
    staged = BODY + "\n" + FOOTER + "\n\n" + BODY
    assert extract(staged) == staged


def test_similar_title_and_content_numbers_are_preserved():
    staged = TITLE + ": дополнение\n" + BODY + " В записи 5591 есть ещё одно измерение.\n" + FOOTER
    result = extract(staged)
    assert result == TITLE + ": дополнение\n" + BODY + " В записи 5591 есть ещё одно измерение."
    assert "5591" in numbers(result)


def test_embedded_reference_to_same_source_is_preserved():
    segment = BODY + "\nПодробности: " + URL
    assert extract(segment + "\n" + FOOTER) == segment


def test_three_title_line_breaks_are_not_treated_as_a_service_boundary():
    staged = TITLE + "\n\n\n" + BODY
    assert extract(staged) == staged
