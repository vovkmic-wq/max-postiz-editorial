"""Bounded local translation and independent checks for supplied English articles.

This module does not fetch an article or establish permission to republish it.
The caller must validate the source and its rights before passing its text here.
"""

import ipaddress
import json
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit

import httpx

from .adapters import extract_object
from .collectors import detect_language
from .config import AgentError
from .editorial import translation_gate
from .network import resemblances_ip

SPELLING_URL = "http://127.0.0.1:8010/v2/check"
TOPICS = {"science", "stories", "humor"}


def canonical_article_url(value):
    """Validate a public HTTPS reference without resolving or requesting its host."""
    try:
        if (not isinstance(value, str) or not value.strip() or len(value) > 2000
                or re.search(r"[\x00-\x20\x7f\\]", value.strip())):
            raise ValueError
        parsed = urlsplit(value.strip())
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                or parsed.password or parsed.port not in {None, 443}):
            raise ValueError
        host = parsed.hostname.rstrip(".").encode("idna").decode("ascii").lower()
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            if (resemblances_ip(host) or "." not in host or len(host) > 253
                    or host.endswith((".local", ".localhost", ".internal", ".lan"))
                    or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                           for label in host.split("."))):
                raise ValueError from None
        else:
            if not address.is_global:
                raise ValueError
            if address.version == 6:
                host = "[" + address.compressed + "]"
        if re.search(r"[\x00-\x1f\x7f\\]", unquote(parsed.path + parsed.query)):
            raise ValueError
        return urlunsplit(("https", host, parsed.path or "/", parsed.query, ""))
    except (ValueError, UnicodeError, AttributeError) as exc:
        raise AgentError("ARTICLE_URL_INVALID") from exc


class ArticleQuality:
    def __init__(self, translator, reviewer, spelling_url=SPELLING_URL, transport=None,
                 prompt_dir=None):
        if (translator is reviewer or (
                getattr(translator, "model", None) and getattr(reviewer, "model", None)
                and translator.model == reviewer.model)):
            raise AgentError("ARTICLE_INDEPENDENT_REVIEW_REQUIRED")
        if spelling_url != SPELLING_URL:
            raise AgentError("LOCAL_SPELLING_REQUIRED")
        self.translator, self.reviewer = translator, reviewer
        prompts = Path(prompt_dir) if prompt_dir else Path(__file__).resolve().parents[2] / "prompts"
        self.translation_prompt = (prompts / "ARTICLE_TRANSLATE.txt").read_text(encoding="utf-8")
        self.review_prompt = (prompts / "ARTICLE_REVIEW.txt").read_text(encoding="utf-8")
        self.spelling_url = spelling_url
        self.spelling = httpx.Client(timeout=60, trust_env=False, transport=transport)

    def _spelling_matches(self, text):
        try:
            response = self.spelling.post(self.spelling_url, data={"language": "ru-RU", "text": text})
            response.raise_for_status()
            matches = response.json()["matches"]
            if not isinstance(matches, list) or any(not isinstance(x, dict) for x in matches):
                raise TypeError
            return matches
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise AgentError("ARTICLE_SPELLING_UNAVAILABLE", 503) from exc

    def prepare(self, title, original, url):
        url = canonical_article_url(url)
        if not isinstance(title, str) or not isinstance(original, str):
            raise AgentError("ARTICLE_INPUT_INVALID")
        title, original = title.strip(), original.strip()
        source = title + "\n\n" + original
        if not 3 <= len(title) <= 500 or len(original) < 80 or len(source) > 9000:
            raise AgentError("ARTICLE_INPUT_SIZE_INVALID")
        if detect_language(source) != "en" or len(re.findall(r"[A-Za-z]{2,}", source)) < 12:
            raise AgentError("ARTICLE_ENGLISH_SOURCE_REQUIRED")
        context = {"instruction": self.translation_prompt,
                   "source_untrusted": {"title": title, "body": original, "url": url}}
        for _ in range(3):
            try:
                translated = extract_object(self.translator.generate_text(
                    json.dumps(context, ensure_ascii=False))[0])
            except AgentError as exc:
                if exc.code != "INVALID_EDITOR_RESULT":
                    raise
                context["repair_feedback"] = "Return only valid JSON with title and body strings."
                continue
            if (set(translated) != {"title", "body"}
                    or any(not isinstance(v, str) or not v.strip() for v in translated.values())):
                context["repair_feedback"] = "Return exactly title and body as nonempty JSON strings."
                continue
            translated = {key: value.strip() for key, value in translated.items()}
            text = translated["title"] + "\n\n" + translated["body"]
            full = text + "\n\nИсточник: " + url + "\nПеревод и редактура с помощью нейросетей."
            context["previous_translation_untrusted"] = translated
            try:
                translation_gate(source, text)
                if len(full) > 3500:
                    raise AgentError("ARTICLE_POST_TOO_LONG")
            except AgentError as exc:
                context["repair_feedback"] = exc.code
                continue
            review_context = {"instruction": self.review_prompt,
                              "source_untrusted": source, "translation_untrusted": text}
            try:
                reviewed = extract_object(self.reviewer.generate_text(
                    json.dumps(review_context, ensure_ascii=False))[0])
            except AgentError as exc:
                if exc.code != "INVALID_EDITOR_RESULT":
                    raise
                context["repair_feedback"] = "Independent review returned invalid JSON; approval was not granted."
                continue
            if (set(reviewed) != {"approved", "topic", "issues"}
                    or type(reviewed["approved"]) is not bool
                    or not isinstance(reviewed["topic"], str)
                    or not isinstance(reviewed["issues"], list)
                    or any(not isinstance(issue, str) for issue in reviewed["issues"])):
                context["repair_feedback"] = "Independent review schema invalid; approval was not granted."
                continue
            matches = self._spelling_matches(text)
            if (reviewed["approved"] and not reviewed["issues"]
                    and reviewed["topic"] in TOPICS and not matches):
                return {"title": translated["title"], "body": full, "translation": text,
                        "original": source, "url": url, "topic": reviewed["topic"]}
            context["repair_feedback"] = {"review_approved": reviewed["approved"],
                "topic": reviewed["topic"], "review_issues": reviewed["issues"],
                "spelling_matches": matches[:20]}
        raise AgentError("ARTICLE_QUALITY_REJECTED")

    def close(self):
        self.spelling.close()
