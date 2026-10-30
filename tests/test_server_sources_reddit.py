"""Проверки серверного Reddit-сборщика: только фикстуры, без внешней сети."""
import copy

import httpx
import pytest

from max_postiz_agent.config import AgentError
from max_postiz_agent.reddit import RedditClient, text_hash
from max_postiz_agent.server_sources import (
    REDDIT_DEFAULTS,
    canonical_reddit_post_url,
    collect_reddit_candidates,
    normalize_reddit_settings,
)


NOW = 1_800_000_000
BODY = ('This is an interesting scientific story about the discovery of planets and stars. '
        'The scientists describe the observations and explain how the telescope works. ')


def post(ident='abc123', **changes):
    subreddit = changes.pop('subreddit', 'science')
    return {'id': ident, 'name': 't3_' + ident, 'title': 'A new discovery of planets',
            'selftext': BODY + ident, 'author': 'astro_writer', 'subreddit': subreddit,
            'is_self': True, 'permalink': f'/r/{subreddit}/comments/{ident}/a_story/',
            'created_utc': NOW - 60, 'score': 100, 'num_comments': 12, **changes}


def settings(**changes):
    return {**copy.deepcopy(REDDIT_DEFAULTS), 'subreddits': ['science'], **changes}


def grant(**changes):
    return {'scope_type': 'author', 'scope': 'astro_writer', 'verified': True,
            'allow_storage': True, 'allow_derivative': True, 'allow_redistribution': True,
            'evidence_ref': 'data/author-permission.local.json', **changes}


class Client:
    options = {'access_approved': True}
    collection_info = {'pages': 1}

    def __init__(self, posts):
        self.posts, self.calls = posts, 0

    def collect(self, value):
        self.calls += 1
        return copy.deepcopy(self.posts)


def collect(posts, config=None, **kwargs):
    return collect_reddit_candidates(Client(posts), config or settings(), now=NOW,
                                     rights_grants=kwargs.pop('rights_grants', [grant()]), **kwargs)


def options(monkeypatch, approved=True):
    opts = {'access_approved': approved}
    for name in ('client_id', 'client_secret', 'refresh_token', 'user_agent'):
        opts[name + '_env'] = 'TEST_SERVER_REDDIT_' + name.upper()
        monkeypatch.setenv(opts[name + '_env'], 'offline-' + name)
    return opts


def response(rows, after=None):
    return httpx.Response(200, json={'kind': 'Listing', 'data': {
        'children': [{'kind': 't3', 'data': row} for row in rows], 'after': after}})


def test_normalizer_defaults_scope_and_no_input_mutation():
    value = {'subreddits': ['r/Science', '/r/science'], 'authors': ['u/Astro_Writer'],
             'keywords': ['Space Science', 'space science']}
    saved = copy.deepcopy(value)
    result = normalize_reddit_settings(value)
    assert value == saved
    assert result == {**REDDIT_DEFAULTS, 'subreddits': ['science'],
                      'authors': ['astro_writer'], 'keywords': ['space science']}
    result['subreddits'].append('space')
    assert REDDIT_DEFAULTS['subreddits'] == []
    assert normalize_reddit_settings({}, require_scope=False) == REDDIT_DEFAULTS
    with pytest.raises(AgentError, match='REDDIT_COLLECTION_SCOPE_REQUIRED'):
        normalize_reddit_settings({})


@pytest.mark.parametrize('change', [
    {'min_score': True}, {'min_comments': -1}, {'max_age_hours': 0},
    {'max_age_hours': 8761}, {'limit': 101}, {'max_pages': 6}, {'sort': []},
    {'sort': 'controversial'}, {'time_filter': None}, {'time_filter': 'forever'},
    {'authors': 'alice'}, {'authors': ['../alice']}, {'subreddits': ['science?x=1']},
    {'keywords': ['']}, {'keywords': ['a\nb']}, {'keywords': ['x'] * 31},
    {'authors': ['x'] * 21}, {'access_approved': True}, {'min_rating': 0},
])
def test_normalizer_rejects_invalid_or_privilege_fields(change):
    with pytest.raises(AgentError, match='REDDIT_COLLECTION_CONFIG_INVALID'):
        normalize_reddit_settings(settings(**change))


