"""Real evidence flows from quality to a draft and an exact human exception."""
import json

import httpx
import pytest

from max_postiz_agent.article_quality import ArticleQuality
from max_postiz_agent.config import AgentError
from max_postiz_agent.hub_models import Dispatch, TargetApproval, Variant
from max_postiz_agent.human_review import rejected_evidence
from test_article_quality import Model, ORIGINAL, TITLE, TRANSLATION
from test_hourly_human_review import queued
from test_machine_exception_approval import NOTES

pytest_plugins = ['test_hourly_human_review']


def prepare_refusal(human_cycle):
    value, core, article, _, _, _, _ = human_cycle
    article.update(title=TITLE, original=ORIGINAL)
    def factory():
        return ArticleQuality(Model(TRANSLATION), Model({'approved':False,'topic':'science',
            'issues':['В тексте якобы потеряно число, хотя оно сохранено.']}),
            allow_human_rejection=True, trace=value._trace_writer(value._load()['records'][-1]['id']),
            transport=httpx.MockTransport(lambda _: httpx.Response(200,json={'matches':[]})))
    value.quality_factory=factory
    return value.prepare(article)


def test_new_negative_draft_requires_explicit_analysis_and_exact_exception(human_cycle):
    _, core, _, records, calls, _, _ = human_cycle
    prepared=prepare_refusal(human_cycle)
    detail=core.hourly_review_details(prepared['variant_id'])
    assert detail['machine_review_status']=='rejected' and detail['machine_review_current']
    assert detail['machine_exception_eligible'] and len(calls)==1
    assert records[prepared['postiz_id']]['state']=='DRAFT'
    with core.db.session() as session:
        assert session.query(TargetApproval).count()==session.query(Dispatch).count()==0
    revision,hashed,due=queued(human_cycle,prepared)
    with pytest.raises(AgentError,match='ARTICLE_MACHINE_EXCEPTION_CONFIRMATION_REQUIRED'):
        core.approve_variant(prepared['variant_id'],revision,hashed,due,actor='owner',human_confirmed=True)
    ident=core.approve_variant(prepared['variant_id'],revision,hashed,due,actor='owner',human_confirmed=True,
        machine_exception_confirmed=True,machine_review_notes=NOTES)
    with core.db.session() as session:
        variant=session.get(Variant,prepared['variant_id'])
        dispatch=session.get(Dispatch,ident)
        assert rejected_evidence(session,variant) is not None
        assert variant.metadata_json['machine_review_status']=='rejected'
        core._valid_dispatch(session,dispatch)


def test_negative_preparation_cannot_be_resampled_through_recheck(human_cycle,monkeypatch):
    value,core,_,_,_,_,_=human_cycle
    prepared=prepare_refusal(human_cycle)
    detail=core.hourly_review_details(prepared['variant_id'])
    monkeypatch.setattr(value,'_quality',lambda *a,**kw:pytest.fail('No new review of identical refusal'))
    with pytest.raises(AgentError,match='ARTICLE_CANDIDATE_PREVIOUSLY_REJECTED'):
        core.recheck_hourly_variant(prepared['variant_id'],detail['revision'],detail['content_hash'])
    assert core.hourly_review_details(prepared['variant_id'])==detail


@pytest.mark.parametrize('field',['review_raw','text_hash','source_hash','hard_gates_passed','spelling_unresolved'])
def test_exception_proof_tampering_is_blocked(human_cycle,field):
    _,core,_,_,_,_,_=human_cycle
    prepared=prepare_refusal(human_cycle)
    with core.db.session.begin() as session:
        v=session.get(Variant,prepared['variant_id'])
        metadata=json.loads(json.dumps(v.metadata_json))
        metadata['machine_review_evidence'][field]=None
        v.metadata_json=metadata
    assert not core.hourly_review_details(prepared['variant_id'])['machine_exception_eligible']
    revision,hashed,due=queued(human_cycle,prepared)
    with pytest.raises(AgentError,match='ARTICLE_MACHINE_RECHECK_REQUIRED'):
        core.approve_variant(prepared['variant_id'],revision,hashed,due,actor='owner',human_confirmed=True,
            machine_exception_confirmed=True,machine_review_notes=NOTES)
