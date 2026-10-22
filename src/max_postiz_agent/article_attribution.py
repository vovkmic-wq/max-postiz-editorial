"""Точная атрибуция отделена от перевода и включена в согласованный snapshot."""

from .article_sources import source_id_for_url

CC_BY_URL = "https://creativecommons.org/licenses/by/4.0/"
TEXT_CREDITS = {"noirlab_iotw": "NOIRLab", "esahubble_potw": "ESA/Hubble"}


def article_footer(url):
    source_id = source_id_for_url(url)
    if source_id is None:
        # Исторические NASA-снимки, хеши и доказательства остаются побуквенно прежними.
        return "Источник: " + url + "\nПеревод и редактура с помощью нейросетей."
    return ("Текст: " + TEXT_CREDITS[source_id] + ".\nИсточник: " + url
            + "\nЛицензия CC BY 4.0: " + CC_BY_URL
            + "\nАрхивный научный материал."
            + "\nМашинный перевод на русский и автоматическая редактура.")


def requires_licensed_footer(url):
    return source_id_for_url(url) is not None