@pytest.mark.parametrize('value', [None, True, [], 'science'])
def test_normalizer_rejects_non_object(value):
    with pytest.raises(AgentError, match='REDDIT_COLLECTION_CONFIG_INVALID'):
        normalize_reddit_settings(value)


def test_oauth_gate_before_any_http(monkeypatch):
    calls = []
    client = RedditClient(options(monkeypatch, False), httpx.MockTransport(lambda r: calls.append(r)))
    with pytest.raises(AgentError, match='REDDIT_APPROVAL_REQUIRED'):
        collect_reddit_candidates(client, settings(), now=NOW, rights_grants=[grant()])
    with pytest.raises(AgentError, match='REDDIT_APPROVAL_REQUIRED'):
        client.collect(settings())
    assert calls == []
    client.close()


def test_scope_pagination_round_robin_ranking_and_exact_urls(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path.endswith('access_token'):
            return httpx.Response(200, json={'access_token': 'opaque', 'expires_in': 3600})
        assert request.url.host == 'oauth.reddit.com'
        assert request.headers['authorization'] == 'Bearer opaque'
        assert request.url.params['raw_json'] == '1'
        assert request.url.params['t'] == 'week'
        if request.url.path == '/r/science/top' and 'after' not in request.url.params:
            return response([post('low123', score=20)], 't3_low123')
        if request.url.path == '/r/space/top':
            return response([post('mid123', subreddit='space', score=60)])
        assert request.url.params['after'] == 't3_low123'
        return response([post('best123', score=90)])

    client = RedditClient(options(monkeypatch), httpx.MockTransport(handler))
    result = collect_reddit_candidates(client, settings(subreddits=['science', 'space']),
                                       now=NOW, rights_grants=[grant()])
    assert [x['external_id'] for x in result['candidates']] == ['t3_best123', 't3_mid123', 't3_low123']
    assert [r.url.path for r in calls[1:]] == ['/r/science/top', '/r/space/top', '/r/science/top']
    assert result['candidates'][0]['article']['url'] == 'https://www.reddit.com/r/science/comments/best123/'
    assert result['diagnostics']['listing']['pages'] == 3
    client.close()


def test_author_feed_intersects_subreddit_and_keywords(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path.endswith('access_token'):
            return httpx.Response(200, json={'access_token': 'opaque'})
        assert request.url.path == '/user/astro_writer/submitted'
        assert request.url.params['sort'] == 'new' and 't' not in request.url.params
        return response([post('valid1'), post('other1', subreddit='stories'),
                         post('keyword1', title='A quiet garden', selftext=BODY.replace('planets', 'gardens'))])

    client = RedditClient(options(monkeypatch), httpx.MockTransport(handler))
    result = collect_reddit_candidates(client, settings(authors=['astro_writer'], keywords=['planets'],
                                                       sort='new'), now=NOW, rights_grants=[grant()])
    assert [x['external_id'] for x in result['candidates']] == ['t3_valid1']
    assert len(calls) == 2
    client.close()


@pytest.mark.parametrize('after', ['../../bad', 't1_comment', 123, False, 't3_UPPER'])
def test_invalid_cursor_stops_without_followup(monkeypatch, after):
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path.endswith('access_token'):
            return httpx.Response(200, json={'access_token': 'opaque'})
        return response([post()], after)

    client = RedditClient(options(monkeypatch), httpx.MockTransport(handler))
    with pytest.raises(AgentError, match='REDDIT_RESPONSE_INVALID'):
        client.collect(settings())
    assert len(calls) == 2
    client.close()


def test_repeated_cursor_stops_without_replay(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path.endswith('access_token'):
            return httpx.Response(200, json={'access_token': 'opaque'})
        return response([post()], 't3_same')

    client = RedditClient(options(monkeypatch), httpx.MockTransport(handler))
    with pytest.raises(AgentError, match='REDDIT_PAGINATION_INVALID'):
        client.collect(settings())
    assert len(calls) == 3
    client.close()


@pytest.mark.parametrize('status', [401, 403, 429, 500, 302])
def test_http_failure_has_no_retry_or_anonymous_fallback(monkeypatch, status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, json={})

    client = RedditClient(options(monkeypatch), httpx.MockTransport(handler))
    with pytest.raises(AgentError):
        client.collect(settings())
    assert len(calls) == 1
    client.close()


def test_rate_limit_header_prevents_next_page(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path.endswith('access_token'):
            return httpx.Response(200, json={'access_token': 'opaque'})
        return httpx.Response(200, headers={'x-ratelimit-remaining': '0', 'x-ratelimit-reset': '60'},
                              json={'data': {'children': [{'kind': 't3', 'data': post()}],
                                             'after': 't3_abc123'}})

    client = RedditClient(options(monkeypatch), httpx.MockTransport(handler))
    with pytest.raises(AgentError, match='REDDIT_RATE_LIMIT'):
        client.collect(settings())
    assert len(calls) == 2
    client.close()


@pytest.mark.parametrize('number', ['nan', 'inf', '-1', 'bad'])
def test_invalid_rate_headers_are_not_treated_as_budget(monkeypatch, number):
    client = RedditClient(options(monkeypatch), httpx.MockTransport(
        lambda r: httpx.Response(200, headers={'x-ratelimit-remaining': number},
                                json={'access_token': 'opaque'})))
    with pytest.raises(AgentError, match='REDDIT_RESPONSE_INVALID'):
        client.collect(settings())
    client.close()


def test_pagination_budget_is_bounded(monkeypatch):
    count = 0

    def handler(request):
        nonlocal count
        if request.url.path.endswith('access_token'):
            return httpx.Response(200, json={'access_token': 'opaque'})
        count += 1
        return response([post('p' + str(count))], 't3_cursor' + str(count))

    client = RedditClient(options(monkeypatch), httpx.MockTransport(handler))
    client.collect(settings(subreddits=['s' + str(i) for i in range(20)], max_pages=5))
    assert count == 20
    assert client.collection_info['budget_exhausted'] is True
    client.close()


@pytest.mark.parametrize('change', [
    {'over_18': True}, {'over_18': 'false'}, {'promoted': True}, {'stickied': True},
    {'is_ad': True}, {'removed_by_category': 'moderator'}, {'author': '[deleted]'},
    {'selftext': '[removed]'}, {'selftext': ' [DELETED] '}, {'is_self': False},
    {'created_utc': None}, {'created_utc': True}, {'created_utc': float('nan')},
    {'created_utc': NOW + 301}, {'created_utc': NOW - 73 * 3600}, {'score': True},
    {'num_comments': -1}, {'name': 't3_other'}, {'permalink': '/r/science/comments/wrong/a/'},
    {'url': 'https://evil.example/a'}, {'link_flair_text': 'Sponsored'},
    {'title': 'A private story [NSFW]'},
    {'title': 'Latest parliamentary election results'}, {'subreddit': 'politics'},
])
def test_strict_source_exclusions(change):
    assert collect([post(**change)])['candidates'] == []


def test_thresholds_keywords_boundaries_and_new_order():
    rows = [post('older', title='Mars observations', created_utc=NOW - 200),
            post('newer', title='MARS observations', created_utc=NOW - 10, score=90),
            post('marsh', title='Marsh observations', selftext=BODY.replace('planets', 'gardens')),
            post('low', title='Mars observations', score=4),
            post('quiet', title='Mars observations', num_comments=1)]
    result = collect(rows, settings(keywords=['mars'], min_score=5, min_comments=2, sort='new', limit=2))
    assert [x['external_id'] for x in result['candidates']] == ['t3_newer', 't3_older']


def test_original_crosspost_identity_and_rights_are_preserved():
    original = post('origin', score=200)
    wrapper = post('cross', author='curator', is_self=False,
                   crosspost_parent='t3_origin', crosspost_parent_list=[original],
                   url='https://www.reddit.com/r/science/comments/origin/a/')
    candidate = collect([wrapper])['candidates'][0]
    assert candidate['external_id'] == 't3_origin'
    assert candidate['author'] == 'astro_writer'
    assert candidate['rights']['source_author'] == 'astro_writer'
    assert candidate['dedup_keys']['ids'] == ['t3_cross', 't3_origin']
    assert candidate['article']['original'] == original['selftext']
    assert collect([wrapper], rights_grants=[grant(scope='curator')])['candidates'] == []


@pytest.mark.parametrize('change', [
    {'crosspost_parent_list': []},
    {'crosspost_parent_list': [post('wrong')]},
    {'crosspost_parent': 't1_comment'},
    {'over_18': True},
])
def test_crosspost_missing_parent_or_unsafe_wrapper_cannot_be_used(change):
    wrapper = post('cross', is_self=False, crosspost_parent='t3_origin',
                   crosspost_parent_list=[post('origin')])
    wrapper.update(change)
    assert collect([wrapper])['candidates'] == []


def test_crosspost_cycles_and_old_original_are_excluded():
    wrapper = post('cross', is_self=False, crosspost_parent='t3_cross',
                   crosspost_parent_list=[post('cross')])
    assert collect([wrapper])['candidates'] == []
    wrapper = post('cross', is_self=False, crosspost_parent='t3_origin',
                   crosspost_parent_list=[post('origin', created_utc=NOW - 80 * 3600)])
    assert collect([wrapper])['candidates'] == []


def test_dedup_uses_highest_rating_and_full_text_not_only_title():
    low = post('low', title='First heading', selftext=BODY, score=1)
    high = post('high', title='Second heading', selftext=BODY, score=200)
    result = collect([low, high, post('different', selftext=BODY + ' A different complete account.')])
    assert [x['external_id'] for x in result['candidates']] == ['t3_high', 't3_different']
    assert result['diagnostics']['excluded']['duplicate'] == 1


@pytest.mark.parametrize('seen', [
    {'ids': ['abc123']}, {'ids': ['t3_abc123']},
    {'urls': ['https://old.reddit.com/r/SCIENCE/comments/abc123/slug/?utm_source=test']},
    {'text_hashes': [text_hash(BODY + 'abc123')]},
    {'text_hashes': [text_hash('A new discovery of planets\n\n' + BODY + 'abc123')]},
])
def test_persisted_seen_ids_url_variants_and_body_hashes(seen):
    assert collect([post()], seen=seen)['candidates'] == []


def test_crosspost_wrapper_alias_in_seen_prevents_republication():
    wrapper = post('cross', is_self=False, crosspost_parent='t3_origin',
                   crosspost_parent_list=[post('origin')])
    assert collect([wrapper], seen={'ids': ['t3_cross']})['candidates'] == []


def test_oauth_is_not_a_rights_grant():
    result = collect([post()], rights_grants=[])
    assert result['candidates'] == []
    assert result['diagnostics']['excluded'] == {'rights_required': 1}


@pytest.mark.parametrize('change', [
    {'verified': 'true'}, {'allow_derivative': False}, {'allow_storage': False},
    {'allow_redistribution': False}, {'scope': 'different_author'}, {'expires_at': NOW - 1},
])
def test_incomplete_or_unrelated_permission_never_authorizes_text(change):
    assert collect([post()], rights_grants=[grant(**change)])['candidates'] == []


def test_post_permission_is_bound_to_exact_original_hash():
    item = post()
    permission = grant(scope_type='post', scope='abc123',
                       source_hash=text_hash(item['title'] + '\n\n' + item['selftext']))
    candidate = collect([item], rights_grants=[permission])['candidates'][0]
    assert candidate['rights']['scope'] == 't3_abc123'
    assert candidate['rights']['grant_hash']
    assert collect([post(selftext=BODY + ' Edited source text.')],
                   rights_grants=[permission])['candidates'] == []


@pytest.mark.parametrize('permission', [
    grant(scope_type='subreddit', scope='science'), grant(evidence_ref=''),
    grant(scope_type='post', scope='t3_abc123'), grant(expires_at=True),
    grant(cookie='must not enter a candidate'), grant(license=['bad']),
    grant(scope_type=[]), grant(source_hash=123),
])
def test_malformed_rights_policy_is_rejected_before_collection(permission):
    client = Client([post()])
    with pytest.raises(AgentError, match='REDDIT_RIGHTS_POLICY_INVALID'):
        collect_reddit_candidates(client, settings(), now=NOW, rights_grants=[permission])
    assert client.calls == 0


def test_bad_time_and_seen_are_rejected_before_collection():
    client = Client([post()])
    with pytest.raises(AgentError, match='REDDIT_COLLECTION_TIME_INVALID'):
        collect_reddit_candidates(client, settings(), now=True, rights_grants=[grant()])
    with pytest.raises(AgentError, match='REDDIT_DEDUP_STATE_INVALID'):
        collect_reddit_candidates(client, settings(), now=NOW, seen={'ids': ['../bad']},
                                  rights_grants=[grant()])
    assert client.calls == 0


@pytest.mark.parametrize('url', [
    'http://www.reddit.com/r/science/comments/abc123/',
    'https://reddit.com.evil.test/r/science/comments/abc123/',
    '//evil.test/r/science/comments/abc123/',
    'https://user:password@reddit.com/r/science/comments/abc123/',
    'https://www.reddit.com:444/r/science/comments/abc123/',
])
def test_canonical_url_rejects_unsafe_variants(url):
    with pytest.raises(AgentError, match='REDDIT_POST_URL_REQUIRED'):
        canonical_reddit_post_url(url)


def test_expired_oauth_token_is_refreshed_before_next_known_read(monkeypatch):
    clock, calls = [100.0], []
    monkeypatch.setattr('max_postiz_agent.reddit.time.monotonic', lambda: clock[0])

    def handler(request):
        calls.append(request)
        if request.url.path.endswith('access_token'):
            return httpx.Response(200, json={'access_token': 'opaque', 'expires_in': 100})
        return response([post()])

    client = RedditClient(options(monkeypatch), httpx.MockTransport(handler))
    client.collect(settings())
    clock[0] = 171
    client.collect(settings())
    assert len([r for r in calls if r.url.path.endswith('access_token')]) == 2
    assert len(calls) == 4
    client.close()


def test_rate_budget_resets_only_after_reported_window(monkeypatch):
    clock, calls = [100.0], []
    monkeypatch.setattr('max_postiz_agent.reddit.time.monotonic', lambda: clock[0])

    def handler(request):
        calls.append(request)
        if request.url.path.endswith('access_token'):
            return httpx.Response(200, json={'access_token': 'opaque'})
        return httpx.Response(200, headers={'x-ratelimit-remaining': '0', 'x-ratelimit-reset': '30'},
                              json={'data': {'children': [], 'after': None}})

    client = RedditClient(options(monkeypatch), httpx.MockTransport(handler))
    client.collect(settings())
    with pytest.raises(AgentError, match='REDDIT_RATE_LIMIT'):
        client.collect(settings())
    assert len(calls) == 2
    clock[0] = 131
    client.collect(settings())
    assert len(calls) == 3
    client.close()


@pytest.mark.parametrize('value', [
    {'access_token': 'bad\ntoken'}, {'access_token': 'opaque', 'token_type': 'mac'},
    {'access_token': 'opaque', 'expires_in': True},
    {'access_token': 'opaque', 'expires_in': float('inf')},
])
def test_invalid_oauth_response_never_reaches_a_listing(monkeypatch, value):
    calls = []

    def handler(request):
        calls.append(request)
        if value.get('expires_in') == float('inf'):
            return httpx.Response(200, content=b'{"access_token":"opaque","expires_in":1e999}')
        return httpx.Response(200, json=value)

    client = RedditClient(options(monkeypatch), httpx.MockTransport(handler))
    with pytest.raises(AgentError, match='REDDIT_OAUTH_REJECTED'):
        client.collect(settings())
    assert len(calls) == 1
    client.close()
