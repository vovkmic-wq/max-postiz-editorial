"""ECMWF image credits are explicit metadata, never an inferred licence."""

import pytest
from max_postiz_agent.source_adapters.science_082 import extract

URL = "https://www.ecmwf.int/en/about/media-centre/news/2026/example"


def page(credit):
    return (
        '<h1 class="page-title">Wildfires</h1><article><div class="field--name-body"><p>'
        + "Scientists measured wildfire emissions. " * 10
        + "</p>"
        + credit
        + "</div></article>"
    ).encode()


def test_exact_top_image_credit_is_preserved_without_granting_rights():
    result = extract(page("<p><em>Top image credit: State of Wildfire 2025–2026</em></p>"), URL)
    assert result["credits"] == ["State of Wildfire 2025–2026"]
    assert "IMAGE_CREDIT_REQUIRED" not in result["blockers"]
    assert "EXACT_TEXT_AND_IMAGE_RIGHTS_ADMISSION_REQUIRED" in result["blockers"]
    assert "NATIVE_IMAGE_PROOF_REQUIRED" in result["blockers"]
    assert not result["automatic_publication_enabled"]


@pytest.mark.parametrize(
    "credit",
    [
        "<p><em>Top image credit:</em></p>",
        "<p>About Top image credit: Someone</p>",
        "<p><em>Top image credit: First</em></p><p><em>Top image credit: Second</em></p>",
        "<p><em>Top image credit: Same</em></p>" * 2,
    ],
)
def test_empty_or_ambiguous_credit_stays_blocked(credit):
    result = extract(page(credit), URL)
    assert "IMAGE_CREDIT_REQUIRED" in result["blockers"]
    assert not result["automatic_publication_enabled"]


def test_conflicting_figcaption_is_retained_and_stays_blocked():
    result = extract(
        page("<figcaption>Other author</figcaption><p>Top image credit: Report</p>"), URL
    )
    assert result["credits"] == ["Other author", "Report"]
    assert "IMAGE_CREDIT_REQUIRED" in result["blockers"]
