"""Разные источники сохраняют общий час, точные права и прежние отказы."""

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from max_postiz_agent.article_repair import ArticleRepair
from max_postiz_agent.article_sources import (policy_for_article, policy_for_record,
    registered_policies, require_legacy_source, source_id_for_url, source_spec, validate_source_url)
from max_postiz_agent.config import AgentError, fingerprint, validate_schema
from max_postiz_agent.db import Audit, Material
from max_postiz_agent.hub_models import Dispatch, DispatchAttempt, Target, TargetApproval, Variant
from test_automatic_article_repair import CORRECT_BODY

pytest_plugins = ["test_automatic_policy"]

URLS = {"noirlab_iotw": "https://noirlab.edu/public/images/iotw2642a/",
        "esahubble_potw": "https://esahubble.org/images/potw2642a/"}


def test_science_example_can_be_installed_without_a_generic_collection_route():
    from max_postiz_agent.collectors import collect_web
    root = Path(__file__).resolve().parents[1]
    configured = json.loads((root / "config/science_sources.example.json").read_text())
    validate_schema(root, "sources.schema.json", configured)
    for source in configured["sources"]:
        assert source["enabled"] is False and source["collection_mode"] == "manual"
        assert source["extraction_profile"] == "server_science"
        source["enabled"] = True
        with pytest.raises(AgentError, match="SCIENCE_DEDICATED_COLLECTOR_REQUIRED"):
            collect_web(source, {}, None, root)
    validate_schema(root, "sources.schema.json", configured)


def configured_sources():
    values = {}
    for source_id in URLS:
        spec = source_spec(source_id)
        values[source_id] = {"id": source_id, "enabled": True,
            "rights": {"verified": True, "allow_storage": True, "allow_derivative": True,
                       "allow_full_repost": True,
                       "evidence_ref": spec["rights_evidence"]}}

    def source(source_id):
        if source_id not in values:
            raise AgentError("UNKNOWN_SOURCE", 404)
        return values[source_id]

    return SimpleNamespace(source=source), values


@pytest.fixture
def base_policy():
    return {"enabled": True, "target_id": "max_test", "source_id": "nasa_svs_articles",
        "allowed_hosts": ["svs.gsfc.nasa.gov"], "rights_evidence": "https://svs.gsfc.nasa.gov/help/",
        "owner_instruction": "Existing explicit hourly authorization", "min_gap_seconds": 3600,
        "max_per_day": 24, "max_source_age_hours": 8760, "approval_mode": "automatic",
        "review_rubric": "meaning-first-v1"}


def test_derived_policies_do_not_rewrite_base_or_weaken_common_guards(base_policy):
    settings, _ = configured_sources()
    before = copy.deepcopy(base_policy)
    policies = registered_policies(base_policy, settings)
    assert base_policy == policies[0] == before
    assert len(policies) == 3 and len({fingerprint(p) for p in policies}) == 3
    changed = {"source_id", "allowed_hosts", "rights_evidence", "owner_instruction"}
    for policy in policies[1:]:
        assert {k: v for k, v in policy.items() if k not in changed} == {
            k: v for k, v in before.items() if k not in changed}
        assert policy["owner_instruction"].startswith(before["owner_instruction"])
        assert policy_for_record(base_policy, settings,
            {"source_id": policy["source_id"], "url": URLS[policy["source_id"]]}) == policy
        assert policy_for_article(base_policy, settings,
            {"url": URLS[policy["source_id"]]}) == policy
    policies[0]["allowed_hosts"].append("untrusted.example")
    assert base_policy == before


@pytest.mark.parametrize("source_id", list(URLS))
def test_exact_registry_paths_and_rights(source_id):
    spec = source_spec(source_id)
    assert spec["attribution"] == "CC BY 4.0"
    assert source_id_for_url(URLS[source_id]) == source_id
    assert validate_source_url(source_id, URLS[source_id]) == URLS[source_id]
    spec["host"] = "untrusted.example"
    assert source_spec(source_id)["host"] != spec["host"]
    other = next(value for key, value in URLS.items() if key != source_id)
    with pytest.raises(AgentError, match="ARTICLE_SOURCE_URL_INVALID"):
        validate_source_url(source_id, other)


