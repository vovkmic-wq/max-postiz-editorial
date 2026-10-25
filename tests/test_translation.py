import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from max_postiz_agent.config import AgentError
from max_postiz_agent.translation import (
    DedicatedTranslator,
    MADLAD_ARTIFACT_SHA256,
    MADLAD_BACKEND_CONTRACT,
    MADLAD_MODEL,
    configured_translator,
    restore_numeric_spelling,
    segments,
    translation_messages,
)


def test_segments_retain_tail_and_paragraph_boundaries():
    text = ('A complete sentence. ' * 160) + '\n\nLAST PARAGRAPH.'
    pieces = segments(text)
    assert max(len(s) for s, flag in pieces if flag) <= 1400
    assert ''.join(s for s, _ in pieces) == text
    assert any(s == '\n\n' and not flag for s, flag in pieces)


def test_long_unbroken_input_fails_without_silent_truncation():
    with pytest.raises(AgentError, match='UNSPLITTABLE'):
        segments('x' * 2000)


def test_translation_wraps_plain_output_into_article_schema():
    seen = []
    def response(req):
        body = json.loads(req.content)
        seen.append(body)
        return httpx.Response(200,json={'model':body['model'],'complete':True,
            'text':'Заголовок' if len(seen)==1 else 'Полный перевод тела.', 'metrics':{'seconds':1}})
    translator = DedicatedTranslator({'model':'hy-mt2-1.8b-q8','base_url':'http://127.0.0.1:8094'},
        transport=httpx.MockTransport(response))
    try:
        result, info = translator.generate_text(json.dumps({'source_untrusted':{'title':'Title','body':'Original body.'}}))
    finally:
        translator.client.close()
    assert json.loads(result) == {'title':'Заголовок','body':'Полный перевод тела.'}
    assert [r['text'] for r in seen] == ['Title','Original body.']
    assert len(info['segments']) == 2


@pytest.mark.parametrize('response', [
    None, [], ['translation'], 'translation', 42, True,
    {'model':'hy-mt2-1.8b-q8','complete':False,'text':'Часть'},
    {'model':'another','complete':True,'text':'Перевод'},
    {'model':'hy-mt2-1.8b-q8','complete':True,'text':''},
])
def test_truncated_wrong_model_or_empty_response_rejected(response):
    translator = DedicatedTranslator({'model':'hy-mt2-1.8b-q8','base_url':'http://127.0.0.1:8094'},
        transport=httpx.MockTransport(lambda _:httpx.Response(200,content=json.dumps(response))))
    try:
        with pytest.raises(AgentError, match='RESPONSE_INVALID'):
            translator.translate('Hello.')
    finally:
        translator.client.close()


def test_config_off_preserves_existing_translator_and_bad_config_fails(tmp_path):
    settings = SimpleNamespace(root=tmp_path)
    assert configured_translator(settings) is None
    (tmp_path/'config').mkdir()
    p=tmp_path/'config/translation.json'
    p.write_text('{"enabled":false}')
    assert configured_translator(settings) is None
    p.write_text('{"enabled":true,"model":"unknown","base_url":"http://localhost"}')
    with pytest.raises(AgentError,match='CONFIG_INVALID'):
        configured_translator(settings)


def test_translategemma_uses_required_language_fields():
    content=translation_messages('translategemma-12b-q4','Hello.','unused')[0]['content'][0]
    assert content == {'type':'text','source_lang_code':'en','target_lang_code':'ru','text':'Hello.'}


@pytest.mark.parametrize('config', [
    None, [], {'model': [], 'base_url': 'http://localhost'},
    {'model': 'hy-mt2-1.8b-q8', 'base_url': 123},
    {'model': 'hy-mt2-1.8b-q8', 'base_url': 'http://[invalid'},
    {'model': 'hy-mt2-1.8b-q8', 'base_url': 'http://localhost:invalid'},
    {'model': 'hy-mt2-1.8b-q8', 'base_url': 'http://user:secret@localhost'},
    {'model': 'hy-mt2-1.8b-q8', 'base_url': 'http://localhost/?private=value'},
])
def test_invalid_config_has_a_stable_error(config):
    with pytest.raises(AgentError, match='TRANSLATION_CONFIG_INVALID'):
        DedicatedTranslator(config)


