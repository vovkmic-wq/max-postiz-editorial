"""Article profiles must not collect the site's navigation heading."""

import json
from pathlib import Path

import pytest

from max_postiz_agent.collectors import collect_web


@pytest.mark.parametrize(
    ("source_id", "markup"),
    [
        ("015", '<div id="fixture_Panel_Title"><h1>{title}</h1></div>'
         '<div id="fixture_Panel_odyText"><p>{body}</p></div>'),
        ("019", '<div class="docs"><div class="titles"><h3>{title}</h3></div>'
         '<p>{body}</p></div>'),
        ("022", '<section class="maincontent"><h2>{title}</h2><p>{body}</p></section>'),
        ("036", '<div class="post-banner"><h1>{title}</h1></div>'
         '<div class="article"><p>{body}</p></div>'),
    ],
)
def test_article_profile_ignores_global_header(source_id, markup):
    root = Path(__file__).resolve().parents[1]
    catalog = json.loads((root / "config/editorial_source_catalog.json").read_text(encoding="utf-8"))
    entry = next(item for item in catalog["sources"] if item["id"] == "science_" + source_id)
    title = "A specific scientific discovery with a unique article title"
    body = "The experiment measured the same physical quantity independently. " * 12
    page = '<html><h1>Generic institution heading</h1>' + markup.format(title=title, body=body) + '</html>'

    class CapturedHTTP:
        def get(self, url, headers):
            assert url == entry["sample_url"]
            return 200, page, {}, url

    items, _ = collect_web(
        {"type": "html", "endpoint": entry["sample_url"], "full_text": True,
         "language_hint": "en", "max_items_per_run": 1,
         "extraction_profile": entry["extraction_profile"]},
        {}, CapturedHTTP(), root,
    )
    assert len(items) == 1
    assert items[0].title == title
    assert "Generic institution heading" not in items[0].text
    assert body.strip() in items[0].text
    assert entry["automatic_publication_enabled"] is False
