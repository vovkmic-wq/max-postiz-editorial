"""The test cascade never resamples unknown outcomes or enables production."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from max_postiz_agent.config import AgentError


@pytest.fixture
def cascade():
    path = Path(__file__).resolve().parents[1] / 'deployment/translation/benchmark_cascade.py'
    spec = importlib.util.spec_from_file_location('translation_cascade_under_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def row(ident='first', translated='Первый перевод.'):
    return {'id': ident, 'source': 'A source without numerical values.',
            'translated': translated, 'seconds': 0.1, 'repeat': 1}


def report(cascade, tmp_path, primary=None, contract=None, resume=False):
    primary = primary or row()
    return cascade.Report(tmp_path / 'data/result.json', contract or {'argos_model': 'argos-test'},
        {'primary': {primary['id']: primary}, 'repeats': []}, resume)


def reviewer(cascade, statuses, calls):
    def generate(prompt):
        payload = json.loads(prompt)
        calls.append(payload)
        status = statuses[min(len(calls) - 1, len(statuses) - 1)]
        if isinstance(status, BaseException):
            raise status
        if isinstance(status, str) and status.startswith('{'):
            return status, {}
        return json.dumps({'verified': status != 'unverified', 'approved': status == 'approved',
            'issues': [] if status == 'approved' else ['Конкретное несоответствие исходному тексту.']}), {}
    return SimpleNamespace(model=cascade.REVIEW_MODEL, generate_text=generate)


def fallback(calls, text='Другой перевод.'):
    def translate(source):
        calls.append(source)
        if isinstance(text, BaseException):
            raise text
        return text, [{'seconds': 0.3}]
    return SimpleNamespace(translate=translate)


def test_approved_argos_does_not_translate_hy_and_reviewer_is_blind(cascade, tmp_path):
    result = report(cascade, tmp_path)
    review_calls, hy_calls = [], []
    output = cascade.run_case(result, reviewer(cascade, ['approved'], review_calls),
        fallback(hy_calls), 'Independent instruction.', row())
    assert output['status'] == 'argos_review_accepted' and not hy_calls
    assert set(review_calls[0]) == {'instruction', 'source_untrusted', 'translation_untrusted'}
    assert not any(key in review_calls[0] for key in ('reference', 'translator', 'required_facts'))


@pytest.mark.parametrize('initial', ['rejected', 'unverified', AgentError('OPENCODE_GENERATION_FAILED'),
    '{"verified":true,"approved":true,"issues":["Ошибка"]}'])
def test_reject_error_or_unverified_uses_one_real_fallback_then_one_review(cascade, tmp_path, initial):
    result = report(cascade, tmp_path)
    review_calls, hy_calls = [], []
    output = cascade.run_case(result, reviewer(cascade, [initial, 'approved'], review_calls),
        fallback(hy_calls), 'Prompt.', row())
    assert output['status'] == 'hy7b_review_accepted'
    assert len(hy_calls) == 1 and len(review_calls) == 2


def test_second_rejection_requires_editor_without_resampling(cascade, tmp_path):
    result = report(cascade, tmp_path)
    review_calls, hy_calls = [], []
    model = reviewer(cascade, ['rejected', 'rejected'], review_calls)
    output = cascade.run_case(result, model, fallback(hy_calls), 'Prompt.', row())
    assert output['status'] == 'needs_editor'
    cascade.run_case(result, model, fallback(hy_calls), 'Prompt.', row())
    assert len(hy_calls) == 1 and len(review_calls) == 2


def test_identical_hy_candidate_reuses_prior_refusal(cascade, tmp_path):
    result = report(cascade, tmp_path)
    review_calls, hy_calls = [], []
    output = cascade.run_case(result, reviewer(cascade, ['rejected', 'approved'], review_calls),
        fallback(hy_calls, row()['translated']), 'Prompt.', row())
    assert output['status'] == 'needs_editor' and len(review_calls) == 1
    assert output['hy7b']['review']['cache_reused'] is True
    cascade.summarize(result, ['first'])
    review_seconds = output['argos']['review']['seconds']
    expected = 0.1 + output['hy7b']['translation_seconds'] + review_seconds
    assert output['measured_component_sum_seconds'] == pytest.approx(expected, abs=0.000001)


def test_resume_unknown_argos_review_does_not_repeat_call(cascade, tmp_path):
    result = report(cascade, tmp_path)
    review_calls, hy_calls = [], []
    model = reviewer(cascade, [KeyboardInterrupt(), 'approved'], review_calls)
    with pytest.raises(KeyboardInterrupt):
        cascade.run_case(result, model, fallback(hy_calls), 'Prompt.', row())
    resumed = report(cascade, tmp_path, resume=True)
    output = cascade.run_case(resumed, model, fallback(hy_calls), 'Prompt.', row())
    assert len(review_calls) == 2 and len(hy_calls) == 1
    assert output['argos']['review']['error'] == 'REVIEW_OUTCOME_UNKNOWN_NO_RESAMPLE'
    assert output['status'] == 'hy7b_review_accepted'


def test_resume_unknown_hy_translation_does_not_replay(cascade, tmp_path):
    result = report(cascade, tmp_path)
    review_calls, hy_calls = [], []
    model = reviewer(cascade, ['rejected'], review_calls)
    with pytest.raises(KeyboardInterrupt):
        cascade.run_case(result, model, fallback(hy_calls, KeyboardInterrupt()), 'Prompt.', row())
    resumed = report(cascade, tmp_path, resume=True)
    output = cascade.run_case(resumed, model, fallback(hy_calls), 'Prompt.', row())
    assert len(review_calls) == len(hy_calls) == 1
    assert output['status'] == 'needs_editor'
    assert output['hy7b']['error'] == 'TRANSLATION_OUTCOME_UNKNOWN_NO_REPLAY'


def test_changed_contract_or_tampered_candidate_is_rejected_on_resume(cascade, tmp_path):
    result = report(cascade, tmp_path)
    calls = []
    cascade.run_case(result, reviewer(cascade, ['approved'], calls), fallback([]), 'Prompt.', row())
    with pytest.raises(AgentError, match='CONTRACT_MISMATCH'):
        report(cascade, tmp_path, resume=True, contract={'argos_model': 'changed'})
    result.value['results']['first']['argos']['output'] = 'Изменённый кандидат.'
    result.save()
    with pytest.raises(AgentError, match='CANDIDATE_MISMATCH'):
        cascade.run_case(report(cascade, tmp_path, resume=True),
            reviewer(cascade, ['approved'], calls), fallback([]), 'Prompt.', row())
    assert len(calls) == 1


def input_files(cascade, tmp_path):
    corpus = json.loads((Path(__file__).resolve().parents[1] /
                        'deployment/translation/cases.json').read_text(encoding='utf-8'))
    packet = {'schema_version': 1, 'status': 'complete', 'model': 'argos-test',
        'engine': 'argos-ctranslate2', 'cases': [{'id': case['id'], 'source': case['text'],
            'translated': 'Проверяемый перевод.', 'seconds': 0.1, 'repeat': 1}
            for case in corpus['cases']]}
    packet['cases'] += [{**sample, 'repeat': 2} for sample in packet['cases'][-2:]]
    corpus_path, argos_path = tmp_path / 'corpus.json', tmp_path / 'argos.json'
    corpus_path.write_text(json.dumps(corpus), encoding='utf-8')
    argos_path.write_text(json.dumps(packet), encoding='utf-8')
    return corpus_path, argos_path, packet


@pytest.mark.parametrize('change', ['source', 'repeat', 'status', 'seconds', 'missing'])
def test_argos_contract_rejects_inexact_or_incomplete_inputs(cascade, tmp_path, change):
    corpus_path, argos_path, packet = input_files(cascade, tmp_path)
    if change == 'source':
        packet['cases'][0]['source'] += ' '
    elif change == 'repeat':
        packet['cases'][-1]['repeat'] = 1
    elif change == 'status':
        packet['status'] = 'running'
    elif change == 'seconds':
        packet['cases'][0]['seconds'] = float('nan')
    else:
        packet['cases'].pop(0)
    argos_path.write_text(json.dumps(packet), encoding='utf-8')
    with pytest.raises(AgentError, match='CASCADE_INPUT_INVALID'):
        cascade.load_inputs(corpus_path, argos_path)


def test_two_repeated_argos_samples_are_timing_only(cascade, tmp_path):
    corpus_path, argos_path, _ = input_files(cascade, tmp_path)
    inputs = cascade.load_inputs(corpus_path, argos_path)
    assert len(inputs['primary']) == 7 and len(inputs['repeats']) == 2 and len(inputs['order']) == 7
    result = cascade.Report(tmp_path / 'report.json', {'argos_model': 'argos-test'}, inputs, False)
    assert len(result.value['argos_speed_samples']) == 9
    assert result.value['review_budget']['repeat2_reviewed'] is False


def test_critical_control_is_retained_but_corpus_collection_continues(cascade, tmp_path):
    result = report(cascade, tmp_path)
    corpus = {'cases': [{'id': 'controlled-negation-modality', 'text': row()['source'],
                        'reference_ru': 'Достоверный буквальный перевод.'}]}
    calls, hy_calls = [], []
    model = reviewer(cascade, ['approved'], calls)
    cascade.run_controls(result, model, 'New independent prompt.', corpus)
    output = cascade.run_case(result, model, fallback(hy_calls), 'New independent prompt.', row())
    assert output['status'] == 'argos_review_accepted' and len(calls) == 3
    assert cascade.summarize(result, ['first']) is True
    assert result.value['summary']['critical_false_control_approved'] is True
    assert output['semantic_eligible'] is False
    assert result.value['automatic_enable_performed'] is False


def test_unit_and_negation_lexical_disagreement_are_only_advisory(cascade):
    value = cascade.lexical_checks('The distance may be 1.5 km.', 'Расстояние может быть 1.5 км.')
    assert value['numeric_multiset_match'] is True
    assert value['units_lexical_advisory']['source']['km'] == 1
    assert value['units_lexical_advisory']['translation']['km'] == 1
    assert value['lexical_counts_establish_semantic_fidelity'] is False