@pytest.mark.parametrize('raw', ['[]', 'null', '{"enabled":"false"}', '{broken'])
def test_config_invalid_json_types_do_not_enable_translation(tmp_path, raw):
    (tmp_path / 'config').mkdir()
    (tmp_path / 'config/translation.json').write_text(raw, encoding='utf-8')
    with pytest.raises(AgentError, match='TRANSLATION_CONFIG_INVALID'):
        configured_translator(SimpleNamespace(root=tmp_path))


@pytest.mark.parametrize('status,raw', [(200, 'not json'), (502, '{}')])
def test_service_protocol_failure_is_explicit(status, raw):
    translator = DedicatedTranslator(
        {'model': 'hy-mt2-1.8b-q8', 'base_url': 'http://127.0.0.1:8094'},
        transport=httpx.MockTransport(lambda _: httpx.Response(status, text=raw)))
    try:
        with pytest.raises(AgentError, match='TRANSLATION_SERVICE_UNAVAILABLE'):
            translator.translate('Hello.')
    finally:
        translator.client.close()


@pytest.mark.parametrize('enabled', [False, True])
def test_article_cycle_keeps_a_separate_reviewer(tmp_path, monkeypatch, enabled):
    from max_postiz_agent import article_cycle

    config_dir = tmp_path / 'config'
    config_dir.mkdir()
    (config_dir / 'translation.json').write_text(json.dumps({
        'enabled': enabled, 'model': 'hy-mt2-1.8b-q8',
        'base_url': 'http://127.0.0.1:8094'}), encoding='utf-8')
    (tmp_path / 'prompts').mkdir()
    for name in ('ARTICLE_TRANSLATE.txt', 'ARTICLE_REVIEW.txt'):
        (tmp_path / 'prompts' / name).write_text('Editorial instruction.', encoding='utf-8')

    class EditorialModel:
        def __init__(self, settings):
            self.client = httpx.Client(transport=httpx.MockTransport(
                lambda _: pytest.fail('Construction must not generate requests')))

    monkeypatch.setattr(article_cycle, 'OpenCode', EditorialModel)
    core = SimpleNamespace(settings=SimpleNamespace(root=tmp_path))
    cycle = article_cycle.HourlyArticleCycle(core)
    # Эта фикстура проверяет wiring, а не PostgreSQL: старый SimpleNamespace
    # не содержит обязательной БД нового серверного ledger.
    monkeypatch.setattr(cycle, '_load', lambda: {'records': []})
    check = cycle._quality()
    try:
        assert isinstance(check.translator, DedicatedTranslator) is enabled
        assert check.translator.model == ('hy-mt2-1.8b-q8' if enabled else 'proxmox/qwen35-9b')
        assert isinstance(check.reviewer, EditorialModel)
        assert check.reviewer.model == 'proxmox/qwen25-coder-7b'
        assert check.reviewer is not check.translator
    finally:
        check.close()
        check.translator.client.close()
        check.reviewer.client.close()


