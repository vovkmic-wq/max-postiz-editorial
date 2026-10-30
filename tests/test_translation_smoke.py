"""Negative controls must not pass because a reviewer was unavailable or malformed."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def smoke():
    path = Path(__file__).resolve().parents[1] / 'deployment/translation/smoke_quality.py'
    spec = importlib.util.spec_from_file_location('translation_smoke_under_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('row,expected', [
    ({'status': 'accepted', 'trace': []}, 'critical_false_claim_approved'),
    ({'status': 'rejected_or_failed', 'error': 'ARTICLE_SPELLING_UNAVAILABLE',
      'trace': []}, 'inconclusive_negative_check'),
    ({'status': 'rejected_or_failed', 'error': 'ARTICLE_QUALITY_REJECTED',
      'trace': [{'phase': 'review_invalid'}]}, 'inconclusive_negative_check'),
    ({'status': 'rejected_or_failed', 'error': 'ARTICLE_QUALITY_REJECTED',
      'trace': [{'phase': 'review_parsed', 'review': {
          'approved': False, 'topic': 'science', 'issues': ['Changed negation']}}]},
     'passed_negative_blocked'),
    ({'status': 'rejected_or_failed', 'error': 'ARTICLE_QUALITY_REJECTED',
      'trace': [{'phase': 'review_parsed', 'review': {
          'approved': True, 'topic': 'science', 'issues': []}}]},
     'critical_false_claim_approved'),
])
def test_negative_control_classification_is_evidence_based(smoke, row, expected):
    assert smoke.classify_check(row, negative=True) == expected


def test_critical_failure_survives_a_later_optional_article_error(smoke, tmp_path, monkeypatch):
    cases = tmp_path / 'deployment/translation'
    cases.mkdir(parents=True)
    (cases / 'cases.json').write_text(json.dumps({'cases': [{
        'id': 'controlled-negation-modality', 'text': 'The spacecraft did not detect life.'}]}))
    monkeypatch.setattr(smoke, 'load_env', lambda _: None)
    monkeypatch.setattr(smoke, 'Settings', SimpleNamespace(load=lambda _: SimpleNamespace(root=tmp_path)))
    client = SimpleNamespace(close=lambda: None)
    translator = SimpleNamespace(model='hy-mt2-1.8b-q8', client=client,
        generate_text=lambda _: (json.dumps({'title': 'Наблюдения аппарата',
            'body': 'Космический аппарат не обнаружил жизнь.'}), {}))
    monkeypatch.setattr(smoke, 'configured_translator', lambda _: translator)
    monkeypatch.setattr(smoke, 'OpenCode', lambda _: SimpleNamespace(client=client))
    calls = []

    class Quality:
        def __init__(self, *args, **kwargs):
            self.trace = kwargs['trace']

        def check_candidate(self, title, body, original_title, original, url):
            calls.append((title, body, original_title, original, url))
            self.trace({'phase': 'review_returned', 'raw': '{"approved":true}'})
            self.trace({'phase': 'review_parsed', 'review': {
                'approved': True, 'topic': 'science', 'issues': []}})
            return {'body': body}

        def close(self):
            pass

    monkeypatch.setattr(smoke, 'ArticleQuality', Quality)
    assert smoke.main(['--root', str(tmp_path), '--full-article']) == 2
    report = json.loads((tmp_path / 'data/translation-benchmark/quality-smoke.json').read_text(encoding='utf-8'))
    assert len(calls) == 2 and calls[0][2:] == calls[1][2:]
    assert calls[1][1] == smoke.FALSE_TRANSLATION
    assert report['status'] == 'critical_failure'
    assert report['setup_or_execution_error'] == 'FileNotFoundError'
    assert len(report['checks']) == 2 and report['checks'][1]['trace']


def previous_report(smoke, source='The spacecraft did not detect life.'):
    candidate = {'title': 'Наблюдения аппарата', 'body': 'Космический аппарат не обнаружил жизнь.'}
    return {'schema_version': 1, 'translation_model': 'hy-mt2-1.8b-q8', 'translations': [{
        'case': 'controlled-negation-modality', 'status': 'translated',
        'source_untrusted': {'title': smoke.CONTROLLED_TITLE, 'body': source},
        'candidate': candidate, 'raw_output': json.dumps(candidate),
        'metrics': {'model': 'hy-mt2-1.8b-q8', 'segments': []}, 'seconds': 6.1}]}


@pytest.mark.parametrize('change', ['schema', 'source', 'title', 'status', 'raw', 'model', 'duplicate'])
def test_reuse_rejects_inexact_source_or_invalid_report_schema(smoke, tmp_path, change):
    from max_postiz_agent.config import AgentError

    previous = previous_report(smoke)
    row = previous['translations'][0]
    if change == 'schema':
        previous['schema_version'] = True
    elif change in {'source', 'title'}:
        row['source_untrusted']['body' if change == 'source' else 'title'] += ' '
    elif change == 'status':
        row['status'] = 'failed'
    elif change == 'raw':
        row['raw_output'] = '{}'
    elif change == 'model':
        row['metrics']['model'] = 'another-model'
    else:
        previous['translations'].append(row.copy())
    path = tmp_path / 'previous.json'
    path.write_text(json.dumps(previous), encoding='utf-8')
    report = smoke.PrivateReport(tmp_path, False, 'comparison.json')
    with pytest.raises(AgentError, match='TRANSLATION_REUSE_INVALID'):
        smoke.reuse_translation(report, path, 'controlled-negation-modality', smoke.CONTROLLED_TITLE,
                                'The spacecraft did not detect life.')
    assert not report.value['translations']


def test_reuse_runs_alternative_reviewer_on_same_candidate_without_generation(smoke, tmp_path, monkeypatch):
    from max_postiz_agent.config import AgentError

    previous = previous_report(smoke)
    source = previous['translations'][0]['source_untrusted']['body']
    path = tmp_path / 'data/translation-benchmark/baseline.json'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(previous), encoding='utf-8')
    original_bytes = path.read_bytes()
    cases = tmp_path / 'deployment/translation'
    cases.mkdir(parents=True)
    (cases / 'cases.json').write_text(json.dumps({'cases': [{
        'id': 'controlled-negation-modality', 'text': source}]}))
    monkeypatch.setattr(smoke, 'load_env', lambda _: None)
    monkeypatch.setattr(smoke, 'Settings', SimpleNamespace(load=lambda _: SimpleNamespace(root=tmp_path)))
    client = SimpleNamespace(close=lambda: None)
    translator = SimpleNamespace(model='hy-mt2-1.8b-q8', client=client,
        generate_text=lambda _: pytest.fail('A reused candidate must never generate again'))
    monkeypatch.setattr(smoke, 'configured_translator', lambda _: translator)
    monkeypatch.setattr(smoke, 'OpenCode', lambda _: SimpleNamespace(client=client))
    calls = []

    class Quality:
        def __init__(self, translator, reviewer, **kwargs):
            self.trace, self.reviewer = kwargs['trace'], reviewer

        def check_candidate(self, title, body, original_title, original, url):
            calls.append((title, body, original_title, original, self.reviewer.model))
            approved = body != smoke.FALSE_TRANSLATION
            self.trace({'phase': 'review_parsed', 'review': {
                'approved': approved, 'topic': 'science', 'issues': [] if approved else ['Invented life']}})
            if not approved:
                raise AgentError('ARTICLE_QUALITY_REJECTED')
            return {'body': body}

        def close(self):
            pass

    monkeypatch.setattr(smoke, 'ArticleQuality', Quality)
    assert smoke.main(['--root', str(tmp_path), '--review-model', 'proxmox/qwen35-9b',
        '--output-name', 'comparison.json', '--reuse-translation-report',
        'data/translation-benchmark/baseline.json']) == 0
    assert path.read_bytes() == original_bytes
    assert len(calls) == 2 and calls[0][0:2] == tuple(previous['translations'][0]['candidate'].values())
    assert calls[0][2:4] == calls[1][2:4]
    assert all(row[4] == 'proxmox/qwen35-9b' for row in calls)
    report = json.loads((path.parent / 'comparison.json').read_text(encoding='utf-8'))
    assert report['review_model'] == 'proxmox/qwen35-9b' and report['reused_source_candidate'] is True
    assert report['translations'][0]['generated_in_this_run'] is False
    assert report['translations'][0]['reuse_trace'][0]['exact_source_and_raw_candidate_validated'] is True
    assert report['translations'][0]['original_translation_seconds'] == 6.1


def test_reuse_source_cannot_be_overwritten_by_output_report(smoke, tmp_path):
    path = tmp_path / 'data/translation-benchmark/quality-smoke.json'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(previous_report(smoke)), encoding='utf-8')
    original_bytes = path.read_bytes()
    with pytest.raises(SystemExit):
        smoke.main(['--root', str(tmp_path), '--reuse-translation-report', str(path)])
    assert path.read_bytes() == original_bytes


@pytest.mark.parametrize('name', ['../outside.json', '/outside.json', 'sub/file.json',
                                  'sub\\file.json', 'outside.txt', '.json'])
def test_output_name_cannot_escape_private_report_directory(smoke, tmp_path, name):
    with pytest.raises(SystemExit):
        smoke.main(['--root', str(tmp_path), '--output-name', name])
    assert not (tmp_path / 'data').exists()
