"""Input isolation, deduplication across paths, leases and publication boundaries."""
import json
from concurrent.futures import ThreadPoolExecutor
from email.message import EmailMessage

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from max_postiz_agent.config import AgentError, fingerprint
from max_postiz_agent.db import Cursor, Draft, now
from max_postiz_agent.hub_api import hub_app
from max_postiz_agent.hub_models import Dispatch, RedditSubmission, Variant
from max_postiz_agent.reddit_mail import MAX_MAIL_BYTES, parse_ifttt_mail
from test_hub import hub as hub_fixture

hub = hub_fixture


URL = 'https://www.reddit.com/r/stories/comments/abc123/the_garden/?utm_source=share'
TEXT = 'A gardener found a beautiful flower. The neighbors enjoyed the garden and talked about the plants. '
OWNER = {'Authorization': 'Bearer hub-test-owner'}


def submit(hub, **changes):
    values = dict(title='The gardener and the flower', original=TEXT, url=URL, target_id='max')
    return hub.reddit_intake.submit(**(values | changes))


def row(hub, ident):
    with hub.db.session() as s:
        return s.get(RedditSubmission, ident)


def attest(hub, ident):
    value = row(hub, ident)
    source_hash = fingerprint({k: getattr(value, k) for k in ('title', 'original', 'url')})
    return hub.reddit_intake.attest_rights(ident, 'Own original test text', source_hash)


class Quality:
    def __init__(self, same=False):
        self.calls = 0
        self.same = same

    def prepare(self, post):
        self.calls += 1
        text = 'Садовник нашёл прекрасный цветок. Соседи любовались садом и обсуждали растения.'
        url = 'https://www.reddit.com' + post['permalink']
        return {'title': 'Необычный цветок', 'body': text + '\nИсточник: ' + url,
                'translation': text if self.same else text + post['id'], 'url': url, 'topic': 'stories'}

    def close(self):
        pass


def test_link_only_never_fetches_or_translates(hub):
    ident = submit(hub, original='')['id']
    assert row(hub, ident).state == 'awaiting_text'
    assert not hub.reddit_intake.process_one()
    hub.reddit_intake.edit(ident, 'A garden', TEXT, 'owner')
    assert row(hub, ident).state == 'awaiting_rights'
    assert not hub.reddit_intake.process_one()


@pytest.mark.parametrize('url', ['http://www.reddit.com/r/a/comments/abc123/',
    'https://www.reddit.com.evil.org/r/a/comments/abc123/',
    'https://www.reddit.com@evil.org/r/a/comments/abc123/', 'https://127.0.0.1/',
    'https://redd.it/abc123', 'https://www.reddit.com:8090/r/a/comments/abc123/'])
def test_urls_cannot_become_fetch_targets(hub, url):
    with pytest.raises(AgentError, match='REDDIT_POST_URL_REQUIRED'):
        submit(hub, url=url)


def test_both_inputs_share_id_and_text_deduplication(hub):
    first = submit(hub, origin='ifttt')
    duplicate = submit(hub, url='https://old.reddit.com/r/stories/comments/abc123/another_slug/')
    assert duplicate['duplicate'] and duplicate['id'] == first['id']
    duplicate = submit(hub, url=URL.replace('abc123', 'xyz456'), original=TEXT.upper())
    assert duplicate['id'] == first['id'] and duplicate['duplicate']


def test_concurrent_idempotent_input(hub):
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: submit(hub), range(2)))
    assert len({r['id'] for r in results}) == 1
    assert sum(not r['duplicate'] for r in results) == 1


def test_rights_are_version_bound_and_edits_revoke_them(hub):
    ident = submit(hub)['id']
    with pytest.raises(AgentError, match='REVISION_CONFLICT'):
        hub.reddit_intake.attest_rights(ident, 'test permission', '0' * 64)
    attest(hub, ident)
    assert row(hub, ident).state == 'queued'
    hub.reddit_intake.edit(ident, 'A different title', TEXT, 'owner')
    assert row(hub, ident).state == 'awaiting_rights'
    assert not row(hub, ident).rights_ref


