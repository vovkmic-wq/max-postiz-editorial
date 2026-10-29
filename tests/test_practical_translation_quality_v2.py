"""Observed LT envelope structure with synthetic text, independent of old articles."""
import copy
import hashlib
import json
from pathlib import Path

import httpx
import pytest

from max_postiz_agent.article_quality import (
    ArticleQuality, HOURLY_PRACTICAL_REVIEW_RUBRIC, HOURLY_PRACTICAL_V2_REVIEW_RUBRIC,
    MEANING_FIRST_REVIEW_RUBRIC, normalize_rubric_review, review_prompt_name,
    rubric_spelling, rubric_translation_gate,
)
from max_postiz_agent.automatic_policy import _validate
from max_postiz_agent.config import AgentError
from test_practical_translation_quality import APPROVED, Model, ORIGINAL, RU, TITLE, URL, finding

ROOT = Path(__file__).resolve().parents[1]
V2 = HOURLY_PRACTICAL_V2_REVIEW_RUBRIC
CAPTURED = json.loads((ROOT / 'tests/fixtures/practical_lt_uncategorized.json').read_text(encoding='utf-8'))


@pytest.fixture
def prompts(tmp_path):
    for name in ('ARTICLE_TRANSLATE.txt', review_prompt_name(V2)):
        (tmp_path / name).write_bytes((ROOT / 'prompts' / name).read_bytes())
    return tmp_path


def quality(prompts, matches, review=None, status=200, events=None):
    return ArticleQuality(Model(RU), Model(APPROVED if review is None else review),
        review_rubric=V2, prompt_dir=prompts, trace=None if events is None else events.append,
        transport=httpx.MockTransport(lambda _: httpx.Response(status, json={'matches': matches})))


@pytest.mark.parametrize('captured', CAPTURED, ids=lambda x: x['name'])
def test_captured_language_findings_are_advice_only_in_v2(captured):
    match = copy.deepcopy(captured['match'])
    unchanged = copy.deepcopy(match)
    advice, blocking = rubric_spelling('', captured['text'], [match], V2)
    assert match == unchanged and blocking == [] and len(advice) == 1
    assert advice[0]['match'] == match and advice[0]['review_rubric'] == V2
    assert advice[0]['effective_issue_type'] == ('style' if match['rule']['category']['id'] == 'STYLE' else 'grammar')
    for old in (HOURLY_PRACTICAL_REVIEW_RUBRIC, MEANING_FIRST_REVIEW_RUBRIC, 'legacy'):
        assert rubric_spelling('', captured['text'], [match], old) == ([], [match])


@pytest.mark.parametrize(('category', 'kind'), [('GRAMMAR', 'grammar'), ('STYLE', 'style'), ('PUNCTUATION', 'typographical')])
def test_uncategorized_uses_only_explicit_category_mapping(category, kind):
    match = finding('uncategorized', category, 'Other')
    advice, blocking = rubric_spelling(ORIGINAL, RU['title'], [match], V2)
    assert blocking == [] and advice[0]['effective_issue_type'] == kind
    expected_count = 1 if kind == 'grammar' else 0
    assert advice[0]['readability_budget']['nonstyle_readability_findings'] == expected_count


@pytest.mark.parametrize('change', [
    {'rule': {'id': 'UNKNOWN', 'issueType': 'uncategorized', 'category': {'id': 'TYPOS'}}},
    {'rule': {'id': 'UNKNOWN', 'issueType': 'uncategorized', 'category': {'id': 'NEW_CATEGORY'}}},
    {'rule': {'id': 'UNKNOWN', 'issueType': 'uncategorized', 'category': {'id': 'grammar'}}},
    {'rule': {'id': '', 'issueType': 'uncategorized', 'category': {'id': 'STYLE'}}},
    {'rule': {'id': 'UNKNOWN', 'issueType': 'new_type', 'category': {'id': 'GRAMMAR'}}},
    {'rule': {}}, {'type': {'typeName': 'Unrecognized'}}, {'type': {}},
    {'offset': -1}, {'offset': True}, {'offset': 99999}, {'length': 0}, {'length': True},
])
def test_unsupported_uncategorized_payload_stays_blocking(change):
    match = {**finding('uncategorized', 'GRAMMAR', 'Other'), **change}
    assert rubric_spelling(ORIGINAL, RU['title'], [match], V2) == ([], [match])


def test_v2_utf16_bounds_reject_half_surrogate_and_accept_valid_offset():
    match = finding('uncategorized', 'STYLE', 'Other')
    match.update(offset=1, length=1)
    assert rubric_spelling('', '😀' + RU['body'], [match], V2) == ([], [match])
    match['offset'] = 2
    assert rubric_spelling('', '😀' + RU['body'], [match], V2)[1] == []


