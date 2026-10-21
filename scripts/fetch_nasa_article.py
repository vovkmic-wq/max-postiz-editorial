"""Copy the text of one owner-selected NASA SVS article; never publish or download media."""
import argparse
import json
import re
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

from max_postiz_agent.collectors import clean_html
from max_postiz_agent.config import AgentError


def _narrative_href(href, base):
    error = "ARTICLE_SOURCE_LINK_INVALID"
    try:
        if (not isinstance(href, str) or not href
                or re.search(r"[\x00-\x20\x7f\\<>\"\[\]]", href)
                or re.search(r"%(?![0-9A-Fa-f]{2})", href)):
            raise ValueError
        parsed = urlsplit(href)
        if (parsed.scheme or href.startswith("//")) and not parsed.hostname:
            raise ValueError
        # Decode only for rejection checks; retain the literal accepted URL.
        # Schemes/authority must not emerge or change after percent decoding.
        decoded = href
        for _ in range(4):
            probe = urlsplit(decoded)
            if (probe.scheme and probe.scheme.lower() not in {"http", "https"}
                    or probe.username is not None or probe.password is not None
                    or probe.scheme != parsed.scheme or probe.netloc != parsed.netloc
                    or re.search(r"[\x00-\x1f\x7f\\]", decoded)):
                raise ValueError
            newer = unquote(decoded, errors="strict")
            if newer == decoded:
                break
            decoded = newer
        else:
            raise ValueError
        absolute = urljoin(base, href)
        target = urlsplit(absolute)
        if (target.scheme not in {"http", "https"} or not target.hostname
                or target.username is not None or target.password is not None
                or "%" in target.netloc or target.port is not None and not 1 <= target.port <= 65535):
            raise ValueError
        # article-links-v1 supports balanced parentheses in plain destinations.
        depth = 0
        for char in absolute:
            depth += (char == "(") - (char == ")")
            if depth < 0:
                raise ValueError
        if depth:
            raise ValueError
        return absolute
    except (ValueError, UnicodeError, TypeError) as exc:
        raise AgentError(error) from exc


class _NarrativeAnchors(HTMLParser):
    def __init__(self, base):
        super().__init__(convert_charrefs=True)
        self.base, self.hrefs, self.depth = base, [], 0

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        hrefs = [value for name, value in attrs if name == "href"]
        if self.depth or len(hrefs) != 1:
            raise AgentError("ARTICLE_SOURCE_LINK_INVALID")
        self.depth += 1
        self.hrefs.append(_narrative_href(hrefs[0], self.base))

    def handle_endtag(self, tag):
        if tag == "a":
            if not self.depth:
                raise AgentError("ARTICLE_SOURCE_LINK_INVALID")
            self.depth = 0

    def close(self):
        super().close()
        if self.depth:
            raise AgentError("ARTICLE_SOURCE_LINK_INVALID")


def narrative_text(section, base):
    """Retain visible anchor words and their targets without fetching the links."""
    validator = _NarrativeAnchors(base)
    validator.feed(section)
    validator.close()
    soup = BeautifulSoup(section, "html.parser")
    anchors = soup.find_all("a")
    if len(anchors) != len(validator.hrefs):
        raise AgentError("ARTICLE_SOURCE_LINK_INVALID")
    if not anchors:
        return clean_html(section)
    for anchor, href in zip(anchors, validator.hrefs, strict=True):
        label = clean_html(str(anchor))
        if not label or re.search(r"[\[\]]", label):
            raise AgentError("ARTICLE_SOURCE_LINK_LABEL_INVALID")
        anchor.replace_with(f"[{label}]({href})")
    return clean_html(str(soup))


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
    # Both are dedicated narrative widgets in the documented SVS Page API.
    # Descriptions attached to media players/images remain captions, not articles.
    narrative_widgets = {"Basic text", "Basic text with HTML"}
    sections = [g.get("description", "") for g in data.get("media_groups", [])
                if g.get("widget") in narrative_widgets and g.get("description")]
    if not sections:
        raise AgentError("ARTICLE_SOURCE_NARRATIVE_REQUIRED")
    original = "\n\n".join(narrative_text(section, canonical) for section in sections)
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
