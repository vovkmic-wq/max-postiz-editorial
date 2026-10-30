"""Versioned model input; full raw evidence and old replay stay separate."""
import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select

from max_postiz_agent.article_quality import ArticleQuality, HOURLY_PRACTICAL_V2_REVIEW_RUBRIC
from max_postiz_agent.article_repair import (
    ArticleRepair, COMPACT_FEEDBACK_CONTRACT, COMPACT_REPAIR_PROMPT, MAX_COMPACT_PROMPT_BYTES,
    REPAIR_MODEL, compact_model_feedback, compact_repair_input, repair_context,
)
from max_postiz_agent.article_repair_cycle import AutomaticArticleRepair
from max_postiz_agent.automatic_policy import automatic_evidence
from max_postiz_agent.config import AgentError, fingerprint
from max_postiz_agent.db import Audit
from max_postiz_agent.hub_models import Dispatch, TargetApproval, Variant
from test_article_repair import ARTICLE, CANDIDATE, FEEDBACK, Editor, changed
from test_automatic_article_repair import CORRECT_BODY, SECOND_BODY
from test_automatic_policy import Model

pytest_plugins = ['test_hourly_reserve_controller']
ROOT = Path(__file__).resolve().parents[1]
INSTRUCTION = (ROOT / 'prompts' / COMPACT_REPAIR_PROMPT).read_text(encoding='utf-8')


def feedback(candidate=None):
    candidate = CANDIDATE if candidate is None else candidate
    text = candidate['title'] + '\n\n' + candidate['body']
    word = 'изучила' if 'изучила' in text else 'Учёные'
    offset = len(text[:text.index(word)].encode('utf-16-le')) // 2
    match = {'message': 'Неверное согласование', 'offset': offset, 'length': len(word),
        'replacements': [{'value': 'изучили'}, {'value': 'исследовали'}],
        'rule': {'id': 'GRAMMAR_RULE', 'issueType': 'uncategorized', 'category': {'id': 'GRAMMAR'}}}
    review = {'approved': False, 'topic': 'science', 'issues': ['Существенный факт пропущен.'],
        'warnings': ['Буквальный оборот допустим.']}
    diagnosis = {'review_approved': False, 'topic': 'science', 'review_issues': review['issues'], 'spelling_matches': [match]}
    return {'events': [
        {'phase': 'review_returned', 'raw': json.dumps(review)},
        {'phase': 'review_parsed', 'review': review},
        {'phase': 'spelling_returned', 'matches': [match, copy.deepcopy(match)]},
        {'phase': 'spelling_adjudicated', 'adjudicated': [], 'unresolved': [match, copy.deepcopy(match)]},
        {'phase': 'retry', 'feedback': diagnosis}, {'phase': 'rejected', 'feedback': diagnosis}]}


def test_compaction_preserves_every_finding_span_suggestion_and_review_without_raw_duplicates():
    full = feedback()
    before = copy.deepcopy(full)
    result = compact_model_feedback(full, CANDIDATE)
    assert full == before and len(result['reviews']) == len(result['blocking_language']) == 1
    assert result['reviews'][0] == full['events'][1]['review']
    finding = result['blocking_language'][0]
    assert finding['matched_text'] == 'изучила' and finding['message'] == 'Неверное согласование'
    assert finding['suggestions'] == full['events'][3]['unresolved'][0]['replacements']
    assert 'raw' not in result and 'spelling_returned' not in json.dumps(result)


def test_distinct_findings_and_long_messages_are_not_truncated_or_merged():
    full = feedback()
    extra = copy.deepcopy(full['events'][3]['unresolved'][0])
    extra.update(message='Полное замечание ' * 100, replacements=[{'value': 'полное предложение ' * 100}])
    full['events'][3]['unresolved'].append(extra)
    result = compact_model_feedback(full, CANDIDATE)
    assert len(result['blocking_language']) == 2
    assert result['blocking_language'][1]['message'] == extra['message']
    assert result['blocking_language'][1]['suggestions'] == extra['replacements']


