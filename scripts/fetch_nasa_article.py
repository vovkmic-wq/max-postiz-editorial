"""Copy one owner-selected NASA SVS article with the packaged, unchanged extractor."""

import httpx as httpx

from max_postiz_agent.nasa_extractor import (
    _NarrativeAnchors as _NarrativeAnchors,
    _narrative_href as _narrative_href,
    clean_html as clean_html,
    fetch_article as fetch_article,
    main as main,
    narrative_text as narrative_text,
)


if __name__ == "__main__":
    main()
