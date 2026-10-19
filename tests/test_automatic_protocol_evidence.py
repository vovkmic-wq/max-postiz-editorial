"""Optional advisory protocol extensions must be exact, bounded and hash-bound."""
import copy

import pytest
from sqlalchemy import select

from max_postiz_agent.automatic_policy import automatic_evidence
from max_postiz_agent.article_quality import protocol_recovery_provenance
from max_postiz_agent.config import fingerprint
from max_postiz_agent.hub_models import Variant

pytest_plugins = ["test_automatic_policy"]


def replace_evidence(core, ident, mutate):
    with core.db.session.begin() as session:
        variant = session.get(Variant, ident)
        evidence = copy.deepcopy(variant.metadata_json["machine_review_evidence"])
        mutate(evidence)
        variant.metadata_json = {**variant.metadata_json, "machine_review_evidence": evidence,
                                 "machine_review_evidence_hash": fingerprint(evidence)}
    with core.db.session() as session:
        return automatic_evidence(session, session.get(Variant, ident))


def test_new_normal_four_warning_review_has_bound_contract_and_old_contract_cannot_claim_it(automatic_cycle):
    (cycle, core, article, _, _, _, _), review = automatic_cycle
    review["warnings"] = ["Advice " + str(i) for i in range(4)]
    prepared = cycle.prepare(article)
    with core.db.session() as session:
        variant = session.get(Variant, prepared["variant_id"])
        assert automatic_evidence(session, variant) is not None
        assert variant.metadata_json["machine_review_evidence"]["parser_contract"]["max_warnings"] == 8
        assert "protocol_recovery" not in variant.metadata_json["machine_review_evidence"]
    assert replace_evidence(core, prepared["variant_id"], lambda evidence: evidence.pop("parser_contract")) is None


def test_existing_modern_evidence_without_new_extensions_remains_valid_for_at_most_three_warnings(automatic_cycle):
    (cycle, core, article, _, _, _, _), _ = automatic_cycle
    prepared = cycle.prepare(article)
    assert replace_evidence(core, prepared["variant_id"], lambda evidence: evidence.pop("parser_contract")) is not None


@pytest.mark.parametrize("field,value", [
    ("contract", "unknown"), ("contract_hash", "0" * 64), ("raw_review_sha256", "0" * 64),
    ("max_issues", 4), ("max_warnings", 9), ("max_warning_chars", 1025),
    ("max_warnings_utf8_bytes", 8193), ("max_warnings", 8.0), ("extra", "not a known field"),
])
def test_recomputed_outer_hash_cannot_hide_a_changed_parser_contract(automatic_cycle, field, value):
    (cycle, core, article, _, _, _, _), _ = automatic_cycle
    prepared = cycle.prepare(article)
    result = replace_evidence(core, prepared["variant_id"],
        lambda evidence: evidence["parser_contract"].update({field: value}))
    assert result is None


@pytest.mark.parametrize("change", ["unknown_extension", "fake_recovery", "missing_parser_contract"])
def test_arbitrary_recovery_or_unknown_evidence_extensions_are_not_trusted(automatic_cycle, change):
    (cycle, core, article, _, _, _, _), _ = automatic_cycle
    prepared = cycle.prepare(article)

    def mutate(evidence):
        if change == "unknown_extension":
            evidence["unknown"] = True
        else:
            evidence["protocol_recovery"] = {"approved": True, "contract": "meaning-first-advisory-v2"}
            if change == "missing_parser_contract":
                evidence.pop("parser_contract")

    assert replace_evidence(core, prepared["variant_id"], mutate) is None
    with core.db.session() as session:
        assert not session.scalars(select(Variant).where(Variant.state == "approved")).all()


def test_recovery_provenance_is_validated_exactly_including_hashes_limits_and_numeric_types(automatic_cycle):
    (cycle, core, article, _, _, _, _), review = automatic_cycle
    review["warnings"] = ["Advice " + str(i) for i in range(4)]
    prepared = cycle.prepare(article)
    with core.db.session() as session:
        evidence = session.get(Variant, prepared["variant_id"]).metadata_json["machine_review_evidence"]
        protocol = protocol_recovery_provenance(evidence["review_raw"], evidence["review_prompt_hash"])
    # This unit checks the extension validator; the durable old/new trace
    # protocol and fresh LanguageTool flow are exercised by cycle recovery tests.
    assert replace_evidence(core, prepared["variant_id"],
        lambda evidence: evidence.update(protocol_recovery=copy.deepcopy(protocol))) is not None
    for field, value in [
        ("contract", "unknown"), ("parser_contract_hash", "0" * 64),
        ("raw_review_sha256", "0" * 64), ("review_prompt_hash", "0" * 64),
        ("legacy_error", "another_error"), ("reason", "arbitrary_override"),
        ("legacy_warning_limit", 4), ("warning_count", 4.0), ("warning_limit", 9),
        ("warnings_utf8_limit", 8193), ("raw_utf8_limit", 65537), ("extra", True),
    ]:
        assert replace_evidence(core, prepared["variant_id"],
            lambda evidence, field=field, value=value: evidence.update(
                protocol_recovery={**protocol, field: value})) is None