def test_every_blocking_finding_beyond_terminal_twenty_item_preview_is_retained():
    full = feedback()
    template = full['events'][3]['unresolved'][0]
    all_matches = [{**copy.deepcopy(template), 'message': 'Полное замечание ' + str(i)} for i in range(25)]
    full['events'][3]['unresolved'] = all_matches
    for event in full['events'][4:]:
        event['feedback']['spelling_matches'] = all_matches[:20]
    result = compact_model_feedback(full, CANDIDATE)
    assert len(result['blocking_language']) == 25
    assert [x['message'] for x in result['blocking_language']] == [x['message'] for x in all_matches]


@pytest.mark.parametrize('missing', ['review_parsed', 'spelling_adjudicated'])
def test_unreconstructible_raw_finding_is_blocked_instead_of_dropped(missing):
    full = feedback()
    full['events'] = [e for e in full['events'] if e['phase'] != missing]
    with pytest.raises(AgentError, match='ARTICLE_REPAIR_FEEDBACK_INVALID'):
        compact_model_feedback(full, CANDIDATE)


def test_full_numbers_and_urls_remain_in_context_and_gate_feedback_once():
    full = feedback()
    diagnostic = {'code': 'TRANSLATION_NUMBER_CHANGED', 'missing_numbers': ['2.4'], 'unexpected_numbers': ['2,4']}
    full['events'] += [{'phase': 'gate_rejected', 'feedback': diagnostic}, {'phase': 'rejected', 'feedback': diagnostic}]
    article = {**ARTICLE, 'original': ARTICLE['original'] + ' See https://example.org/A_(B)?x=2#exact'}
    prepared = compact_repair_input(article, CANDIDATE, full, INSTRUCTION)
    context = prepared['context']
    assert context['source_untrusted']['body'] == article['original']
    assert context['candidate_untrusted'] == CANDIDATE
    assert 'https://example.org/A_(B)?x=2#exact' in context['reference_url_manifest']['required_urls']
    assert context['feedback_untrusted']['hard_gates'] == [diagnostic]
    assert context['raw_feedback_hash'] == fingerprint(full)


def test_old_v8_context_and_prompt_are_byte_exact_without_feedback_marker():
    context = repair_context(ARTICLE, CANDIDATE, FEEDBACK)
    assert context['feedback_untrusted'] == FEEDBACK and 'feedback_contract' not in context
    editor = Editor(changed())
    repair = ArticleRepair(editor)
    repair.propose(ARTICLE, CANDIDATE, FEEDBACK)
    old = (ROOT / 'prompts/ARTICLE_REPAIR_V8.txt').read_bytes()
    assert hashlib.sha256(old).hexdigest() == 'c11533e45e9fe2e0c7d12948deac6c54103240cf0dd946085d649d3137d7da98'
    assert editor.calls == [old.decode() + '\n\nUNTRUSTED_JSON_DATA:\n' + json.dumps(context, ensure_ascii=False)]


def test_compact_proposal_signs_brief_prompt_context_and_full_raw_trace():
    full = feedback()
    events = []
    editor = Editor(changed())
    repair = ArticleRepair(editor, trace=events.append)
    prepared = compact_repair_input(ARTICLE, CANDIDATE, full, INSTRUCTION)
    result = repair.propose(ARTICLE, CANDIDATE, full, feedback_contract=COMPACT_FEEDBACK_CONTRACT,
        model_input_proof=prepared['proof'])
    assert len(editor.calls) == 1 and editor.calls[0] == prepared['prompt']
    assert events[0]['raw_feedback'] == full and events[0]['context'] == prepared['context']
    assert events[0]['model_input'] == prepared['proof']
    assert result['evidence']['prompt_hash'] == hashlib.sha256(INSTRUCTION.encode()).hexdigest()
    assert result['evidence']['raw_feedback_hash'] == fingerprint(full)
    assert result['evidence']['model_input_hash'] == fingerprint(prepared['proof'])


