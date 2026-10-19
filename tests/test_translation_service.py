"""Exercise the standalone worker without loading models or opening sockets."""
import importlib.util
import io
import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest


@pytest.fixture
def service():
    path = Path(__file__).resolve().parents[1] / 'deployment/translation/service.py'
    spec = importlib.util.spec_from_file_location('translation_service_under_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def translator(service, tmp_path):
    prompt = tmp_path / 'prompt.txt'
    prompt.write_text('Translate into Russian:\n{source_text}', encoding='utf-8')
    value = service.Translator({'prompt_path': str(prompt), 'gguf': {
        'hy-mt2-1.8b-q8': 'http://llama.test:8089',
        'translategemma-12b-q4': 'http://llama.test:8087'}})
    yield value
    value.http.close()


def mock_backend(translator, response):
    seen = []

    def handle(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=response)

    translator.http.close()
    translator.http = httpx.Client(transport=httpx.MockTransport(handle))
    return seen


@pytest.mark.parametrize('model', ['hy-mt2-1.8b-q8', 'translategemma-12b-q4'])
def test_gguf_templates_and_complete_response(translator, model):
    seen = mock_backend(translator, {'choices': [{'finish_reason': 'stop',
        'message': {'content': ' Полный перевод. '}}], 'usage': {'completion_tokens': 3}})
    value = translator.translate(model, 'Complete source.')
    assert value['text'] == 'Полный перевод.' and value['complete'] is True
    assert value['model'] == model and value['metrics']['seconds'] >= 0
    assert seen[0]['max_tokens'] == 1024
    content = seen[0]['messages'][0]['content']
    assert content == ([{'type': 'text', 'source_lang_code': 'en', 'target_lang_code': 'ru',
                          'text': 'Complete source.'}] if model.startswith('translategemma')
                       else 'Translate into Russian:\nComplete source.')


@pytest.mark.parametrize('choice,error', [
    ({'finish_reason': 'length', 'message': {'content': 'Incomplete'}}, 'TRUNCATED'),
    ({'finish_reason': 'stop', 'message': {'content': ''}}, 'EMPTY'),
    ({'finish_reason': 'stop', 'message': {'content': None}}, 'EMPTY'),
])
def test_gguf_does_not_accept_truncation_or_empty_output(translator, choice, error):
    mock_backend(translator, {'choices': [choice]})
    with pytest.raises(ValueError, match=error):
        translator.translate('hy-mt2-1.8b-q8', 'Original text.')


def test_unknown_model_does_not_contact_backend(translator):
    seen = mock_backend(translator, {})
    with pytest.raises(ValueError, match='MODEL_UNAVAILABLE'):
        translator.translate('unknown', 'Original text.')
    assert not seen


def opus_fakes(translator, monkeypatch, source_count=3, hypothesis=None):
    monkeypatch.setitem(sys.modules, 'ctranslate2', SimpleNamespace())
    monkeypatch.setitem(sys.modules, 'transformers', SimpleNamespace(MarianTokenizer=object))
    calls = []

    def batch(tokens, **options):
        calls.append((tokens, options))
        return [SimpleNamespace(hypotheses=[['перевод', '</s>'] if hypothesis is None else hypothesis])]

    translator.opus = SimpleNamespace(translate_batch=batch, compute_type='int8_float32')
    translator.tokenizer = SimpleNamespace(
        encode=lambda _: list(range(source_count)), convert_ids_to_tokens=lambda tokens: tokens,
        convert_tokens_to_ids=lambda tokens: tokens, decode=lambda *args, **kwargs: 'Перевод.',
        eos_token='</s>')
    return calls


def test_opus_has_no_input_truncation_and_reports_actual_compute_type(translator, monkeypatch):
    calls = opus_fakes(translator, monkeypatch)
    value = translator.translate('opus-mt-en-ru-int8', 'Original text.')
    assert value['metrics']['compute_type'] == 'int8_float32'
    assert value['complete'] is True
    options = calls[0][1]
    assert options['max_input_length'] == 0 and options['return_end_token'] is True


def test_opus_rejects_overlong_input_before_inference(translator, monkeypatch):
    calls = opus_fakes(translator, monkeypatch, source_count=501)
    with pytest.raises(ValueError, match='SOURCE_TOO_LONG'):
        translator.translate('opus-mt-en-ru-int8', 'Original text.')
    assert not calls


@pytest.mark.parametrize('hypothesis', [[], ['incomplete']])
def test_opus_requires_end_token(translator, monkeypatch, hypothesis):
    opus_fakes(translator, monkeypatch, hypothesis=hypothesis)
    with pytest.raises(ValueError, match='TRUNCATED'):
        translator.translate('opus-mt-en-ru-int8', 'Original text.')


def request(service, data, translator, path='/translate', length=None):
    handler = object.__new__(service.Handler)
    handler.path, handler.translator = path, translator
    raw = json.dumps(data).encode()
    handler.rfile = io.BytesIO(raw)
    handler.headers = {'Content-Length': str(len(raw) if length is None else length)}
    replies = []
    handler.reply = lambda status, body: replies.append((status, body))
    handler.do_POST()
    assert len(replies) == 1
    return replies[0]


@pytest.mark.parametrize('data', [
    [], None, 'text', {},
    {'model': 'hy-mt2-1.8b-q8', 'text': 'Hello.', 'source': 'ru', 'target': 'en'},
    {'model': 'hy-mt2-1.8b-q8', 'text': '  ', 'source': 'en', 'target': 'ru'},
    {'model': 'hy-mt2-1.8b-q8', 'text': 'x' * 1601, 'source': 'en', 'target': 'ru'},
    {'model': [], 'text': 'Hello.', 'source': 'en', 'target': 'ru'},
    {'model': 'hy-mt2-1.8b-q8', 'text': 'Hello.', 'source': 'en', 'target': 'ru', 'extra': True},
])
def test_http_handler_rejects_bad_input_before_inference(service, data):
    translator = SimpleNamespace(translate=lambda *_: pytest.fail('Invalid input reached inference'))
    status, body = request(service, data, translator)
    assert status == 400 and body == {'error': 'TRANSLATION_INPUT_INVALID'}


@pytest.mark.parametrize('length', [0, -1, 24001, 'invalid'])
def test_http_handler_bounds_body_size(service, length):
    assert request(service, {}, None, length=length)[0] == 400


def test_busy_worker_does_not_start_concurrent_generation(service):
    lock = threading.Lock()
    lock.acquire()
    translator = SimpleNamespace(lock=lock,
        translate=lambda *_: pytest.fail('Busy worker must not infer'))
    try:
        assert request(service, {'model': 'hy-mt2-1.8b-q8', 'text': 'Hello.',
            'source': 'en', 'target': 'ru'}, translator) == (503, {'error': 'TRANSLATION_BUSY'})
    finally:
        lock.release()


def test_backend_failure_releases_lock_and_hides_private_text(service):
    lock = threading.Lock()

    def fail(*_):
        raise ValueError('PRIVATE ARTICLE CONTENT')

    translator = SimpleNamespace(lock=lock, translate=fail)
    assert request(service, {'model': 'hy-mt2-1.8b-q8', 'text': 'Private source.',
        'source': 'en', 'target': 'ru'}, translator) == (502, {'error': 'TRANSLATION_FAILED'})
    assert not lock.locked()


def test_http_success_returns_translation_without_changing_protocol(service):
    value = {'model': 'hy-mt2-1.8b-q8', 'text': 'Привет.', 'complete': True}
    translator = SimpleNamespace(lock=threading.Lock(), translate=lambda *_: value)
    assert request(service, {'model': value['model'], 'text': 'Hello.', 'source': 'en',
        'target': 'ru'}, translator) == (200, value)
    assert not translator.lock.locked()
