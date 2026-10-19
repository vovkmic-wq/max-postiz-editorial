"""Passive model residency checks never wake models or retry inference."""

import importlib.util
import json
from pathlib import Path

import httpx
import pytest

MODEL = 'hy-mt2-7b-q8'
PEER = 'http://node.test:8086/props'
OWN = 'http://node.test:8089/props'
RESULT = {'choices': [{'finish_reason': 'stop', 'message': {'content': 'Полный перевод.'}}],
          'usage': {'completion_tokens': 4}, 'timings': {'predicted_ms': 4500}}


class Clock:
    def __init__(self):
        self.now = 0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def service():
    path = Path(__file__).resolve().parents[1] / 'deployment/translation/service.py'
    spec = importlib.util.spec_from_file_location('translation_memory_guard_under_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def config(tmp_path):
    prompt = tmp_path / 'prompt.txt'
    prompt.write_text('Translate into Russian:\n{source_text}', encoding='utf-8')
    return {'prompt_path': str(prompt), 'gguf': {
        MODEL: 'http://node.test:8089', 'hy-mt2-1.8b-q8': 'http://node.test:8088'},
        'memory_guard': {'model': MODEL, 'peer_props_url': PEER, 'own_props_url': OWN,
                         'timeout_seconds': 3, 'poll_seconds': 1}}


def guarded(service, config, clock, callback):
    translator = service.Translator(config, clock=clock.monotonic, sleep=clock.sleep)
    translator.http.close()
    translator.http = httpx.Client(transport=httpx.MockTransport(callback),
                                  trust_env=False, follow_redirects=False)
    return translator


def test_waits_for_peer_then_single_inference_then_own_sleep_before_return(service, config):
    clock, order = Clock(), []
    peer, own = iter([False, False, True]), iter([False, True])

    def request(value):
        order.append((value.method, str(value.url)))
        if value.method == 'GET':
            sleeping = next(peer if str(value.url) == PEER else own)
            return httpx.Response(200, json={'is_sleeping': sleeping})
        body = json.loads(value.content)
        assert body['model'] == MODEL and body['messages'][0]['content'].endswith('Source text.')
        clock.now += 4.5
        return httpx.Response(200, json=RESULT)

    translator = guarded(service, config, clock, request)
    try:
        result = translator.translate(MODEL, 'Source text.')
        order.append(('RETURN', result['text']))
    finally:
        translator.http.close()
    assert order == [('GET', PEER)] * 3 + [
        ('POST', 'http://node.test:8089/v1/chat/completions'),
        ('GET', OWN), ('GET', OWN), ('RETURN', 'Полный перевод.')]
    assert result['model'] == MODEL and result['complete'] is True
    assert result['metrics']['usage'] == RESULT['usage']
    assert result['metrics']['timings'] == RESULT['timings']
    assert result['metrics']['memory_guard'] == {'peer_wait_seconds': 2, 'own_wait_seconds': 1}
    assert result['metrics']['inference_seconds'] == 4.5
    assert result['metrics']['seconds'] == 7.5


@pytest.mark.parametrize('sleeping', ['true', 'false', 1, 0, None, [], {}])
def test_only_actual_boolean_sleeping_allows_inference(service, config, sleeping):
    requests = []

    def response(request):
        requests.append(request.method)
        return httpx.Response(200, json={'is_sleeping': sleeping})

    translator = guarded(service, config, Clock(), response)
    try:
        with pytest.raises(ValueError, match='MEMORY_GUARD_PROPS_INVALID'):
            translator.translate(MODEL, 'Source text.')
    finally:
        translator.http.close()
    assert requests == ['GET']


def test_peer_timeout_never_starts_inference(service, config):
    clock, requests = Clock(), []

    def response(request):
        requests.append(request.method)
        return httpx.Response(200, json={'is_sleeping': False})

    translator = guarded(service, config, clock, response)
    try:
        with pytest.raises(ValueError, match='MEMORY_GUARD_TIMEOUT'):
            translator.translate(MODEL, 'Source text.')
    finally:
        translator.http.close()
    assert requests == ['GET'] * 3 and clock.now == 3


def test_own_timeout_does_not_return_translation_or_retry_inference(service, config):
    clock, requests = Clock(), []

    def response(request):
        requests.append((request.method, str(request.url)))
        if request.method == 'POST':
            return httpx.Response(200, json=RESULT)
        return httpx.Response(200, json={'is_sleeping': str(request.url) == PEER})

    translator = guarded(service, config, clock, response)
    try:
        with pytest.raises(ValueError, match='MEMORY_GUARD_TIMEOUT'):
            translator.translate(MODEL, 'Source text.')
    finally:
        translator.http.close()
    assert requests.count(('POST', 'http://node.test:8089/v1/chat/completions')) == 1
    assert requests[-1] == ('GET', OWN) and clock.now == 3


@pytest.mark.parametrize('failure', ['http', 'transport', 'truncated', 'invalid_json'])
def test_inference_failure_still_waits_for_own_sleep_without_retry(service, config, failure):
    clock, requests = Clock(), []
    own = iter([False, True])

    def response(request):
        requests.append((request.method, str(request.url)))
        if str(request.url) == PEER:
            return httpx.Response(200, json={'is_sleeping': True})
        if str(request.url) == OWN:
            return httpx.Response(200, json={'is_sleeping': next(own)})
        if failure == 'http':
            return httpx.Response(503, json={'error': 'Loading model'})
        if failure == 'transport':
            raise httpx.ReadTimeout('Backend response did not arrive', request=request)
        if failure == 'truncated':
            return httpx.Response(200, json={'choices': [{'finish_reason': 'length'}]})
        return httpx.Response(200, text='invalid JSON')

    translator = guarded(service, config, clock, response)
    try:
        with pytest.raises((httpx.HTTPError, ValueError)):
            translator.translate(MODEL, 'Source text.')
    finally:
        translator.http.close()
    assert requests == [('GET', PEER), ('POST', 'http://node.test:8089/v1/chat/completions'),
                        ('GET', OWN), ('GET', OWN)]
    assert clock.now == 1


def test_slow_props_response_after_deadline_cannot_authorize_inference(service, config):
    clock, requests = Clock(), []

    def response(request):
        requests.append(request.method)
        clock.now += 4
        return httpx.Response(200, json={'is_sleeping': True})

    translator = guarded(service, config, clock, response)
    try:
        with pytest.raises(ValueError, match='MEMORY_GUARD_TIMEOUT'):
            translator.translate(MODEL, 'Source text.')
    finally:
        translator.http.close()
    assert requests == ['GET']


def test_passive_transport_failure_never_retries_or_infers(service, config):
    requests = []

    def response(request):
        requests.append(request.method)
        raise httpx.ConnectError('Peer unavailable', request=request)

    translator = guarded(service, config, Clock(), response)
    try:
        with pytest.raises(ValueError, match='MEMORY_GUARD_UNAVAILABLE'):
            translator.translate(MODEL, 'Source text.')
    finally:
        translator.http.close()
    assert requests == ['GET']


@pytest.mark.parametrize('loading', [
    {'status': 'loading'},
    {'error': {'code': 503, 'message': 'Loading model', 'type': 'unavailable_error'}},
])
def test_loading_503_is_bounded_and_not_treated_as_sleeping(service, config, loading):
    clock, order = Clock(), []
    peer_checks = 0

    def response(request):
        nonlocal peer_checks
        order.append((request.method, str(request.url), clock.now))
        if str(request.url) == PEER:
            peer_checks += 1
            if peer_checks == 1:
                return httpx.Response(503, json=loading)
        if request.method == 'GET':
            return httpx.Response(200, json={'is_sleeping': True})
        return httpx.Response(200, json=RESULT)

    translator = guarded(service, config, clock, response)
    try:
        result = translator.translate(MODEL, 'Source text.')
    finally:
        translator.http.close()
    assert order[:3] == [('GET', PEER, 0), ('GET', PEER, 1),
                         ('POST', 'http://node.test:8089/v1/chat/completions', 1)]
    assert result['metrics']['memory_guard']['peer_wait_seconds'] == 1


@pytest.mark.parametrize('status,payload', [
    (200, []), (200, {}), (200, {'status': 'ok'}), (200, 'invalid JSON'),
    (401, {}), (404, {}), (500, {}), (503, {'status': 'error'}),
    (302, {'is_sleeping': True}),
])
def test_bad_props_or_permanent_http_failure_never_follows_redirect_or_infers(
        service, config, status, payload):
    requests = []

    def response(request):
        requests.append((request.method, str(request.url)))
        options = {'text': payload} if isinstance(payload, str) else {'json': payload}
        return httpx.Response(status, headers={'Location': 'http://other.test/props'}, **options)

    translator = guarded(service, config, Clock(), response)
    try:
        with pytest.raises(ValueError, match='MEMORY_GUARD_(PROPS_INVALID|UNAVAILABLE)'):
            translator.translate(MODEL, 'Source text.')
    finally:
        translator.http.close()
    assert requests == [('GET', PEER)]


def test_other_model_skips_guard_without_touching_peer_or_own_props(service, config):
    requests = []

    def response(request):
        requests.append((request.method, str(request.url)))
        return httpx.Response(200, json=RESULT)

    translator = guarded(service, config, Clock(), response)
    try:
        result = translator.translate('hy-mt2-1.8b-q8', 'Source text.')
    finally:
        translator.http.close()
    assert requests == [('POST', 'http://node.test:8088/v1/chat/completions')]
    assert 'memory_guard' not in result['metrics']


def test_guard_absent_preserves_hy_worker_contract(service, config):
    del config['memory_guard']
    requests = []

    def response(request):
        requests.append((request.method, str(request.url)))
        return httpx.Response(200, json=RESULT)

    translator = guarded(service, config, Clock(), response)
    try:
        result = translator.translate(MODEL, 'Source text.')
    finally:
        translator.http.close()
    assert result['complete'] is True
    assert 'memory_guard' not in result['metrics']
    assert requests == [('POST', 'http://node.test:8089/v1/chat/completions')]


def test_client_disables_environment_proxy_and_redirects(service, config, monkeypatch):
    options = []
    client = httpx.Client

    def capture(**kwargs):
        options.append(kwargs)
        return client(**kwargs)

    monkeypatch.setattr(service.httpx, 'Client', capture)
    translator = service.Translator(config)
    translator.http.close()
    assert options == [{'timeout': 600, 'trust_env': False, 'follow_redirects': False}]


@pytest.mark.parametrize('routes', [None, [], 'http://node.test:8089', {}])
def test_missing_or_malformed_routes_fail_before_opening_client(service, config, monkeypatch, routes):
    config['gguf'] = routes
    monkeypatch.setattr(service.httpx, 'Client', lambda **_: pytest.fail('Invalid config opened a client'))
    with pytest.raises(ValueError, match='MEMORY_GUARD_CONFIG_INVALID'):
        service.Translator(config)


def test_missing_prompt_cannot_leak_a_new_http_client(service, config, monkeypatch):
    config['prompt_path'] += '.missing'
    monkeypatch.setattr(service.httpx, 'Client', lambda **_: pytest.fail('Missing prompt opened a client'))
    with pytest.raises(FileNotFoundError):
        service.Translator(config)


@pytest.mark.parametrize('field,value', [
    ('model', 'hy-mt2-1.8b-q8'),
    ('own_props_url', 'http://node.test:8087/props'),
    ('own_props_url', 'http://node.test:0/props'),
    ('own_props_url', 'http://name:password@node.test:8089/props'),
    ('peer_props_url', 'http://other.test:8086/props'),
    ('peer_props_url', 'https://node.test:8086/props'),
    ('peer_props_url', OWN),
    ('peer_props_url', 'http://node.test:8086/health'),
    ('peer_props_url', 'http://node.test:8086/props?'),
    ('peer_props_url', 'http://node.test:8086/props#'),
    ('peer_props_url', 'http://node.test:8086/props?secret=value'),
    ('peer_props_url', 'http://node.test:8086/props\n'),
    ('peer_props_url', 'ftp://node.test:8086/props'),
    ('timeout_seconds', True), ('timeout_seconds', '180'),
    ('timeout_seconds', 0), ('timeout_seconds', 181), ('timeout_seconds', float('nan')),
    ('poll_seconds', 0), ('poll_seconds', 4), ('poll_seconds', float('inf')),
])
def test_invalid_guard_config_fails_before_any_network_client(service, config, monkeypatch, field, value):
    config['memory_guard'][field] = value
    monkeypatch.setattr(service.httpx, 'Client', lambda **_: pytest.fail('Invalid config opened a client'))
    with pytest.raises(ValueError, match='MEMORY_GUARD_CONFIG_INVALID'):
        service.Translator(config)