@pytest.mark.parametrize("source_id", list(URLS))
def test_semantic_rights_contract_is_bound_to_policy_hash(base_policy, monkeypatch, source_id):
    from max_postiz_agent import article_sources
    settings, _ = configured_sources()
    before = copy.deepcopy(base_policy)
    article = {"url": URLS[source_id]}
    old = policy_for_article(base_policy, settings, article)
    digest = source_spec(source_id)["rights_text_sha256"]
    assert len(digest) == 64
    assert "rights-contract-sha256=" + digest in old["owner_instruction"]
    changed_spec = {**source_spec(source_id), "rights_text_sha256": "0" * 64}
    monkeypatch.setitem(article_sources._SOURCES, source_id, changed_spec)
    current = policy_for_article(base_policy, settings, article)
    assert fingerprint(current) != fingerprint(old)
    assert current["rights_evidence"] == old["rights_evidence"]
    assert base_policy == before == registered_policies(base_policy, settings)[0]


@pytest.mark.parametrize("url", [
    "http://noirlab.edu/public/images/iotw2642a/",
    "https://noirlab.edu:443/public/images/iotw2642a/",
    "https://owner@noirlab.edu/public/images/iotw2642a/",
    "https://noirlab.edu/public/images/iotw2642a/?edit=1",
    "https://noirlab.edu/public/images/iotw2642a/#caption",
    "https://noirlab.edu/public/images/iotw2642a",
    "https://noirlab.edu/public/images/potw2642a/",
    "https://noirlab.edu/public/images/iotw2642a/../../copyright/",
    "https://noirlab.edu/public/images/iotw2642a%2f/",
    "https://esahubble.org/news/",
    "https://esahubble.org/images/potw2642/",
])
def test_known_host_never_grants_other_sections_or_noncanonical_urls(url):
    with pytest.raises(AgentError, match="ARTICLE_SOURCE_URL_INVALID"):
        source_id_for_url(url)


def test_unknown_or_unconfigured_source_never_falls_back_to_nasa(base_policy):
    settings, values = configured_sources()
    assert source_id_for_url("https://svs.gsfc.nasa.gov/5682/") is None
    assert policy_for_article(base_policy, settings,
        {"url": "https://svs.gsfc.nasa.gov/5682/"}) == base_policy
    for record in ({"source_id": "unregistered", "url": URLS["noirlab_iotw"]},
                   {"source_id": "nasa_svs_articles", "url": URLS["noirlab_iotw"]},
                   {"source_id": "noirlab_iotw", "url": URLS["esahubble_potw"]}):
        with pytest.raises(AgentError):
            policy_for_record(base_policy, settings, record)
    values["noirlab_iotw"]["enabled"] = False
    with pytest.raises(AgentError, match="EDITORIAL_SOURCE_POLICY_REQUIRED"):
        policy_for_article(base_policy, settings, {"url": URLS["noirlab_iotw"]})
    with pytest.raises(AgentError, match="EDITORIAL_SOURCE_POLICY_REQUIRED"):
        policy_for_article(base_policy, settings, {"url": "https://unknown.example/science/"})


@pytest.mark.parametrize("key,value", [("verified", False), ("allow_storage", False),
    ("allow_derivative", False), ("allow_full_repost", False),
    ("evidence_ref", "https://noirlab.edu/")])
def test_rights_are_explicit_before_policy_selection(base_policy, key, value):
    settings, values = configured_sources()
    values["noirlab_iotw"]["rights"][key] = value
    with pytest.raises(AgentError, match="ARTICLE_RIGHTS_REQUIRED"):
        registered_policies(base_policy, settings)


@pytest.mark.parametrize("mode", ["human", "legacy"])
def test_new_sources_never_enable_a_human_or_legacy_policy(base_policy, mode):
    settings, _ = configured_sources()
    base_policy["approval_mode"] = mode
    assert registered_policies(base_policy, settings) == [base_policy]
    with pytest.raises(AgentError, match="EDITORIAL_SOURCE_POLICY_REQUIRED"):
        policy_for_article(base_policy, settings, {"url": URLS["noirlab_iotw"]})


@pytest.mark.parametrize("source_id", list(URLS))
def test_old_addressed_recoveries_do_not_expand_to_new_sources(source_id):
    with pytest.raises(AgentError, match="ARTICLE_LEGACY_RECOVERY_SOURCE_REQUIRED"):
        require_legacy_source({"source_id": source_id})
    require_legacy_source({"source_id": "nasa_svs_articles"})