@pytest.mark.parametrize('approved', [False, True])
@pytest.mark.parametrize('model', ['hy-mt2-1.8b-q8', MADLAD_MODEL])
def test_dedicated_translation_obeys_independent_review_and_spelling(approved, model):
    from max_postiz_agent.article_quality import ArticleQuality

    source_title = 'A small telescope studies the night sky'
    source_body = ('A small telescope observed the night sky for 3 hours. Researchers studied '
                   'the light from a distant star and checked their observations before sharing '
                   'the results.')
    translated_title = 'Небольшой телескоп изучает ночное небо'
    translated_body = ('Небольшой телескоп наблюдал ночное небо 3 часа. Исследователи изучили '
                       'свет далёкой звезды и проверили наблюдения, прежде чем сообщить результаты.')
    calls, reviews, spelling = [], [], []

    def translate(request):
        value = json.loads(request.content)
        calls.append(value)
        text = translated_title if value['text'] == source_title else translated_body
        return httpx.Response(200, json={'model': value['model'], 'complete': True, 'text': text,
            **({'metrics': {'backend_contract': MADLAD_BACKEND_CONTRACT,
                            'artifact_sha256': MADLAD_ARTIFACT_SHA256}} if model == MADLAD_MODEL else {})})

    class Reviewer:
        model = 'independent-reviewer'

        def generate_text(self, prompt):
            reviews.append(json.loads(prompt))
            return json.dumps({'approved': approved, 'topic': 'science',
                               'issues': [] if approved else ['Changed meaning']}), {}

    def spell(request):
        spelling.append(request)
        return httpx.Response(200, json={'matches': []})

    translator = DedicatedTranslator(
        {'model': model, 'base_url': 'http://127.0.0.1:8094'},
        transport=httpx.MockTransport(translate))
    check = ArticleQuality(translator, Reviewer(), transport=httpx.MockTransport(spell),
        prompt_dir=Path(__file__).resolve().parents[1] / 'prompts')
    try:
        if approved:
            assert check.prepare(source_title, source_body, 'https://science.nasa.gov/example/')[
                'translation'] == translated_title + '\n\n' + translated_body
        else:
            with pytest.raises(AgentError, match='ARTICLE_QUALITY_REJECTED'):
                check.prepare(source_title, source_body, 'https://science.nasa.gov/example/')
    finally:
        check.close()
        translator.client.close()
    assert len(reviews) == len(spelling) == 1
    assert reviews[0]['source_untrusted'] == source_title + '\n\n' + source_body
    assert reviews[0]['translation_untrusted'] == translated_title + '\n\n' + translated_body
    assert len(calls) == 2


def test_repair_feedback_reuses_exact_translation_without_claiming_revision():
    calls = []

    def translate(request):
        value = json.loads(request.content)
        calls.append(value)
        return httpx.Response(200, json={'model': value['model'], 'complete': True,
            'text': 'Перевод ' + value['text'], 'metrics': {'seconds': 12.5}})

    translator = DedicatedTranslator(
        {'model': 'hy-mt2-1.8b-q8', 'base_url': 'http://127.0.0.1:8094'},
        transport=httpx.MockTransport(translate))
    prompt = {'source_untrusted': {'title': 'Title', 'body': 'Original body.'}}
    try:
        original, info = translator.generate_text(json.dumps(prompt))
        repaired, cached = translator.generate_text(json.dumps({**prompt,
            'repair_feedback': {'review_approved': False, 'review_issues': ['Changed meaning']}}))
        assert original == repaired
        assert info['cache_reused'] is False and cached['cache_reused'] is True
        assert info['segments'] == cached['segments']
        # Callers cannot alter the cached metrics accidentally.
        cached['segments'][0]['seconds'] = -1
        _, again = translator.generate_text(json.dumps(prompt))
        assert again['segments'][0]['seconds'] == 12.5
    finally:
        translator.client.close()
    assert len(calls) == 2