def test_v2_budget_still_blocks_dense_grammar_but_never_counts_style_or_punctuation():
    optional = [finding('uncategorized', 'STYLE', 'Other') for _ in range(20)]
    optional += [finding('uncategorized', 'PUNCTUATION', 'Other') for _ in range(20)]
    grammar = [finding('uncategorized', 'GRAMMAR', 'Other') for _ in range(9)]
    advice, blocking = rubric_spelling('', RU['title'], optional + grammar, V2)
    assert len(advice) == 40 and blocking == grammar
    assert all(a['readability_budget']['nonstyle_readability_findings'] == 9 for a in advice)
    assert rubric_spelling('', RU['title'], grammar[:8], V2)[1] == []


def test_v2_evidence_retains_original_payload_raw_and_replays_at_approval(prompts):
    matches = [finding('uncategorized', 'GRAMMAR', 'Other'), finding('uncategorized', 'STYLE', 'Other')]
    before = copy.deepcopy(matches)
    events = []
    checker = quality(prompts, matches, events=events)
    try:
        prepared = checker.check_candidate(**RU, original_title=TITLE, original=ORIGINAL, url=URL)
    finally:
        checker.close()
    evidence = {**prepared['machine_review_evidence'], 'trace_hash': '0' * 64}
    assert prepared['review_rubric'] == V2 and prepared['machine_review_status'] == 'passed'
    assert matches == before and evidence['spelling_matches'] == before
    assert evidence['spelling_unresolved'] == [] and json.loads(evidence['review_raw']) == APPROVED
    assert next(e for e in events if e['phase'] == 'spelling_returned')['matches'] == before
    assert _validate(evidence, prepared['title'], prepared['body'],
        {'title': TITLE, 'body': ORIGINAL, 'url': URL})['approved'] is True
    old_evidence = {**evidence, 'review_rubric': HOURLY_PRACTICAL_REVIEW_RUBRIC}
    with pytest.raises(AgentError, match='ARTICLE_AUTOMATIC_EVIDENCE_INVALID'):
        _validate(old_evidence, prepared['title'], prepared['body'], {'title': TITLE, 'body': ORIGINAL, 'url': URL})


@pytest.mark.parametrize('issue', ['Главный объект изменён.', 'Потеряно отрицание not.', 'Пропущено существенное предложение о возрасте и массе звёзд.'])
def test_v2_language_advice_never_overrides_substantive_rejection(prompts, issue):
    checker = quality(prompts, [finding('uncategorized', 'STYLE', 'Other')],
        review={'approved': False, 'topic': 'science', 'issues': [issue], 'warnings': []})
    try:
        with pytest.raises(AgentError, match='ARTICLE_QUALITY_REJECTED'):
            checker.check_candidate(**RU, original_title=TITLE, original=ORIGINAL, url=URL)
    finally:
        checker.close()


@pytest.mark.parametrize(('source', 'translated', 'code'), [
    ('A telescope studied 3 stars.', 'Телескоп изучил 4 звезды.', 'TRANSLATION_NUMBER_CHANGED'),
    ('A telescope with a 2.4-meter mirror.', 'Телескоп с зеркалом 2,4 метра.', 'TRANSLATION_NUMBER_CHANGED'),
    ('A telescope studied the sky.', 'A telescope studied the sky.', 'TRANSLATION_NOT_RUSSIAN'),
    ('A telescope https://example.org/original studied the sky.', 'Телескоп изучил небо.', 'TRANSLATION_REFERENCE_CHANGED'),
])
def test_v2_does_not_change_factual_hard_gates(source, translated, code):
    with pytest.raises(AgentError, match=code):
        rubric_translation_gate(source, translated, V2, reference_contract='article-links-v2')


def test_v2_infrastructure_failure_and_contradictory_verdict_stay_blocking(prompts):
    checker = quality(prompts, [], status=503)
    try:
        with pytest.raises(AgentError, match='ARTICLE_SPELLING_UNAVAILABLE'):
            checker.check_candidate(**RU, original_title=TITLE, original=ORIGINAL, url=URL)
    finally:
        checker.close()
    with pytest.raises(AgentError, match='ARTICLE_REVIEW_SCHEMA_INVALID'):
        normalize_rubric_review({**APPROVED, 'issues': ['Отрицание изменено.']}, V2)


def test_v1_prompt_bytes_preserved_v2_adds_specific_content_and_negation_checks():
    old = (ROOT / 'prompts' / review_prompt_name(HOURLY_PRACTICAL_REVIEW_RUBRIC)).read_bytes()
    assert hashlib.sha256(old).hexdigest() == 'ce2a71ac2428566c2ac658f9fa8726006360bb312e46b4c6700252a7436339b0'
    new = (ROOT / 'prompts' / review_prompt_name(V2)).read_text(encoding='utf-8')
    assert 'whole omitted factual sentences' in new and 'not/no/without' in new
    assert 'minor grammar/spelling errors' in new and 'exactly four keys' in new