def install_science_sources(core):
    template = core.settings.sources["sources"][0]
    for source_id in URLS:
        spec = source_spec(source_id)
        source = copy.deepcopy(template)
        source.update(id=source_id, name=source_id, enabled=True, type="html",
            endpoint="https://" + spec["host"] + "/", channel_ids=["main"])
        source["rights"].update(verified=True, allow_storage=True, allow_derivative=True,
            allow_full_repost=True,
            evidence_ref=spec["rights_evidence"])
        core.settings.sources["sources"].append(source)


@pytest.mark.parametrize("source_id", list(URLS))
def test_new_source_repair_policy_and_restricted_worker_deliver_once(
    automatic_cycle, bridge, monkeypatch, source_id
):
    (cycle, core, article, records, writes, stamp, base), review = automatic_cycle
    install_science_sources(core)
    with core.db.session.begin() as session:
        for target in session.scalars(select(Target)):
            target.paused = True
    policy_path = core.settings.root / "data/hourly-article-policy.local.json"
    historical_policy = policy_path.read_bytes()
    article["url"] = URLS[source_id]
    review.update(approved=False, issues=["The discovery of the stellar wind is missing."])
    counts = {"hy": 0, "min": 0, "lt": 0, "qwen": 0}
    factory = cycle._quality

    def quality(*args, **kwargs):
        result = factory(*args, **kwargs)
        for model, key in ((result.translator, "hy"), (result.reviewer, "min")):
            generate = model.generate_text

            def counted(prompt, *, generate=generate, key=key):
                counts[key] += 1
                return generate(prompt)

            model.generate_text = counted
        spelling = result._spelling_matches

        def checked(text):
            counts["lt"] += 1
            return spelling(text)

        result._spelling_matches = checked
        return result

    monkeypatch.setattr(cycle, "_quality", quality)
    with pytest.raises(AgentError, match="ARTICLE_QUALITY_REJECTED"):
        cycle.prepare(article)
    original = copy.deepcopy(cycle._load()["records"][0])
    old_trace = (core.settings.root / original["quality_trace"]).read_bytes()
    assert original["source_id"] == source_id
    assert original["policy_hash"] != fingerprint(base)
    other_id = next(value for value in URLS if value != source_id)
    with pytest.raises(AgentError, match="ARTICLE_SLOT_ALREADY_CLAIMED"):
        cycle.prepare({**article, "url": URLS[other_id], "original": article["original"] + " A new fact."})

    class Editor:
        model = "proxmox/qwen35-9b"

        def generate_text(self, _prompt):
            counts["qwen"] += 1
            return json.dumps({"title": "Космический ветер", "body": CORRECT_BODY}), {}

    cycle.repair_factory = lambda trace: ArticleRepair(
        Editor(), prompt_dir=core.settings.root / "prompts", trace=trace)
    review.update(approved=True, issues=[])
    prepared = cycle.repair_rejected(original["id"])
    repaired = copy.deepcopy(cycle._load()["records"][0])
    assert counts == {"hy": 1, "min": 2, "lt": 2, "qwen": 1}
    assert repaired["source_id"] == source_id
    assert repaired["automatic_repair_attempts"][0]["policy_hash"] == original["policy_hash"]
    assert repaired["preparation_attempts"][0] == original["preparation_attempts"][0]
    assert (core.settings.root / original["quality_trace"]).read_bytes().startswith(old_trace)
    assert len(records) == len(writes) == 1 and records[prepared["postiz_id"]]["state"] == "DRAFT"
    with core.db.session() as session:
        variant = session.get(Variant, prepared["variant_id"])
        target = session.get(Target, variant.target_id)
        original_body = variant.body
        # Generic gate признаёт только полный footer, заданный кодом.
        for changed in (original_body.replace("CC BY 4.0", "CC BY 3.0"),
                        original_body.replace("Текст:", "Фото:"),
                        original_body.replace("Машинный перевод на русский и автоматическая редактура.", "")):
            variant.body = changed
            with pytest.raises(AgentError, match="ARTICLE_ATTRIBUTION_REQUIRED"):
                core._quality(session, variant, target)
        variant.body = "123 " + original_body
        with pytest.raises(AgentError, match="QUALITY_GATE_BLOCKED"):
            core._quality(session, variant, target)
        variant.body = original_body

    planned = cycle.publish(prepared["id"], prepared["content_hash"])
    core.publisher.mode = "browser"
    assert core.dispatch_tick() == 0 and not bridge[3]
    stamp[0] += 121
    assert core.dispatch_tick() == 1
    assert core.dispatch_tick() == 0 and len(bridge[3]) == 1
    records[planned["postiz_id"]]["state"] = "PUBLISHED"
    assert cycle.status(planned["id"])["state"] == "published"
    with core.db.session() as session:
        dispatch = session.get(Dispatch, planned["dispatch_id"])
        assert dispatch.verified and dispatch.attempts == 1
        assert session.query(DispatchAttempt).count() == session.query(TargetApproval).count() == 1
        assert session.query(Variant).count() == session.query(Material).count() == 1
        assert all(target.paused for target in session.scalars(select(Target)))
        audit = session.scalar(select(Audit).where(Audit.action == "hourly_article_policy_review"))
        assert audit.details["policy_hash"] == repaired["policy_hash"]
        assert not session.scalar(select(Audit).where(Audit.action == "hourly_article_human_review"))
    stamp[0] += 3600
    with pytest.raises(AgentError, match="ARTICLE_DUPLICATE_SOURCE"):
        cycle.prepare({**article, "url": URLS[other_id]})
    assert counts == {"hy": 1, "min": 2, "lt": 2, "qwen": 1}
    assert policy_path.read_bytes() == historical_policy


