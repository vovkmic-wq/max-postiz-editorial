"""The second policy experiment preserves the live baseline and exact reviews."""
import importlib.util
from pathlib import Path

import pytest

from max_postiz_agent.config import AgentError
from test_translation_cascade import fallback, reviewer


@pytest.fixture
def complex_cascade():
    path = Path(__file__).resolve().parents[1] / 'deployment/translation/benchmark_cascade_complex.py'
    spec = importlib.util.spec_from_file_location('cascade_complex_under_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def seeded(module, tmp_path, source='NASA observed a planet.', use_hy=False):
    base = module.BASE
    argos = {'id': 'first', 'source': source, 'translated': 'Перевод первого движка.',
             'seconds': 0.1, 'repeat': 1}
    inputs = {'primary': {'first': argos}, 'repeats': [], 'order': ['first'],
        'corpus_sha256': 'corpus-hash', 'argos_sha256': 'argos-hash', 'full_source_sha256': None,
        'packet': {'model': 'argos-test', 'engine': 'argos-ctranslate2'}}
    prompt_path = tmp_path / 'review.txt'
    prompt_path.write_text('Same frozen review prompt.', encoding='utf-8')
    prompt = prompt_path.read_text(encoding='utf-8')
    expected = module.expected_contract(inputs, prompt, prompt_path, 'http://127.0.0.1:8094', False)
    seed = base.Report(tmp_path / 'seed.json', expected, inputs, False)
    seed_calls, hy_calls = [], []
    base.run_case(seed, reviewer(base, ['rejected', 'approved'] if use_hy else ['approved'], seed_calls),
                  fallback(hy_calls), prompt, argos)
    base.summarize(seed, inputs['order'])
    seed.value['status'] = 'complete'
    seed.save()
    checked, sha = module.validate_seed(seed.path, expected, inputs)
    contract = {**expected, 'complexity_policy': 'length-acronyms-v1', 'seed_report_sha256': sha}
    experiment = base.Report(tmp_path / 'experiment.json', contract, inputs, False)
    module.seed_experiment(experiment, checked, seed.path, sha)
    return seed, experiment, inputs, expected, prompt, prompt_path


@pytest.mark.parametrize('source,forced', [
    ('a' * 349, False), ('a' * 350, True),
    ('NASA observed a planet.', True), ('ESCAPADE Theoretical Flight', True),
    ('AB observed a planet.', False), ('Nasa observed a planet.', False),
    ('Ordinary sentence without acronyms.', False), ('НАСА изучает планеты.', False),
])
def test_frozen_source_only_complexity_criteria(complex_cascade, source, forced):
    result = complex_cascade.complexity(source, 'length-acronyms-v1')
    assert result['force_hy'] is forced
    assert result['criteria']['uses_source_ids_or_gold_references'] is False
    assert complex_cascade.complexity(source, 'review-only')['force_hy'] is False


@pytest.mark.parametrize('source', ['NASA observed a planet.', 'ESCAPADE Theoretical Flight', 'a' * 350])
def test_complex_policy_forces_hy_without_rewriting_baseline_approval(complex_cascade, tmp_path, source):
    seed, experiment, inputs, _, prompt, prompt_path = seeded(complex_cascade, tmp_path, source)
    original_seed, original_prompt = seed.path.read_bytes(), prompt_path.read_bytes()
    review_calls, hy_calls = [], []
    output = complex_cascade.apply_policy(experiment,
        reviewer(complex_cascade.BASE, ['approved'], review_calls), fallback(hy_calls),
        prompt, inputs['primary']['first'], 'length-acronyms-v1')
    assert output['status'] == 'hy7b_review_accepted'
    assert output['argos']['review']['status'] == 'approved'
    assert output['baseline_selection']['status'] == 'argos_review_accepted'
    assert experiment.value['seed_baseline_outcomes']['first']['status'] == 'argos_review_accepted'
    assert len(hy_calls) == len(review_calls) == 1
    assert seed.path.read_bytes() == original_seed and prompt_path.read_bytes() == original_prompt
    complex_cascade.summarize(experiment, inputs['order'])
    summary = experiment.value['summary']
    assert summary['seed_review_count'] == summary['new_review_count'] == 1
    assert summary['experiment_is_post_baseline_not_blind'] is True
    assert experiment.value['blind_evaluation'] is False
    assert output['additional_measured_seconds_this_experiment'] == pytest.approx(
        output['hy7b']['translation_seconds'] + output['hy7b']['review']['seconds'], abs=0.000001)


def test_short_normal_text_stays_argos_without_additional_model_calls(complex_cascade, tmp_path):
    _, experiment, inputs, _, prompt, _ = seeded(complex_cascade, tmp_path,
                                                'An ordinary source sentence.')
    review_calls, hy_calls = [], []
    output = complex_cascade.apply_policy(experiment,
        reviewer(complex_cascade.BASE, ['approved'], review_calls), fallback(hy_calls),
        prompt, inputs['primary']['first'], 'length-acronyms-v1')
    assert output['status'] == 'argos_review_accepted' and not hy_calls and not review_calls
    assert output['argos']['review']['seed_reused'] is True
    complex_cascade.summarize(experiment, inputs['order'])
    assert experiment.value['summary']['new_review_count'] == 0
    assert output['additional_measured_seconds_this_experiment'] == 0


def test_existing_seed_hy_translation_and_review_are_reused(complex_cascade, tmp_path):
    seed, experiment, inputs, _, prompt, _ = seeded(complex_cascade, tmp_path, use_hy=True)
    original_hy = seed.value['results']['first']['hy7b']
    calls, hy_calls = [], []
    output = complex_cascade.apply_policy(experiment,
        reviewer(complex_cascade.BASE, ['approved'], calls), fallback(hy_calls),
        prompt, inputs['primary']['first'], 'length-acronyms-v1')
    assert output['status'] == 'hy7b_review_accepted' and not calls and not hy_calls
    assert output['hy7b']['output'] == original_hy['output']
    assert output['hy7b']['review']['cache_reused'] is True
    assert output['hy7b']['seed_reused_translation'] is True


@pytest.mark.parametrize('change', ['contract', 'source', 'argos_output', 'hy_output', 'running'])
def test_seed_validation_rejects_changed_bindings_or_unfinished_baseline(complex_cascade, tmp_path, change):
    seed, _, inputs, expected, _, _ = seeded(complex_cascade, tmp_path, use_hy=True)
    if change == 'contract':
        seed.value['contract']['review_prompt_sha256'] = 'changed'
    elif change == 'source':
        seed.value['results']['first']['source'] += ' '
    elif change == 'argos_output':
        seed.value['results']['first']['argos']['output'] = 'Иной перевод.'
    elif change == 'hy_output':
        seed.value['results']['first']['hy7b']['output'] = 'Иной перевод.'
    else:
        seed.value['status'] = 'partial'
    seed.save()
    with pytest.raises(AgentError, match='CASCADE_COMPLEX_SEED_INVALID'):
        complex_cascade.validate_seed(seed.path, expected, inputs)


def test_identical_hy_output_reuses_existing_exact_review_without_resampling(complex_cascade, tmp_path):
    _, experiment, inputs, _, prompt, _ = seeded(complex_cascade, tmp_path)
    review_calls, hy_calls = [], []
    output = complex_cascade.apply_policy(experiment,
        reviewer(complex_cascade.BASE, ['rejected'], review_calls),
        fallback(hy_calls, inputs['primary']['first']['translated']), prompt,
        inputs['primary']['first'], 'length-acronyms-v1')
    assert output['status'] == 'hy7b_review_accepted' and len(hy_calls) == 1 and not review_calls
    assert output['hy7b']['review']['cache_reused'] is True
    complex_cascade.summarize(experiment, inputs['order'])
    assert experiment.value['summary']['new_review_count'] == 0


@pytest.mark.parametrize('interrupt', ['translation', 'review'])
def test_resume_unknown_outcome_does_not_repeat_model_call(complex_cascade, tmp_path, interrupt):
    _, experiment, inputs, _, prompt, _ = seeded(complex_cascade, tmp_path)
    review_calls, hy_calls = [], []
    model = reviewer(complex_cascade.BASE, [KeyboardInterrupt(), 'approved'], review_calls)
    translator = fallback(hy_calls, KeyboardInterrupt() if interrupt == 'translation' else 'Другой перевод.')
    with pytest.raises(KeyboardInterrupt):
        complex_cascade.apply_policy(experiment, model, translator, prompt,
                                     inputs['primary']['first'], 'length-acronyms-v1')
    resumed = complex_cascade.BASE.Report(experiment.path, experiment.value['contract'], inputs, True)
    output = complex_cascade.apply_policy(resumed, model, fallback(hy_calls), prompt,
                                         inputs['primary']['first'], 'length-acronyms-v1')
    assert len(hy_calls) == 1 and len(review_calls) == (1 if interrupt == 'review' else 0)
    assert output['status'] == 'needs_editor'
    assert output['argos']['review']['status'] == 'approved'


def test_source_and_output_paths_must_differ_before_seed_is_opened(complex_cascade, tmp_path):
    target = tmp_path / 'data/translation-benchmark/seed.json'
    target.parent.mkdir(parents=True)
    target.write_text('UNTOUCHED', encoding='utf-8')
    with pytest.raises(SystemExit):
        complex_cascade.main(['--root', str(tmp_path), '--argos-results', 'argos.json',
            '--seed-report', str(target), '--output-name', 'seed.json'])
    assert target.read_text(encoding='utf-8') == 'UNTOUCHED'
