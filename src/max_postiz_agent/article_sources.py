"""Явные права и URL научных источников; общая политика остаётся неизменной.

Реестр не выдаёт права по доменному суффиксу и не подменяет проверку конкретной
страницы сборщиком. Каждый дополнительный источник должен быть включён отдельно
в конфигурации с тем же точным доказательством прав.
"""

import copy
from pathlib import Path
import re
from urllib.parse import urlsplit

from .config import AgentError, fingerprint, parse_json

_SOURCES = {
    "noirlab_iotw": {
        "id": "noirlab_iotw", "host": "noirlab.edu",
        "path": r"/public/images/iotw[0-9]{4}[a-z]/",
        "rights_evidence": "https://noirlab.edu/public/copyright/",
        "rights_text_sha256": "b07b25ddc7ef6e54dae494fec6f2a76b0b32cbc770e1c5bb25f8bd5b076b88f0",
        "attribution": "CC BY 4.0",
    },
    "esahubble_potw": {
        "id": "esahubble_potw", "host": "esahubble.org",
        "path": r"/images/potw[0-9]{4}[a-z]/",
        "rights_evidence": "https://esahubble.org/copyright/",
        "rights_text_sha256": "2651482536d414ad5fb300eab4c4fa7ae80e7836ee1b2cfb6ae08c643905d552",
        "attribution": "CC BY 4.0",
    },
}


def source_spec(source_id):
    """Возвращаем копию: сборщик не может изменить правила соседнего источника."""
    if not isinstance(source_id, str) or source_id not in _SOURCES:
        raise AgentError("EDITORIAL_SOURCE_POLICY_REQUIRED", 409)
    return copy.deepcopy(_SOURCES[source_id])


def validate_source_url(source_id, url):
    spec = source_spec(source_id)
    try:
        parsed = urlsplit(url)
        if (not isinstance(url, str) or parsed.scheme != "https" or parsed.netloc != spec["host"]
                or parsed.query or parsed.fragment or not re.fullmatch(spec["path"], parsed.path)
                or url != "https://" + spec["host"] + parsed.path):
            raise ValueError
    except (ValueError, TypeError, AttributeError) as exc:
        raise AgentError("ARTICLE_SOURCE_URL_INVALID", 409) from exc
    return url


def source_id_for_url(url):
    """Источник для footer: известные домены проверяются по точному разделу.

    None оставляет исторический footer неизменным. Это не разрешение публикации:
    неизвестный URL всё равно отклоняет отдельный policy_for_article.
    """
    try:
        host = urlsplit(url).hostname
    except (ValueError, TypeError, AttributeError) as exc:
        raise AgentError("ARTICLE_SOURCE_URL_INVALID", 409) from exc
    for source_id, spec in _SOURCES.items():
        if host == spec["host"]:
            validate_source_url(source_id, url)
            return source_id
    return None


def registered_policies(base, settings):
    """Не переписываем NASA policy: новые hashes относятся только к новым статьям."""
    result = [copy.deepcopy(base)]
    if base.get("approval_mode") != "automatic":
        return result
    for source_id, spec in _SOURCES.items():
        # Конфигурация обязательна; наличие встроенного адаптера само по себе не
        # разрешает новый маршрут. Неизвестный ID никогда не превращается в NASA.
        try:
            source = settings.source(source_id)
        except AgentError as exc:
            if exc.code == "UNKNOWN_SOURCE":
                continue
            raise
        if not source.get("enabled"):
            continue
        rights = source.get("rights", {})
        if (rights.get("verified") is not True or rights.get("allow_storage") is not True
                or rights.get("allow_derivative") is not True
                or rights.get("allow_full_repost") is not True
                or rights.get("evidence_ref") != spec["rights_evidence"]):
            raise AgentError("ARTICLE_RIGHTS_REQUIRED", 409)
        policy = copy.deepcopy(base)
        policy.update(source_id=source_id, allowed_hosts=[spec["host"]],
            rights_evidence=spec["rights_evidence"], owner_instruction=base["owner_instruction"]
            + " | Owner authorized additional scientific source: " + source_id
            + "; unchanged quality, history, cadence and deduplication safeguards"
            + "; rights-contract-sha256=" + spec["rights_text_sha256"])
        result.append(policy)
    return result


