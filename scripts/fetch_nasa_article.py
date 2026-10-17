"""Copy the text of one owner-selected NASA SVS article; never publish or download media."""
import argparse
import json
import re
from datetime import datetime
from pathlib import Path

import httpx

from max_postiz_agent.collectors import clean_html
from max_postiz_agent.config import AgentError


def fetch_article(url):
    match = re.fullmatch(r"https://svs\.gsfc\.nasa\.gov/([1-9][0-9]{0,6})/?", url)
    if not match:
        raise AgentError("ARTICLE_SOURCE_URL_NOT_ALLOWED")
    canonical = f"https://svs.gsfc.nasa.gov/{match[1]}/"
    with httpx.Client(timeout=30, trust_env=False, follow_redirects=False) as client:
        with client.stream("GET", f"https://svs.gsfc.nasa.gov/api/{match[1]}/") as response:
            if response.status_code != 200:
                raise AgentError("ARTICLE_SOURCE_UNAVAILABLE")
            raw = bytearray()
            for chunk in response.iter_bytes():
                raw.extend(chunk)
                if len(raw) > 2 * 1024 * 1024:
                    raise AgentError("ARTICLE_SOURCE_TOO_LARGE")
    data = json.loads(raw)
    if (data.get("page_type") != "Visualization"
            or data.get("url") not in {canonical, canonical.replace("https://", "http://", 1)}):
        raise AgentError("ARTICLE_SOURCE_METADATA_INVALID")
    title = clean_html(data["title"])
    sections = [g.get("description", "") for g in data.get("media_groups", [])
                if g.get("widget") == "Basic text" and g.get("description")]
    if not sections:
        raise AgentError("ARTICLE_SOURCE_NARRATIVE_REQUIRED")
    original = "\n\n".join(clean_html(section) for section in sections)
    if not 100 <= len(original) <= 7500 or not title:
        raise AgentError("ARTICLE_SOURCE_LENGTH_INVALID")
    # A reviewer must inspect the original page and its rights/credits before this call.
    # These obvious warnings additionally prevent automatic reuse of known exceptions.
    if re.search(r"copyright|all rights reserved|©|licensed music|music:", original, re.I):
        raise AgentError("ARTICLE_SOURCE_RIGHTS_REVIEW_REQUIRED")
    published = datetime.fromisoformat(data["release_date"])
    if published.tzinfo is None:
        raise AgentError("ARTICLE_SOURCE_DATE_INVALID")
    return {"url": canonical, "title": title, "original": original,
            "published_at": published.timestamp()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = fetch_article(args.url)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        args.output.chmod(0o600)
        print(json.dumps({"url": result["url"], "title": result["title"],
                          "chars": len(result["original"]), "state": "copied", "external_posts": 0}))
    except (AgentError, httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({"code": exc.code if isinstance(exc, AgentError) else "ARTICLE_FETCH_FAILED"}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