def test_oversize_is_known_before_editor_call_without_truncating_content():
    full = feedback()
    full['events'][1]['review']['warnings'] = ['Полное предупреждение ' * 400]
    before = copy.deepcopy(full)
    prepared = compact_repair_input(ARTICLE, CANDIDATE, full, INSTRUCTION)
    assert not prepared['proof']['fits'] and prepared['proof']['prompt_bytes'] > MAX_COMPACT_PROMPT_BYTES
    editor = Editor(changed())
    with pytest.raises(AgentError, match='ARTICLE_REPAIR_MODEL_INPUT_BUDGET'):
        ArticleRepair(editor).propose(ARTICLE, CANDIDATE, full, feedback_contract=COMPACT_FEEDBACK_CONTRACT)
    assert editor.calls == [] and full == before


def test_changed_signed_model_input_blocks_before_editor_request():
    full = feedback()
    prepared = compact_repair_input(ARTICLE, CANDIDATE, full, INSTRUCTION)
    proof = {**prepared['proof'], 'prompt_sha256': '0' * 64}
    editor = Editor(changed())
    with pytest.raises(AgentError, match='ARTICLE_REPAIR_MODEL_INPUT_CHANGED'):
        ArticleRepair(editor).propose(ARTICLE, CANDIDATE, full,
            feedback_contract=COMPACT_FEEDBACK_CONTRACT, model_input_proof=proof)
    assert editor.calls == []


@pytest.mark.parametrize('change', [{'offset': -1}, {'offset': 100000}, {'length': 0}, {'offset': True}])
def test_invalid_language_span_is_not_silently_dropped(change):
    full = feedback()
    full['events'][3]['unresolved'][0].update(change)
    with pytest.raises(AgentError, match='ARTICLE_REPAIR_FEEDBACK_INVALID'):
        compact_model_feedback(full, CANDIDATE)


def test_brief_prompt_and_input_cap_have_fixed_bounded_contract():
    assert len(INSTRUCTION.encode()) <= 1200
    proof = compact_repair_input(ARTICLE, CANDIDATE, feedback(), INSTRUCTION)['proof']
    assert proof['max_prompt_bytes'] + proof['output_tokens'] + proof['chat_reserve_tokens'] + 1 == proof['context_tokens']
    assert proof['context_tokens'] == 16384 and proof['output_tokens'] == 4096


@pytest.fixture
def compact_controller(reserve_controller, monkeypatch):
    value = reserve_controller
    path = value.core.settings.root / 'data/hourly-article-policy.local.json'
    policy = json.loads(path.read_text())
    policy['review_rubric'] = HOURLY_PRACTICAL_V2_REVIEW_RUBRIC
    path.write_text(json.dumps(policy))
    controls = SimpleNamespace(done=False, oversize=False, error=None, output=None,
        require_second=False, qwen=0, minimum=0, lt=0, contexts=[])

    class Reviewer:
        model = 'ministral'
        immutable_candidates = True

        def __init__(self):
            self.client = self

        def close(self):
            pass

        def generate_text(self, prompt):
            controls.minimum += 1
            approved = controls.done and (not controls.require_second or controls.qwen >= 2)
            review = {'approved': approved, 'topic': 'science',
                'issues': [] if approved else ['The discovery of the stellar wind was omitted.'], 'warnings': []}
            if controls.oversize:
                review.update(issues=[str(i) + 'x' * 999 for i in range(3)],
                    warnings=[str(i) + 'w' * 999 for i in range(8)])
            return json.dumps(review), {}

    def spelling(_request):
        controls.lt += 1
        return httpx.Response(200, json={'matches': []})

    def quality(record_id=None, review_only=False):
        return ArticleQuality(Model({'title': 'Космический ветер',
            'body': 'Учёные изучили необычные процессы в атмосфере далёкой звезды.'}, 'hy-mt2-7b-q8'),
            Reviewer(), prompt_dir=value.core.settings.root / 'prompts',
            trace=value.cycle._trace_writer(record_id), review_rubric=HOURLY_PRACTICAL_V2_REVIEW_RUBRIC,
            transport=httpx.MockTransport(spelling))

    class CompactEditor:
        model = REPAIR_MODEL

        def generate_text(self, prompt):
            controls.qwen += 1
            controls.contexts.append(json.loads(prompt.split('UNTRUSTED_JSON_DATA:\n', 1)[1]))
            if controls.error:
                raise controls.error
            if controls.output is not None:
                return json.dumps(controls.output, ensure_ascii=False), {}
            controls.done = True
            body = (('Учёные изучили необычную атмосферу далёкой звезды.' if controls.qwen == 1 else SECOND_BODY)
                    if controls.require_second else CORRECT_BODY)
            return json.dumps({'title': 'Космический ветер', 'body': body}, ensure_ascii=False), {}

    monkeypatch.setattr(value.cycle, '_quality', quality)
    value.cycle.repair_factory = lambda trace: ArticleRepair(CompactEditor(),
        prompt_dir=value.core.settings.root / 'prompts', trace=trace)
    return value, controls