@pytest.mark.parametrize('field', ['title', 'body'])
def test_changed_source_invalidates_the_instance_cache(field):
    calls = []

    def translate(request):
        value = json.loads(request.content)
        calls.append(value)
        return httpx.Response(200, json={'model': value['model'], 'complete': True,
            'text': 'Перевод ' + value['text']})

    translator = DedicatedTranslator(
        {'model': 'hy-mt2-1.8b-q8', 'base_url': 'http://127.0.0.1:8094'},
        transport=httpx.MockTransport(translate))
    source = {'title': 'Title', 'body': 'Original body.'}
    changed = {**source, field: source[field] + ' Changed.'}
    try:
        first, _ = translator.generate_text(json.dumps({'source_untrusted': source}))
        second, info = translator.generate_text(json.dumps({'source_untrusted': changed}))
        third, final_info = translator.generate_text(json.dumps({'source_untrusted': source}))
    finally:
        translator.client.close()
    assert first != second and first == third
    assert info['cache_reused'] is False and final_info['cache_reused'] is False
    assert len(calls) == 6


def test_translation_cache_is_never_shared_between_instances():
    calls = []

    def translate(request):
        value = json.loads(request.content)
        calls.append(value)
        return httpx.Response(200, json={'model': value['model'], 'complete': True,
            'text': 'Перевод ' + value['text']})

    config = {'model': 'hy-mt2-1.8b-q8', 'base_url': 'http://127.0.0.1:8094'}
    first = DedicatedTranslator(config, transport=httpx.MockTransport(translate))
    second = DedicatedTranslator(config, transport=httpx.MockTransport(translate))
    prompt = json.dumps({'source_untrusted': {'title': 'Title', 'body': 'Original body.'}})
    try:
        _, first_info = first.generate_text(prompt)
        _, second_info = second.generate_text(prompt)
    finally:
        first.client.close()
        second.client.close()
    assert not first_info['cache_reused'] and not second_info['cache_reused']
    assert len(calls) == 4


@pytest.mark.parametrize('source,translated,expected,count', [
    ('A distance of 930,000 miles and 1.5 million km.',
     'Расстояние 930 000 миль и 1,5 миллиона км.',
     'Расстояние 930,000 миль и 1.5 миллиона км.', 2),
    ('The probe travelled 1,250 km.', 'Аппарат прошёл 1\u00a0250 км.',
     'Аппарат прошёл 1,250 км.', 1),
    ('The probe travelled 1,250 km.', 'Аппарат прошёл 1\u202f250 км.',
     'Аппарат прошёл 1,250 км.', 1),
    ('Two readings of 1.5 and 1.5 hours.', 'Два измерения: 1,5 и 1,5 часа.',
     'Два измерения: 1.5 и 1.5 часа.', 2),
    ('Measurements: 1.5, 2.5.', 'Измерения: 2,5 и 1,5.',
     'Измерения: 2.5 и 1.5.', 2),
    ('The temperature was -12.5 C.', 'Температура была −12,5 C.',
     'Температура была −12.5 C.', 1),
    ('The delta was +1.5.', 'Изменение было +1,5.', 'Изменение было +1.5.', 1),
    ('L2 is 930,000 miles away; ID-01 has value 1.5.',
     'L2 находится в 930 000 миль; значение ID-01 равно 1,5.',
     'L2 находится в 930,000 миль; значение ID-01 равно 1.5.', 2),
    ('The value is 9007199254740993.25.', 'Значение 9007199254740993,25.',
     'Значение 9007199254740993.25.', 1),
    ('The distance is 1,234.5 km.', 'Расстояние 1.234,5 км.',
     'Расстояние 1,234.5 км.', 1),
    ('A distance of 930,000 miles and 1.5 million km.',
     'Расстояние 930,000 миль и 1,5 миллиона км.',
     'Расстояние 930,000 миль и 1.5 миллиона км.', 1),
])
def test_numeric_spelling_restores_only_exact_equivalent_typography(source, translated, expected, count):
    assert restore_numeric_spelling(source, translated) == (expected, count)


