"""Отдельный адаптер раздела noirlab_iotw."""

SPEC = {
    "id": "noirlab_iotw",
    "host": "noirlab.edu",
    "path": "/public/images/iotw[0-9]{4}[a-z]/",
    "rights_evidence": "https://noirlab.edu/public/copyright/",
    "rights_text_sha256": "b07b25ddc7ef6e54dae494fec6f2a76b0b32cbc770e1c5bb25f8bd5b076b88f0",
    "attribution": "CC BY 4.0",
}

CATALOG = {
    "catalog": "/public/images/iotw/",
    "pages": "/public/images/iotw/page/([1-9][0-9]{0,3})/",
    "kind": "Image of the Week",
    "publisher": "NOIRLab",
    "rights_labels": {"Copyright", "Usage of NOIRLab Images and Videos"},
    "newsletter_url": "https://noirlab.edu/public/subscribe-media/",
    "newsletter_label": "Are you a journalist? Subscribe to the NOIRLab Media Newsletter.",
}

IMAGE_HOST = ("storage.noirlab.edu", "/media/archives/images/")

TEXT_CREDIT = "NOIRLab"

IMAGE_SECTION = "/public/images/"

ADAPTATION_NOTICE = "Перевод и адаптация."


def narrative_node(node):
    return node.name == "p"


def known_notice(anchor):
    return False