def test_full_v2_reserve_repair_uses_one_brief_editor_then_fresh_qa_and_signed_ready(compact_controller):
    value, controls = compact_controller
    value.controller.prepare_once()
    record = value.cycle._load()['records'][0]
    assert record['state'] == 'quality_ready' and controls.qwen == 1
    assert controls.minimum == controls.lt == 2 and not value.posts and not value.writes
    claim = record['automatic_repair_attempts'][0]
    assert claim['state'] == 'passed' and claim['feedback_contract'] == COMPACT_FEEDBACK_CONTRACT
    assert 'ARTICLE_REPAIR_V8.txt' in claim['prompts'] and COMPACT_REPAIR_PROMPT in claim['prompts']
    assert claim['evidence']['prompt_hash'] == claim['prompts'][COMPACT_REPAIR_PROMPT]
    snapshot = json.loads((value.core.settings.root / claim['snapshot_path']).read_text(encoding='utf-8'))
    events = [json.loads(line) for line in (value.core.settings.root / claim['repair_trace']).read_text(encoding='utf-8').splitlines()]
    assert events[0]['raw_feedback'] == snapshot['feedback']
    assert events[0]['model_input'] == snapshot['model_input'] == claim['model_input']
    assert events[0]['context'] == controls.contexts[0]
    with value.core.db.session() as session:
        assert automatic_evidence(session, session.get(Variant, record['variant_id'])) is not None
        assert session.query(Dispatch).count() == session.query(TargetApproval).count() == 0
    with value.core.db.session() as session:
        AutomaticArticleRepair(value.cycle).assert_complete(record, session)


def test_budget_skip_persists_full_snapshot_audit_and_native_cancel_without_editor_claim(compact_controller):
    value, controls = compact_controller
    controls.oversize = True
    value.controller.prepare_once()
    record = value.cycle._load()['records'][0]
    assert record['state'] == 'cancelled' and not record.get('automatic_repair_attempts')
    assert controls.qwen == 0 and controls.minimum == controls.lt == 1
    assert value.store.active_preparation() is None and not value.posts and not value.writes
    with value.core.db.session() as session:
        audit = session.scalar(select(Audit).where(Audit.action == 'automatic_article_repair_model_input_budget'))
        assert audit is not None and audit.details['model_request_sent'] is False
        snapshot = json.loads((value.core.settings.root / audit.details['snapshot_path']).read_text(encoding='utf-8'))
        assert not snapshot['model_input']['fits'] and snapshot['feedback']['events']
        assert len(snapshot['feedback']['events'][1]['review']['issues'][0]) == 1000
        assert session.scalar(select(Audit).where(Audit.action == 'automatic_article_repair_claim')) is None
        assert session.query(Dispatch).count() == session.query(TargetApproval).count() == 0
    value.controller.prepare_once()
    assert controls.qwen == 0 and controls.minimum == controls.lt == 1


