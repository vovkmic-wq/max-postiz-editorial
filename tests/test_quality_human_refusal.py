"""A valid refusal can reach a person without becoming a model pass."""
import json

import httpx
import pytest

from max_postiz_agent.article_quality import ArticleQuality
from max_postiz_agent.config import AgentError
from test_article_quality import APPROVAL, Model, ORIGINAL, TITLE, TRANSLATION, URL


def checker(review, *, matches=None, status=200):
    translator, reviewer, events = Model(TRANSLATION), Model(review), []
    quality = ArticleQuality(translator, reviewer, allow_human_rejection=True, trace=events.append,
        transport=httpx.MockTransport(lambda _: httpx.Response(status, json={"matches": matches or []})))
    return quality, translator, reviewer, events


@pytest.mark.parametrize("candidate", [False, True])
def test_exact_negative_result_is_preserved_without_sampling_again(candidate):
    review = {"approved": False, "topic": "science", "issues": ["Исходное замечание модели."]}
    quality, translator, reviewer, events = checker(review)
    try:
        result = (quality.check_candidate(TRANSLATION['title'], TRANSLATION['body'], TITLE, ORIGINAL, URL)
                  if candidate else quality.prepare(TITLE, ORIGINAL, URL))
    finally:
        quality.close()
    assert result['machine_review_status'] == 'rejected'
    evidence = result['machine_review_evidence']
    assert evidence['review'] == review and json.loads(evidence['review_raw']) == review
    assert evidence['spelling_unresolved'] == [] and evidence['hard_gates_passed'] is True
    assert events[-1]['phase'] == 'rejected_for_human'
    assert len(reviewer.prompts) == 1 and len(translator.prompts) == (0 if candidate else 1)


def test_pass_and_refusal_remain_distinct():
    quality, _, _, _ = checker(APPROVAL)
    try:
        result = quality.prepare(TITLE, ORIGINAL, URL)
    finally:
        quality.close()
    assert result['machine_review_status'] == 'passed'
    assert result['machine_review_evidence']['review']['approved'] is True


@pytest.mark.parametrize('review,matches,status', [
    ({'approved':False,'topic':'science','issues':[]}, [], 200),
    ({'approved':True,'topic':'science','issues':['Contradictory verdict']}, [], 200),
    ({'approved':False,'topic':'politics','issues':['No']}, [], 200),
    ({'approved':False,'topic':'science','issues':['No']}, [{'rule':{'id':'SPELL'}}], 200),
    ({'approved':False,'topic':'science','issues':['No']}, [], 503),
    ('malformed refusal', [], 200),
], ids=['empty-refusal','inconsistent','topic','spelling','infrastructure','malformed'])
def test_invalid_or_incomplete_checks_cannot_enter_exception(review, matches, status):
    quality, _, _, _ = checker(review, matches=matches, status=status)
    try:
        with pytest.raises(AgentError):
            quality.check_candidate(TRANSLATION['title'], TRANSLATION['body'], TITLE, ORIGINAL, URL)
    finally:
        quality.close()
