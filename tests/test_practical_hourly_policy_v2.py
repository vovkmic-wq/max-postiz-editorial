"""Future-only V2 is bound before models through record, audit and approval."""
import copy
import json
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from max_postiz_agent.article_cycle import automatic_review_rubric, review_rubric_binding
from max_postiz_agent.article_quality import ArticleQuality, HOURLY_PRACTICAL_V2_REVIEW_RUBRIC
from max_postiz_agent.automatic_policy import automatic_evidence
from max_postiz_agent.config import AgentError
from max_postiz_agent.db import Audit
from max_postiz_agent.hub_models import Dispatch, Target, TargetApproval, Variant
from test_automatic_policy import Model

pytest_plugins = ['test_automatic_policy']
V2 = HOURLY_PRACTICAL_V2_REVIEW_RUBRIC


@pytest.fixture
def practical_v2_cycle(automatic_cycle, monkeypatch):
    values, review = automatic_cycle
    cycle, core, _, _, _, _, policy = values
    policy['review_rubric'] = V2
    (core.settings.root / 'data/hourly-article-policy.local.json').write_text(json.dumps(policy))
    prompt = Path(__file__).resolve().parents[1] / 'prompts/ARTICLE_REVIEW_HOURLY_PRACTICAL_V2.txt'
    (core.settings.root / 'prompts' / prompt.name).write_bytes(prompt.read_bytes())
    with core.db.session.begin() as session:
        for target in session.scalars(select(Target)):
            target.paused = True
    candidate = {'title': 'Космический ветер', 'body': 'Учёные изучили необычные процессы в атмосфере далёкой звезды.'}
    match = {'offset': 0, 'length': 11, 'message': 'Замечание стиля', 'type': {'typeName': 'Other'},
        'rule': {'id': 'OPREDELENIA', 'issueType': 'uncategorized', 'category': {'id': 'STYLE'}}}

    def checker(record_id=None, review_only=False):
        return ArticleQuality(Model(copy.deepcopy(candidate), 'hy-mt2-7b-q8'),
            Model(copy.deepcopy(review), 'ministral'), prompt_dir=core.settings.root / 'prompts',
            trace=cycle._trace_writer(record_id), review_rubric=V2,
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json={'matches': [match]})))
    monkeypatch.setattr(cycle, '_quality', checker)
    return values, review, match


def test_new_v2_record_preparation_and_evidence_use_same_exact_marker(practical_v2_cycle):
    (cycle, core, article, _, _, _, _), _, match = practical_v2_cycle
    assert cycle._policy()['review_rubric'] == V2
    prepared = cycle.prepare(article)
    record = cycle._load()['records'][0]
    assert record['review_rubric'] == record['preparation_attempts'][0]['review_rubric'] == V2
    with core.db.session() as session:
        variant = session.get(Variant, prepared['variant_id'])
        assert review_rubric_binding(core, session, record, variant) == V2
        evidence = automatic_evidence(session, variant)
        assert evidence is not None
        raw = variant.metadata_json['machine_review_evidence']
        assert raw['review_rubric'] == V2
        assert raw['spelling_matches'] == [match] and raw['spelling_unresolved'] == []
        assert raw['spelling_adjudicated'][0]['effective_issue_type'] == 'style'


def test_v2_full_fake_chain_keeps_policy_approval_and_one_dispatch(practical_v2_cycle):
    (cycle, core, article, records, _, stamp, _), _, _ = practical_v2_cycle
    core.publisher.mode = 'browser'
    prepared = cycle.prepare(article)
    planned = cycle.publish(prepared['id'], prepared['content_hash'])
    with core.db.session() as session:
        approval = session.query(TargetApproval).one()
        audit = session.scalar(select(Audit).where(Audit.entity == approval.id,
            Audit.action == 'hourly_article_policy_review'))
        assert audit.details['review_rubric'] == V2 and approval.actor == 'hourly_article_policy'
    stamp[0] += 121
    assert core.dispatch_tick() == 1 and core.dispatch_tick() == 0
    with core.db.session() as session:
        dispatch = session.get(Dispatch, planned['dispatch_id'])
        assert dispatch.state == 'published' and dispatch.verified and dispatch.attempts == 1
    records[planned['postiz_id']]['state'] = 'PUBLISHED'
    assert cycle.status(prepared['id'])['state'] == 'published'


@pytest.mark.parametrize('field', ['record', 'attempt', 'audit', 'variant'])
def test_v2_cannot_be_mixed_with_v1_at_any_preparation_binding(practical_v2_cycle, field):
    (cycle, core, article, _, _, _, _), _, _ = practical_v2_cycle
    prepared = cycle.prepare(article)
    record = cycle._load()['records'][0]
    if field == 'record':
        record['review_rubric'] = 'hourly-practical-v1'
    elif field == 'attempt':
        record['preparation_attempts'][0]['review_rubric'] = 'hourly-practical-v1'
    with core.db.session.begin() as session:
        if field == 'audit':
            audit = session.scalar(select(Audit).where(Audit.entity == record['id'],
                Audit.action == 'hourly_article_prepare_attempt'))
            audit.details = {**audit.details, 'review_rubric': 'hourly-practical-v1'}
        variant = session.get(Variant, prepared['variant_id'])
        if field == 'variant':
            variant.metadata_json = {**variant.metadata_json, 'review_rubric': 'hourly-practical-v1'}
        with pytest.raises(AgentError, match='ARTICLE_REVIEW_RUBRIC_BINDING_INVALID'):
            review_rubric_binding(core, session, record, variant)


def test_new_policy_does_not_select_v2_for_unmarked_or_v1_history(practical_v2_cycle):
    assert automatic_review_rubric({}) == 'meaning-first-v1'
    assert automatic_review_rubric({'review_rubric': 'hourly-practical-v1'}) == 'hourly-practical-v1'
    assert automatic_review_rubric({'review_rubric': V2}) == V2


def test_v2_semantic_refusal_creates_no_draft_or_approval(practical_v2_cycle):
    (cycle, core, article, records, calls, _, _), review, _ = practical_v2_cycle
    review.update(approved=False, issues=['Пропущено существенное утверждение оригинала.'])
    with pytest.raises(AgentError, match='ARTICLE_QUALITY_REJECTED'):
        cycle.prepare(article)
    assert not records and not calls
    with core.db.session() as session:
        assert session.query(TargetApproval).count() == session.query(Dispatch).count() == 0
