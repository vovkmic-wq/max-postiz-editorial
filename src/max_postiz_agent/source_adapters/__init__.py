"""Явный реестр: каждый раздел имеет собственный адаптер, без fallback."""

from ..config import AgentError
from . import esahubble_potw, eso_potw, noirlab_iotw

REGISTRY = {
    "eso_potw": eso_potw,
    "noirlab_iotw": noirlab_iotw,
    "esahubble_potw": esahubble_potw,
}


def adapter_for(source_id):
    if not isinstance(source_id, str) or source_id not in REGISTRY:
        raise AgentError("EDITORIAL_SOURCE_POLICY_REQUIRED", 409)
    return REGISTRY[source_id]