def test_prepare_is_durable_and_never_creates_dispatch_or_pikabu(hub):
    quality = Quality()
    hub.reddit_intake.quality_factory = lambda: quality
    ident = submit(hub)['id']
    attest(hub, ident)
    assert hub.reddit_intake.process_one()
    assert not hub.reddit_intake.process_one()
    assert quality.calls == 1
    saved = row(hub, ident)
    assert saved.state == 'ready' and saved.variant_id
    with hub.db.session() as s:
        variant = s.get(Variant, saved.variant_id)
        assert variant.target_id == 'max' and variant.sources
        assert s.get(Draft, variant.draft_id).state == 'reddit_review'
        assert s.scalar(select(func.count()).select_from(Dispatch)) == 0
        assert s.scalar(select(func.count()).select_from(Variant)) == 1


def test_duplicate_translation_rolls_back_all_new_drafts(hub):
    hub.reddit_intake.quality_factory = lambda: Quality(same=True)
    first = submit(hub)['id']
    attest(hub, first)
    assert hub.reddit_intake.process_one()
    second = submit(hub, url=URL.replace('abc123', 'xyz456'), original=TEXT + 'A different source.')['id']
    attest(hub, second)
    assert not hub.reddit_intake.process_one()
    assert row(hub, second).error == 'REDDIT_DUPLICATE_TRANSLATION'
    with hub.db.session() as s:
        assert s.scalar(select(func.count()).select_from(Variant)) == 1


def test_interrupted_processing_needs_explicit_resume(hub):
    ident = submit(hub)['id']
    attest(hub, ident)
    with hub.db.session.begin() as s:
        saved = s.get(RedditSubmission, ident)
        saved.state, saved.owner, saved.lease_until = 'processing', 'old-worker', now() - 1
    assert not hub.reddit_intake.process_one()
    assert row(hub, ident).state == 'needs_attention'
    assert hub.reddit_intake.resume(ident)['state'] == 'queued'


def test_quality_failure_is_not_retried_by_worker(hub):
    class Failed(Quality):
        def prepare(self, post):
            self.calls += 1
            raise AgentError('REDDIT_QUALITY_REJECTED')
    quality = Failed()
    hub.reddit_intake.quality_factory = lambda: quality
    ident = submit(hub)['id']
    attest(hub, ident)
    assert not hub.reddit_intake.process_one()
    assert not hub.reddit_intake.process_one()
    assert row(hub, ident).state == 'rejected' and quality.calls == 1


def test_unknown_postiz_creation_is_not_automatically_repeated(hub, monkeypatch):
    (hub.settings.root / 'config/reddit_intake.json').write_text(json.dumps({
        'processing_enabled': True, 'stage_postiz': True, 'mail_target_id': 'max'}))
    hub.reddit_intake.quality_factory = Quality
    calls = []

    def unknown(ident):
        calls.append(ident)
        raise AgentError('POSTIZ_UNKNOWN')

    monkeypatch.setattr(hub, 'stage_remote', unknown)
    ident = submit(hub)['id']
    attest(hub, ident)
    assert hub.reddit_intake.process_one()
    assert not hub.reddit_intake.process_one()
    assert row(hub, ident).state == 'ready' and row(hub, ident).error == 'POSTIZ_UNKNOWN'
    assert len(calls) == 1


def raw_mail(token, **changes):
    message = EmailMessage()
    message['From'] = changes.get('sender', 'action@ifttt.com')
    message['Subject'] = changes.get('subject', '[MAX-REDDIT ' + token + ']')
    message.set_content('URL=' + URL + '\nTITLE=The garden\nTEXT_BEGIN\n' + TEXT + '\nTEXT_END')
    return message.as_bytes()


def test_mail_auth_and_template_are_not_source_permissions(hub):
    hub.reddit_mail.configure('owner@gmail.com', '', False)
    token = hub.reddit_mail.read()['token']
    parsed = parse_ifttt_mail(raw_mail(token), token)
    ident = hub.reddit_intake.submit(**parsed, target_id='max', origin='ifttt')['id']
    assert row(hub, ident).state == 'awaiting_rights'
    assert not row(hub, ident).rights_ref
    for changes in ({'sender': 'someone@example.org'}, {'subject': '[MAX-REDDIT wrong]'},
                    {'subject': 'Неверная тема'}):
        with pytest.raises(AgentError, match='IFTTT_MAIL_UNTRUSTED'):
            parse_ifttt_mail(raw_mail(token, **changes), token)
    with pytest.raises(AgentError, match='IFTTT_MAIL_TOO_LARGE'):
        parse_ifttt_mail(b'x' * (MAX_MAIL_BYTES + 1), token)


