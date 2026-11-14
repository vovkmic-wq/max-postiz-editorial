"""Webb release metadata must not use observation dates or other table rows."""

import pytest
from max_postiz_agent.source_adapters.science_005 import extract

URL = "https://esawebb.org/images/potm2610a/"


def page(rows):
    return (
        '<div class="left-column"><h1 class="my-3">A stellar cloud</h1><p>'
        + "Scientists observed a cloud of stars. " * 12
        + '</p><div class="credit">ESA/Webb</div></div><table>'
        + rows
        + "</table>"
    ).encode()


def test_webb_extracts_exact_release_date_not_observation_date():
    result = extract(
        page(
            "<tr><th>Observation date:</th><td>1999</td></tr>"
            "<tr><th>Release date:</th><td>22 October 2026, 10:00</td></tr>"
        ),
        URL,
    )
    assert result["date_literals"] == ["22 October 2026, 10:00"]
    assert "PUBLICATION_DATE_MISSING_OR_AMBIGUOUS" not in result["blockers"]
    assert not result["automatic_publication_enabled"]


@pytest.mark.parametrize(
    "rows",
    [
        "",
        "<tr><th>Previous Release date:</th><td>1999</td></tr>",
        "<tr><th>Release date:</th><td></td></tr>",
        "<tr><th>Release date:</th><td>22 October 2026, 10:00</td></tr>" * 2,
    ],
)
def test_webb_missing_or_duplicate_release_metadata_stays_blocked(rows):
    assert "PUBLICATION_DATE_MISSING_OR_AMBIGUOUS" in extract(page(rows), URL)["blockers"]


@pytest.mark.parametrize(
    "image,accepted",
    [
        ("https://cdn.esawebb.org/archives/images/publicationjpg/potm2610a.jpg", True),
        ("https://cdn.esawebb.org/archives/images/publicationjpg/potm2609a.jpg", False),
        (
            "https://cdn.esawebb.org.evil.example/archives/images/publicationjpg/potm2610a.jpg",
            False,
        ),
        ("https://cdn.esawebb.org/archives/images/publicationjpg/potm2610a.jpg?token=x", False),
    ],
)
def test_webb_publication_image_must_belong_to_exact_article(image, accepted):
    raw = page("") + f'<a href="{image}">Publication JPEG</a>'.encode()
    result = extract(raw, URL)
    assert result["image_candidates"] == ([image] if accepted else [])
    assert ("SOURCE_IMAGE_CANDIDATE_REQUIRED" not in result["blockers"]) == accepted
    assert "NATIVE_IMAGE_PROOF_REQUIRED" in result["blockers"]
    assert "EXACT_TEXT_AND_IMAGE_RIGHTS_ADMISSION_REQUIRED" in result["blockers"]
