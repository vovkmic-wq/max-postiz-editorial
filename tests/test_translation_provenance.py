"""Persist translation provenance without altering article preparation policy."""
from types import SimpleNamespace

import httpx
import pytest

from max_postiz_agent.hub_models import Dispatch, TargetApproval, Variant
from max_postiz_agent.translation import DedicatedTranslator, MODELS
from test_article_cycle import CandidateQuality, Quality, failed_preparation

pytest_plugins = ['test_article_cycle']


@pytest.mark.parametrize('model', sorted(MODELS))
def test_dedicated_article_metadata_names_actual_translation_and_review_clients(cycle, model):
    value, core, article, records, calls, _, _ = cycle
    translator = DedicatedTranslator({'model': model, 'base_url': 'http://127.0.0.1:8094'},
        transport=httpx.MockTransport(lambda _: pytest.fail('Fixture must not contact a model')))
    quality = Quality()
    quality.translator = translator
    quality.reviewer = SimpleNamespace(model='actual-independent-reviewer')
    value.quality_factory = lambda: quality
    try:
        result = value.prepare(article)
    finally:
        translator.client.close()
    with core.db.session() as session:
        variant = session.get(Variant, result['variant_id'])
        assert variant.metadata_json['quality'] == 'dedicated_translation_independent_review_languagetool'
        assert variant.metadata_json['translation_model'] == model
        assert variant.metadata_json['review_model'] == 'actual-independent-reviewer'
        assert session.query(Dispatch).count() == session.query(TargetApproval).count() == 0
    assert result['state'] == 'prepared'
    assert not calls and not records


def test_legacy_qwen_preparation_keeps_existing_metadata_label(cycle):
    value, core, article, records, calls, _, _ = cycle
    result = value.prepare(article)
    with core.db.session() as session:
        metadata = session.get(Variant, result['variant_id']).metadata_json
        assert metadata['quality'] == 'qwen_ministral_languagetool'
        assert 'translation_model' not in metadata and 'review_model' not in metadata
    assert not calls and not records


def test_operator_candidate_does_not_claim_to_have_used_configured_translator(cycle):
    value, core, article, records, calls, _, _ = cycle
    failed = failed_preparation(value, article)
    translator = DedicatedTranslator(
        {'model': 'hy-mt2-1.8b-q8', 'base_url': 'http://127.0.0.1:8094'},
        transport=httpx.MockTransport(lambda _: pytest.fail('Manual candidate must not translate')))
    quality = CandidateQuality()
    quality.translator = translator
    quality.reviewer = SimpleNamespace(model='actual-independent-reviewer')
    value.quality_factory = lambda: quality
    try:
        result = value.prepare(article, retry_record=failed['id'], candidate={
            'title': 'Космический ветер',
            'body': 'Исследователи изучили необычную атмосферу далёкой звезды и обнаружили звёздный ветер.'})
    finally:
        translator.client.close()
    with core.db.session() as session:
        metadata = session.get(Variant, result['variant_id']).metadata_json
        assert metadata['quality'] == 'operator_ministral_languagetool'
        assert 'translation_model' not in metadata and 'review_model' not in metadata
    assert not calls and not records
