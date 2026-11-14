"""Отдельный адаптер раздела esahubble_potw."""

SPEC = {
    "id": "esahubble_potw",
    "host": "esahubble.org",
    "path": "/images/potw[0-9]{4}[a-z]/",
    "rights_evidence": "https://esahubble.org/copyright/",
    "rights_text_sha256": "2651482536d414ad5fb300eab4c4fa7ae80e7836ee1b2cfb6ae08c643905d552",
    "attribution": "CC BY 4.0",
}

CATALOG = {
    "catalog": "/images/potw/",
    "pages": "/images/potw/page/([1-9][0-9]{0,3})/",
    "kind": "Picture of the Week",
    "publisher": "ESA/Hubble",
    "rights_labels": {"Copyright", "Usage of ESA/Hubble Images and Videos"},
    "newsletter_url": "https://esahubble.org/press/mailinglist",
    "newsletter_label": "Are you a journalist? Subscribe to the ESA/Hubble Media Newsletter.",
}

IMAGE_HOST = ("cdn.esahubble.org", "/archives/images/")

TEXT_CREDIT = "ESA/Hubble"

IMAGE_SECTION = "/images/"

ADAPTATION_NOTICE = "Перевод и адаптация."


def narrative_node(node):
    return (
        node.name == "div"
        and set(node.attrs) == {"style"}
        and node.get("style") == "margin-top: 10px"
        and node.find("p", recursive=False) is not None
    )


def known_notice(anchor):
    return False