def test_second_repair_binds_latest_rejected_candidate_and_refuses_initial_budget_proof(compact_controller):
    value, controls = compact_controller
    controls.require_second = True
    value.controller.prepare_once()
    record = value.cycle._load()['records'][0]
    assert record['state'] == 'quality_ready' and controls.qwen == 2
    assert controls.minimum == controls.lt == 3
    first, second = record['automatic_repair_attempts']
    assert first['state'] == 'rejected' and second['state'] == 'passed'
    before = json.loads((value.core.settings.root / first['snapshot_path']).read_text(encoding='utf-8'))
    after = json.loads((value.core.settings.root / second['snapshot_path']).read_text(encoding='utf-8'))
    assert before['candidate'] != after['candidate']
    assert after['candidate']['body'] == 'Учёные изучили необычную атмосферу далёкой звезды.'
    assert controls.contexts[1]['candidate_untrusted'] == after['candidate']
    assert first['parent_trace_hash'] != second['parent_trace_hash']
    assert first['model_input']['prompt_sha256'] != second['model_input']['prompt_sha256']
    editor = Editor({'title': 'Космический ветер', 'body': SECOND_BODY})
    with pytest.raises(AgentError, match='ARTICLE_REPAIR_MODEL_INPUT_CHANGED'):
        ArticleRepair(editor).propose(after['article'], after['candidate'], after['feedback'],
            feedback_contract=COMPACT_FEEDBACK_CONTRACT, model_input_proof=first['model_input'])
    assert editor.calls == []


@pytest.mark.parametrize('field', ['feedback_contract', 'raw_feedback_hash', 'model_input'])
def test_compact_claim_and_full_snapshot_cannot_disagree(compact_controller, field):
    value, _ = compact_controller
    value.controller.prepare_once()
    record = value.cycle._load()['records'][0]
    claim = copy.deepcopy(record['automatic_repair_attempts'][0])
    claim[field] = 'tampered'
    with pytest.raises(AgentError, match='ARTICLE_AUTOMATIC_REPAIR_INPUT_CHANGED'):
        AutomaticArticleRepair(value.cycle)._files(record, claim, value.article)


def test_compact_truthful_refusal_is_preserved_without_quality_promotion(compact_controller):
    value, controls = compact_controller
    controls.output = {'repairable': False, 'reason': 'Безопасная содержательная правка не обоснована оригиналом.'}
    value.controller.prepare_once()
    record = value.cycle._load()['records'][0]
    claim = record['automatic_repair_attempts'][0]
    assert record['state'] == 'cancelled' and claim['state'] == 'not_repairable'
    assert claim['reason'] == controls.output['reason'] and controls.qwen == 1
    assert controls.minimum == controls.lt == 1 and not value.posts


def test_compact_completed_protocol_failure_replays_exact_brief_binding(compact_controller):
    value, controls = compact_controller
    controls.output = {'invalid': 'Полный ответ сохранён.'}
    value.controller.prepare_once()
    record = value.cycle._load()['records'][0]
    claim = record['automatic_repair_attempts'][0]
    assert record['state'] == 'cancelled' and claim['state'] == 'not_repairable'
    assert claim['outcome_origin'] == 'protocol_guard' and controls.qwen == 1
    assert claim['error'] == 'ARTICLE_REPAIR_PATCH_INVALID' and not value.posts


@pytest.mark.parametrize(('claim_state', 'reason'), [('failed', 'FAILED'), ('unknown', 'FAILED'),
    ('correcting', 'IN_PROGRESS'), ('checking', 'IN_PROGRESS')])
def test_existing_failed_unknown_claim_blocks_before_budget_and_never_repeats_editor(compact_controller, monkeypatch, claim_state, reason):
    value, controls = compact_controller
    controls.error = AgentError('OPENCODE_PROVIDER_UNAVAILABLE', 503)
    with pytest.raises(AgentError, match='OPENCODE_PROVIDER_UNAVAILABLE'):
        value.controller.prepare_once()
    record = value.cycle._load()['records'][0]
    if claim_state != 'failed':
        ledger = value.cycle._load()
        ledger['records'][0]['automatic_repair_attempts'][0]['state'] = claim_state
        value.cycle._save(ledger)
        record = ledger['records'][0]
    monkeypatch.setattr(AutomaticArticleRepair, '_payload', lambda *_: pytest.fail('budget must not inspect failed claims'))
    assert value.cycle.automatic_repair_status(record)['reason'] == reason
    value.controller.prepare_once()
    assert controls.qwen == 1 and value.store.active_preparation()['state'] == 'blocked_unknown'