def _historical_policy(policy, settings, record):
    """Вернуть только точную неизменяемую политику существующей записи.

    Архив не выбирает источники и не разрешает повтор. Его имя и содержимое
    должны совпасть с уже закреплённым в PostgreSQL hash записи.
    """
    expected = record.get("policy_hash")
    root = getattr(settings, "root", None)
    if (not isinstance(expected, str) or re.fullmatch(r"[a-f0-9]{64}", expected) is None
            or fingerprint(policy) == expected or root is None):
        return policy
    root = Path(root).resolve()
    directory = root / "data/hourly-policy-history"
    path = directory / (expected + ".local.json")
    if not path.exists() and not path.is_symlink():
        return policy
    try:
        if (root.is_symlink() or (root / "data").is_symlink() or directory.is_symlink()
                or path.is_symlink() or path.resolve() != path or not path.is_file()
                or not 0 < path.stat().st_size <= 64 * 1024):
            raise ValueError
        saved = parse_json(path.read_text(encoding="utf-8"))
        if (not isinstance(saved, dict) or fingerprint(saved) != expected or set(saved) != set(policy)
                or any(saved.get(key) != policy.get(key) for key in
                       ("source_id", "target_id", "allowed_hosts", "rights_evidence",
                        "presentation_contract", "presentation_owner_instruction"))
                or saved.get("presentation_contract") != record.get("presentation_contract")):
            raise ValueError
    except (OSError, ValueError, TypeError, KeyError, AgentError) as exc:
        raise AgentError("ARTICLE_POLICY_HISTORY_INVALID", 409) from exc
    return saved


def policy_for_record(base, settings, record):
    if not isinstance(record, dict) or not isinstance(record.get("source_id"), str):
        raise AgentError("EDITORIAL_SOURCE_POLICY_REQUIRED", 409)
    from .article_attribution import illustrated_presentation
    selected_base = copy.deepcopy(base)
    presentation = record.get("presentation_contract")
    if presentation is None:
        # Настройка нового оформления не переписывает прежние policy hashes,
        # согласования и машинные доказательства уже подготовленных статей.
        selected_base.pop("presentation_contract", None)
        selected_base.pop("presentation_owner_instruction", None)
    elif not illustrated_presentation(presentation) or not illustrated_presentation(selected_base.get("presentation_contract")):
        raise AgentError("ARTICLE_PRESENTATION_CONTRACT_INVALID", 409)
    else:
        # При смене только оформления исходное поручение остаётся буквальным.
        # Историческая версия воспроизводит прежний policy hash; новый резерв
        # принимает только текущий marker через ready_for_policy.
        selected_base["presentation_contract"] = presentation
    for policy in registered_policies(selected_base, settings):
        if record["source_id"] == policy["source_id"]:
            policy = _historical_policy(policy, settings, record)
            if record["source_id"] in _SOURCES:
                validate_source_url(record["source_id"], record.get("url"))
            else:
                try:
                    parsed = urlsplit(record.get("url"))
                    if (parsed.scheme != "https" or parsed.hostname not in policy["allowed_hosts"]
                            or parsed.username or parsed.password or parsed.port not in {None, 443}
                            or parsed.query or parsed.fragment or not parsed.path):
                        raise ValueError
                except (ValueError, TypeError, AttributeError) as exc:
                    raise AgentError("ARTICLE_SOURCE_URL_INVALID", 409) from exc
            return policy
    raise AgentError("EDITORIAL_SOURCE_POLICY_REQUIRED", 409)


def policy_for_article(base, settings, article):
    try:
        host = urlsplit(article["url"]).hostname
    except (ValueError, TypeError, AttributeError, KeyError) as exc:
        raise AgentError("ARTICLE_INPUT_INVALID", 422) from exc
    for source_id, spec in _SOURCES.items():
        if host == spec["host"]:
            # Не принимаем другие разделы на том же домене под общей лицензией.
            validate_source_url(source_id, article["url"])
            return policy_for_record(base, settings, {"source_id": source_id, "url": article["url"],
                **({"presentation_contract": base["presentation_contract"]} if "presentation_contract" in base else {})})
    if host in base["allowed_hosts"]:
        return copy.deepcopy(base)
    raise AgentError("EDITORIAL_SOURCE_POLICY_REQUIRED", 409)


def require_legacy_source(record):
    """Старые адресные восстановления не расширяются на новый корпус источников."""
    if record.get("source_id") in _SOURCES:
        raise AgentError("ARTICLE_LEGACY_RECOVERY_SOURCE_REQUIRED", 409)