def test_html_mail_and_unexpanded_ingredients(hub):
    token = 't' * 40
    message = EmailMessage()
    message['From'], message['Subject'] = 'action@ifttt.com', '[MAX-REDDIT ' + token + ']'
    message.set_content(f'<p>URL={URL}</p><p>TITLE=Garden</p><p>TEXT_BEGIN</p><p>{TEXT}</p><p>TEXT_END</p>', subtype='html')
    assert parse_ifttt_mail(message.as_bytes(), token)['original'] == TEXT.strip()
    with pytest.raises(AgentError, match='IFTTT_INGREDIENT_NOT_EXPANDED'):
        parse_ifttt_mail(raw_mail(token).replace(b'The garden', b'{{Title}}'), token)


def test_mail_cursor_restarts_and_never_marks_mail(hub):
    (hub.settings.root / 'config/reddit_intake.json').write_text(json.dumps({
        'processing_enabled': True, 'stage_postiz': False, 'mail_target_id': 'max'}))
    hub.reddit_mail.configure('owner@gmail.com', 'abcdefghijklmnop', True)
    token = hub.reddit_mail.read()['token']
    calls = []

    class Mail:
        def login(self, *args):
            pass

        def select(self, folder, readonly):
            assert folder == '"Reddit-IFTTT"' and readonly
            return 'OK', []

        def response(self, name):
            assert name == 'UIDVALIDITY'
            return name, [b'123']

        def uid(self, command, *args):
            calls.append(command)
            if command == 'search':
                return 'OK', [b'1']
            assert command == 'fetch' and 'BODY.PEEK' in args[1]
            return 'OK', [(b'1', raw_mail(token))]

        def logout(self):
            pass

    assert hub.reddit_mail.poll(connector=Mail) == 1
    with hub.db.session.begin() as s:
        s.get(Cursor, 'reddit_ifttt_mail').next_due = 0
    assert hub.reddit_mail.poll(connector=Mail) == 0
    assert calls.count('fetch') == 1 and 'store' not in calls


def test_auth_failure_stops_automatic_attempts(hub):
    (hub.settings.root / 'config/reddit_intake.json').write_text(json.dumps({
        'processing_enabled': True, 'stage_postiz': False, 'mail_target_id': 'max'}))
    hub.reddit_mail.configure('owner@gmail.com', 'abcdefghijklmnop', True)
    count = []

    def broken():
        count.append(1)
        raise OSError('private transport message')

    with pytest.raises(AgentError, match='GMAIL_CONNECTION_OR_AUTH_FAILED'):
        hub.reddit_mail.poll(connector=broken)
    assert hub.reddit_mail.poll(connector=broken) == 0
    assert count == [1]


def test_api_roles_no_secret_echo_or_remote_rights(hub):
    client = TestClient(hub_app(hub))
    assert client.get('/api/hub/reddit').status_code == 401
    ident = submit(hub)['id']
    viewer = {'Authorization': 'Bearer hub-test-viewer'}
    assert client.post('/api/hub/reddit/mail', headers=viewer,
        json={'username': 'owner@gmail.com', 'app_password': '', 'enabled': False}).status_code == 403
    secret = 'do-not-echo-' * 20
    response = client.post('/api/hub/reddit/mail', headers=OWNER,
        json={'username': 'owner@gmail.com', 'app_password': secret, 'enabled': False})
    assert response.status_code == 422 and secret not in response.text
    response = client.post('/api/hub/reddit/mail', headers=OWNER,
        json={'username': 'owner@gmail.com', 'app_password': 'abcdefghijklmnop', 'enabled': False})
    assert response.status_code == 200 and 'abcdefghijklmnop' not in response.text
    assert client.get('/api/hub/reddit/mail/template', headers=viewer).status_code == 403
    items = client.get('/api/hub/reddit', headers=OWNER).json()['items']
    assert items[0]['id'] == ident and len(items[0]['source_hash']) == 64
    assert client.post('/api/hub/reddit/items', headers=OWNER, json={
        'title': 'test', 'original': TEXT, 'url': URL, 'target_id': 'max',
        'rights_ref': 'remote payload cannot authorize'}).status_code == 422
