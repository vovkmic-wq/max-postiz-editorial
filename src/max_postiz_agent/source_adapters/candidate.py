"""Общие ограничения чтения для индивидуальных, ещё не допущенных адаптеров.

Извлечение не выдаёт права, не вызывает модель и не создаёт запись в очереди.
Исходные HTML сохраняет вызывающий код по возвращённому SHA256.
"""

from dataclasses import dataclass
import hashlib
import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from ..config import AgentError

MAX_HTML_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class CandidateAdapter:
    source_id: str
    name: str
    host: str
    article_path: str
    title_selector: str
    body_selector: str
    date_selector: str
    credit_selector: str
    rights_reference: str | None
    query_pattern: str = ""
    exclusions: str = ""
    boundary_blockers: tuple[str, ...] = ()
    duplicate_attributes: tuple[tuple[str, str, str], ...] = ()
    image_hosts: tuple[str, ...] = ()
    image_selector: str = 'meta[property="og:image"]'

    def validate_url(self, url):
        try:
            parsed = urlsplit(url)
            valid = (
                isinstance(url, str)
                and not re.search(r"[\x00-\x20\x7f\\]", url)
                and parsed.scheme == "https"
                and parsed.netloc == self.host
                and not parsed.fragment
                and not re.search(r"%2[eEfF]|%5[cC]|%25", parsed.path)
                and not any(part in {".", ".."} for part in parsed.path.split("/"))
                and re.fullmatch(self.article_path, parsed.path) is not None
                and re.fullmatch(self.query_pattern, parsed.query) is not None
            )
        except (ValueError, TypeError, AttributeError):
            valid = False
        if not valid:
            raise AgentError("ARTICLE_SOURCE_URL_INVALID", 409)
        return url

    def _html(self, raw):
        if not isinstance(raw, bytes) or not raw or len(raw) > MAX_HTML_BYTES:
            raise AgentError("ARTICLE_SOURCE_TOO_LARGE", 409)

        def duplicate(attrs, name, value):
            # Только вручную рассмотренные ошибки оформления конкретного сайта.
            # Первый атрибут сохраняется, как в HTML браузера. URL и признаки
            # лицензии никогда не разрешаются этой таблицей исключений.
            if (
                name
                in {
                    "class",
                    "title",
                    "id",
                    "rel",
                    "defer",
                    "aria-current",
                    "role",
                    "type",
                    "aria-label",
                    "itemprop",
                }
                and (name, attrs.get(name), value) in self.duplicate_attributes
            ):
                return
            raise AgentError("ARTICLE_SOURCE_HTML_INVALID", 409)

        try:
            return BeautifulSoup(
                raw.decode("utf-8"), "html.parser", on_duplicate_attribute=duplicate
            )
        except UnicodeError as exc:
            raise AgentError("ARTICLE_SOURCE_HTML_INVALID", 409) from exc

    def discover(self, raw, index_url):
        """Только ссылки разрешённого раздела; без оценки похожести и запасного URL."""
        parsed = urlsplit(index_url)
        if parsed.scheme != "https" or parsed.netloc != self.host or parsed.fragment:
            raise AgentError("ARTICLE_SOURCE_URL_INVALID", 409)
        soup = self._html(raw)
        result = []
        for anchor in soup.select("a[href]"):
            literal = anchor["href"]
            if re.search(r"[\x00-\x20\x7f\\]", literal):
                continue
            if any(part in {".", ".."} for part in urlsplit(literal).path.split("/")):
                continue
            url = urljoin(index_url, literal)
            try:
                self.validate_url(url)
            except AgentError:
                continue
            if url != index_url and url not in result:
                result.append(url)
        return result[:100]

    @staticmethod
    def _values(soup, selector):
        if not selector:
            return []
        return list(
            dict.fromkeys(
                node.get("content") or node.get("datetime") or node.get_text(" ", strip=True)
                for node in soup.select(selector)
            )
        )

    def extract(self, raw, url):
        self.validate_url(url)
        soup = self._html(raw)
        titles = self._values(soup, self.title_selector)
        if len(titles) != 1 or not titles[0]:
            raise AgentError("ARTICLE_SOURCE_TITLE_AMBIGUOUS", 409)
        bodies = soup.select(self.body_selector)
        if len(bodies) != 1:
            raise AgentError("ARTICLE_SOURCE_BODY_AMBIGUOUS", 409)
        # Проверяем исключения до удаления оформления: право на картинку может
        # быть ограничено в подписи, которая не входит в повествование.
        original_body = str(bodies[0])
        rights_notices = []
        for node in bodies[0].select("p, figcaption, [class*='credit'], [class*='caption']"):
            text = node.get_text(" ", strip=True)
            if re.search(
                r"not released|not licensed|all rights reserved|used with permission|"
                r"no derivatives|permission required|Adobe Stock|iStock",
                text,
                re.I,
            ):
                rights_notices.append(text)
        body = BeautifulSoup(original_body, "html.parser")
        for node in body.select(
            "script, style, nav, form, button, iframe, noscript"
            + (", " + self.exclusions if self.exclusions else "")
        ):
            node.decompose()
        paragraphs = []
        links = []
        contact_links = []
        for node in body.select("h2, h3, h4, p, li, blockquote"):
            # Не дублируем вложенные p/li; сохраняем единый текст внешнего блока.
            if node.find_parent(["p", "li", "blockquote"]):
                continue
            for anchor in node.select("a[href]"):
                target = urljoin(url, anchor["href"])
                parsed = urlsplit(target)
                if (
                    parsed.scheme == "mailto"
                    and not parsed.query
                    and re.fullmatch(
                        r"mailto:[A-Za-z0-9_.+\-]+@[A-Za-z0-9.\-]+(?:, ?[A-Za-z0-9_.+\-]+@[A-Za-z0-9.\-]+)*",
                        target,
                    )
                ):
                    contact_links.append(target)
                    # Контакт остаётся текстом; отправка письма никогда не вызывается.
                    continue
                if parsed.scheme not in {"https", "http"} or parsed.username or parsed.password:
                    raise AgentError("ARTICLE_SOURCE_LINK_UNSUPPORTED", 409)
                label = anchor.get_text(" ", strip=True)
                links.append({"label": label, "url": target})
                anchor.replace_with(f"[{label}]({target})")
            text = node.get_text(" ", strip=True)
            if text:
                paragraphs.append(text)
        text = "\n\n".join(paragraphs)
        if len(text) < 100:
            raise AgentError("ARTICLE_SOURCE_NARRATIVE_REQUIRED", 409)
        dates = self._values(soup, self.date_selector)
        credits = self._values(soup, self.credit_selector)
        images = []
        for image in soup.select(self.image_selector):
            literal = image.get("content") or image.get("src")
            if not literal:
                continue
            target = urljoin(url, literal)
            parsed = urlsplit(target)
            if (
                parsed.scheme == "https"
                and parsed.netloc in self.image_hosts
                and not parsed.fragment
                and not re.search(r"[\x00-\x20\x7f\\]", target)
                and target not in images
            ):
                images.append(target)
        blockers = list(self.boundary_blockers)
        if contact_links:
            blockers.append("CONTACT_REFERENCES_REQUIRE_EDITORIAL_REVIEW")
        if len(dates) != 1 or not dates[0]:
            blockers.append("PUBLICATION_DATE_MISSING_OR_AMBIGUOUS")
        if not credits or not all(credits):
            blockers.append("IMAGE_CREDIT_REQUIRED")
        if not images:
            blockers.append("SOURCE_IMAGE_CANDIDATE_REQUIRED")
        if rights_notices:
            blockers.append("MATERIAL_RIGHTS_EXCEPTION_REQUIRES_REVIEW")
        if len(text) > 7500:
            blockers.append("ARTICLE_SOURCE_LENGTH_INVALID")
        blockers.extend(
            (
                "EXACT_TEXT_AND_IMAGE_RIGHTS_ADMISSION_REQUIRED",
                "NATIVE_IMAGE_PROOF_REQUIRED",
                "FULL_EDITORIAL_CYCLE_NOT_VERIFIED",
            )
        )
        return {
            "contract": "editorial-source-candidate-v1",
            "source_id": self.source_id,
            "url": url,
            "title": titles[0],
            "original": text,
            "links": links,
            "contact_links": contact_links,
            "date_literals": dates,
            "credits": credits,
            "rights_notices": rights_notices,
            "image_candidates": images,
            "rights_reference": self.rights_reference,
            "html_sha256": hashlib.sha256(raw).hexdigest(),
            "body_html_sha256": hashlib.sha256(original_body.encode()).hexdigest(),
            "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "blockers": list(dict.fromkeys(blockers)),
            "automatic_publication_enabled": False,
        }