@pytest.mark.parametrize("change,error", [("policy", "ARTICLE_POLICY_CHANGED"),
                                           ("rights", "ARTICLE_RIGHTS_REQUIRED")])
def test_source_policy_or_rights_change_during_qa_cannot_create_a_draft(
    automatic_cycle, monkeypatch, change, error
):
    (cycle, core, article, records, writes, _, _), _ = automatic_cycle
    install_science_sources(core)
    article["url"] = URLS["noirlab_iotw"]
    factory = cycle._quality
    count = []

    def quality(*args, **kwargs):
        result = factory(*args, **kwargs)
        generate = result.reviewer.generate_text

        def changed(prompt):
            count.append(prompt)
            raw = generate(prompt)
            if change == "policy":
                path = core.settings.root / "data/hourly-article-policy.local.json"
                policy = json.loads(path.read_text(encoding="utf-8"))
                policy["max_per_day"] = 23
                path.write_text(json.dumps(policy), encoding="utf-8")
            else:
                core.settings.source("noirlab_iotw")["rights"]["allow_derivative"] = False
            return raw

        result.reviewer.generate_text = changed
        return result

    monkeypatch.setattr(cycle, "_quality", quality)
    with pytest.raises(AgentError, match=error):
        cycle.prepare(article)
    record = cycle._load()["records"][0]
    assert record["state"] == "needs_attention" and record["error"] == error
    assert record["source_id"] == "noirlab_iotw" and len(count) == 1
    assert not records and not writes
    with core.db.session() as session:
        assert session.query(Material).count() == session.query(Variant).count() == 0
        assert session.query(TargetApproval).count() == session.query(Dispatch).count() == 0


def test_new_source_cannot_borrow_nasa_policy_hash_for_dispatch(automatic_cycle, bridge):
    (cycle, core, article, _, _, stamp, base), _ = automatic_cycle
    install_science_sources(core)
    article["url"] = URLS["noirlab_iotw"]
    prepared = cycle.prepare(article)
    planned = cycle.publish(prepared["id"], prepared["content_hash"])
    with core.db.session.begin() as session:
        variant = session.get(Variant, prepared["variant_id"])
        variant.metadata_json = {**variant.metadata_json, "policy_hash": fingerprint(base)}
    core.publisher.mode = "browser"
    stamp[0] += 121
    assert core.dispatch_tick() == 0 and not bridge[3]
    with core.db.session() as session:
        dispatch = session.get(Dispatch, planned["dispatch_id"])
        assert dispatch.state == "cancelled" and dispatch.attempts == 0
        assert session.query(DispatchAttempt).count() == 0
