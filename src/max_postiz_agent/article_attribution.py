"""Точная атрибуция отделена от перевода и включена в согласованный snapshot."""

from .article_sources import source_id_for_url
from .config import AgentError

CC_BY_URL = "https://creativecommons.org/licenses/by/4.0/"
TEXT_CREDITS = {"noirlab_iotw": "NOIRLab", "esahubble_potw": "ESA/Hubble"}
ILLUSTRATED_PRESENTATION_CONTRACT = "article-illustrated-v1"
CURRENT_ILLUSTRATED_PRESENTATION_CONTRACT = "article-illustrated-v2"
ILLUSTRATED_PRESENTATION_CONTRACTS = frozenset({
    ILLUSTRATED_PRESENTATION_CONTRACT, CURRENT_ILLUSTRATED_PRESENTATION_CONTRACT,
})


def illustrated_presentation(contract):
    """Принимаем только явную версию; прежние подписи не переписываем."""
    return isinstance(contract, str) and contract in ILLUSTRATED_PRESENTATION_CONTRACTS


def presentation_binding(presentation_contract=None, image_credit=None):
    """Новая подпись выбирается явно; прежние тексты не получают новый формат."""
    if presentation_contract is None and image_credit is None:
        return {}
    if not illustrated_presentation(presentation_contract):
        raise AgentError("ARTICLE_PRESENTATION_CONTRACT_INVALID", 409)
    if (not isinstance(image_credit, str) or not image_credit or image_credit != image_credit.strip()
            or len(image_credit) > 1000 or any(ord(char) < 32 or char in "\u0085\u2028\u2029"
                                             for char in image_credit)):
        raise AgentError("ARTICLE_IMAGE_CREDIT_REQUIRED", 409)
    return {"presentation_contract": presentation_contract, "image_credit": image_credit}


def article_footer(url, *, presentation_contract=None, image_credit=None):
    presentation = presentation_binding(presentation_contract, image_credit)
    source_id = source_id_for_url(url)
    if source_id is None:
        # Исторические NASA-снимки, хеши и доказательства остаются побуквенно прежними.
        footer = "Источник: " + url
        if presentation:
            return footer + "\nПеревод и адаптация.\nИзображение: " + image_credit
        return footer + "\nПеревод и редактура с помощью нейросетей."
    footer = ("Текст: " + TEXT_CREDITS[source_id] + ".\nИсточник: " + url
            + "\nЛицензия CC BY 4.0: " + CC_BY_URL)
    if presentation_contract != CURRENT_ILLUSTRATED_PRESENTATION_CONTRACT:
        # Legacy и illustrated-v1 воспроизводятся по прежней точной подписи.
        footer += "\nАрхивный научный материал."
    if presentation:
        return footer + "\nПеревод и адаптация.\nИзображение: " + image_credit
    return footer + "\nМашинный перевод на русский и автоматическая редактура."


def requires_licensed_footer(url):
    return source_id_for_url(url) is not None