@pytest.mark.parametrize('source,translated', [
    ('Readings: 1.5 and 2.5.', 'Измерение: 1,5.'),  # Missing value: do not fix even 1,5.
    ('Readings: 1.5.', 'Измерения: 1,5 и 2,5.'),
    ('Readings: 1.5 and 2.5.', 'Измерения: 1,5 и 2,6.'),
    ('Readings: 1.5 and 1.5.', 'Измерения: 1,5.'),
    ('Readings: 1.5 and 2.5.', 'Измерения: 1,5 и 1,5.'),
    ('Readings: 1.25 and 1,250.', 'Измерения: 1,250 и 1,250.'),
    ('Readings: 1.25 and 1,250.', 'Измерения: 1,250 и 1 250.'),
    ('A value of 1.25.', 'Значение 1,250.'),
    ('A value of 1.', 'Значение 1,000.'),
    ('Values: 1.25 and 2.5.', 'Значения: 1,250 и 2,5.'),
    ('Readings: 01 and 1 and 1.5.', 'Измерения: 1 и 1 и 1,5.'),
    ('Readings: 1.0 and 1 and 1.5.', 'Измерения: 1 и 1 и 1,5.'),
    ('The temperature was -12.5 and the delta 1.5.', 'Температура 12,5, изменение 1,5.'),
    ('The temperature was 12.5 and the delta 1.5.', 'Температура -12,5, изменение 1,5.'),
    ('The delta was +1.5.', 'Изменение 1,5.'),
    ('Values: -0.0 and 1.5.', 'Значения: 0,0 и 1,5.'),
    ('The distance was 1.5 km.', 'Расстояние 1500 м.'),
    ('Duration: 2.5 hours.', 'Продолжительность: 150 минут.'),
    ('ID-01 and L2 have value 1.5.', 'Значение ID-1 и L2 равно 1,5.'),
    ('L2 has value 1.5.', 'Значение L1 равно 1,5.'),
    ('On 2024-02-08 the value was 1.5.', '08.02.2024 значение было 1,5.'),
    ('On 08.02.2024 the value was 1.5.', '08.02.2024 значение было 1,5.'),
    ('Values: 1.5 and 1,234.', 'Значения: 1,5 и 1 23 4.'),
    ('Value: 9007199254740992.25.', 'Значение 9007199254740993,25.'),
])
def test_ambiguous_changed_missing_or_protected_numbers_disable_all_restoration(source, translated):
    assert restore_numeric_spelling(source, translated) == (translated, 0)


def test_formatter_does_not_correct_wrong_units_or_claim_semantic_fidelity():
    # Numerically equivalent spelling does not establish correct units or meaning.
    output, count = restore_numeric_spelling('A distance of 1.5 km.', 'Расстояние 1,5 мили.')
    assert output == 'Расстояние 1.5 мили.' and count == 1


def test_real_roman_reference_passes_unchanged_raw_number_gate_after_typography_only():
    from max_postiz_agent.editorial import translation_gate

    cases = json.loads((Path(__file__).resolve().parents[1] /
                        'deployment/translation/cases.json').read_text(encoding='utf-8'))['cases']
    roman = next(case for case in cases if case['id'] == 'nasa-roman-body')
    with pytest.raises(AgentError, match='TRANSLATION_NUMBER_CHANGED'):
        translation_gate(roman['text'], roman['reference_ru'])
    restored, count = restore_numeric_spelling(roman['text'], roman['reference_ru'])
    assert count == 2
    translation_gate(roman['text'], restored)
    assert 'L2' in restored and '930,000' in restored and '1.5' in restored


def test_translator_reports_numeric_typography_count_without_private_output_in_metrics():
    translator = DedicatedTranslator(
        {'model': 'hy-mt2-1.8b-q8', 'base_url': 'http://127.0.0.1:8094'},
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={
            'model': 'hy-mt2-1.8b-q8', 'complete': True, 'text': 'Расстояние 930 000 миль.',
            'metrics': {'seconds': 1.2}})))
    try:
        text, metrics = translator.translate('A distance of 930,000 miles.')
    finally:
        translator.client.close()
    assert text == 'Расстояние 930,000 миль.'
    assert metrics == [{'seconds': 1.2, 'numeric_spelling_restored': 1}]
