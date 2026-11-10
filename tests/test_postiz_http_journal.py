import hashlib
import json
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from max_postiz_agent.config import AgentError
from max_postiz_agent.postiz import PostizClient


def events(root):
    return [json.loads(line) for line in (root / 'data/postiz-http.jsonl').read_text().splitlines()]


@pytest.fixture
def client_factory(tmp_path, monkeypatch):
    monkeypatch.setenv('POSTIZ_BASE_URL', 'http://127.0.0.1:14007/api/public/v1')
    monkeypatch.setenv('POSTIZ_API_KEY', 'private-token')
    clients = []

    def create(handler, options=None):
        client = PostizClient(options or {}, httpx.MockTransport(handler), journal_root=tmp_path)
        clients.append(client)
        return client

    yield create
    for client in clients:
        client.close()


def test_guarded_schedule_waits_for_postiz_bridge_budget_without_retry(client_factory, tmp_path):
    calls = []
    expected_hash = 'a' * 64

    def respond(request):
        calls.append(request.extensions['timeout'])
        if len(calls) == 1:
            return httpx.Response(200, json={'posts': []})
        return httpx.Response(201, headers={'X-Max-Postiz-CAS-Verified': expected_hash},
                              json=[{'postId': 'opaque-id', 'private': 'private-body'}])

    client = client_factory(respond)
    assert client.posts() == []
    result = client.request('POST', '/posts', expected_cas_hash=expected_hash,
                            expected_post_id='opaque-id', json={'type': 'schedule', 'secret': 'private-input'})
    assert result[0]['postId'] == 'opaque-id'
    assert calls == [dict.fromkeys(('connect', 'read', 'write', 'pool'), 30),
                     {'connect': 30, 'read': 75, 'write': 30, 'pool': 30}]
    get, post = events(tmp_path)
    assert get['http_status'] == 200 and post['http_status'] == 201
    assert post['outcome'] == 'ok' and post['cas'] == 'verified'
    assert post['operation'] == 'schedule' and post['read_timeout_s'] == 75
    assert post['post_id_sha256'] == hashlib.sha256(b'opaque-id').hexdigest()
    assert all(value not in str(post) for value in ('private-', expected_hash, 'opaque-id', '127.0.0.1'))


@pytest.mark.parametrize('exc', [httpx.ReadTimeout, httpx.ConnectError, httpx.RemoteProtocolError])
def test_one_transport_failure_is_logged_without_raw_exception_or_retry(client_factory, tmp_path, exc):
    calls = []

    def fail(request):
        calls.append(request)
        raise exc('private-url-and-header', request=request)

    client = client_factory(fail)
    with pytest.raises(AgentError, match='^POSTIZ_UNKNOWN$'):
        client.request('POST', '/posts', expected_cas_hash='b' * 64)
    assert len(calls) == 1
    row, = events(tmp_path)
    assert row['outcome'] == 'transport_error' and row['stage'] == 'waiting_for_headers'
    assert row['exception_type'] == exc.__name__ and row['http_status'] is None
    assert 'private-url' not in str(row)


def test_http_status_survives_failure_while_reading_response_body(client_factory, tmp_path):
    class Interrupted(httpx.SyncByteStream):
        def __iter__(self):
            yield b'private-prefix'
            raise httpx.ReadTimeout('private-response-body')

    client = client_factory(lambda _: httpx.Response(201, stream=Interrupted()))
    with pytest.raises(AgentError, match='^POSTIZ_UNKNOWN$'):
        client.request('POST', '/posts')
    row, = events(tmp_path)
    assert row['http_status'] == 201 and row['stage'] == 'reading_body'
    assert row['exception_type'] == 'ReadTimeout' and 'private-' not in str(row)


@pytest.mark.parametrize('status,header,body,outcome,code', [
    (409, None, b'{"message":"MAX_BROWSER_CAS_UNCONFIRMED","extra":"private"}',
     'http_error', 'POSTIZ_REJECTED'),
    (503, None, b'private', 'http_error', 'POSTIZ_UNKNOWN'),
    (307, None, b'private', 'redirect', 'POSTIZ_UNKNOWN'),
    (201, None, b'[]', 'cas_unconfirmed', 'POSTIZ_UNKNOWN'),
    (201, 'wrong', b'[]', 'cas_unconfirmed', 'POSTIZ_UNKNOWN'),
    (201, 'a' * 64, b'private', 'invalid_json', 'POSTIZ_UNKNOWN'),
    (201, 'a' * 64, b'[]', 'unexpected_response_shape', 'POSTIZ_UNKNOWN'),
])
def test_response_journal_distinguishes_status_cas_and_body_errors(
        client_factory, tmp_path, status, header, body, outcome, code):
    headers = {'X-Max-Postiz-CAS-Verified': header} if header is not None else {}
    client = client_factory(lambda _: httpx.Response(status, headers=headers, content=body))
    with pytest.raises(AgentError, match=f'^{code}$'):
        client.request('POST', '/posts', expected_cas_hash='a' * 64, expected_post_id='opaque-id')
    row, = events(tmp_path)
    assert (row['http_status'], row['outcome'], row['error_code']) == (status, outcome, code)
    assert row['response_bytes'] == len(body)
    assert row['upstream_code'] == ('MAX_BROWSER_CAS_UNCONFIRMED' if status == 409 else None)
    assert 'private' not in str(row) and ('a' * 64) not in str(row) and 'wrong' not in str(row)


def test_rotation_is_bounded_and_retains_latest_events(client_factory, tmp_path, monkeypatch):
    from max_postiz_agent import postiz_http_journal

    monkeypatch.setattr(postiz_http_journal, 'MAX_BYTES', 1024)
    client = client_factory(lambda _: httpx.Response(200, json={}))
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: client.request('GET', '/integrations'), range(24)))
    client.request('DELETE', '/posts/opaque-private-id')
    files = list((tmp_path / 'data').glob('postiz-http.jsonl*'))
    assert {file.name for file in files} == {'postiz-http.jsonl', 'postiz-http.jsonl.1', 'postiz-http.jsonl.2'}
    assert all(file.stat().st_size <= 1024 for file in files)
    for file in files:
        for line in file.read_text().splitlines():
            assert json.loads(line)['http_status'] == 200
    assert events(tmp_path)[-1]['endpoint'] == '/posts/:id'
    assert 'opaque-private-id' not in str(events(tmp_path))


@pytest.mark.parametrize('general,configured,expected', [(30, 90, 90), (100, 75, 100)])
def test_configured_timeouts_keep_the_larger_read_budget(client_factory, general, configured, expected):
    def respond(request):
        assert request.extensions['timeout'] == {'connect': general, 'read': expected,
                                                 'write': general, 'pool': general}
        return httpx.Response(201, headers={'X-Max-Postiz-CAS-Verified': 'a' * 64}, json=[])

    client = client_factory(respond, {'timeout_seconds': general, 'mutation_read_timeout_seconds': configured})
    assert client.request('POST', '/posts', expected_cas_hash='a' * 64) == []


def test_log_write_failure_does_not_mask_http_outcome(client_factory, caplog, monkeypatch):
    def fail(_):
        raise OSError('private filesystem detail')

    client = client_factory(lambda _: httpx.Response(503))
    monkeypatch.setattr(client.journal, '_write', fail)
    with pytest.raises(AgentError, match='^POSTIZ_UNKNOWN$'):
        client.request('POST', '/posts')
    assert 'POSTIZ_HTTP_JOURNAL_UNAVAILABLE' in caplog.text
    assert 'private filesystem detail' not in caplog.text
