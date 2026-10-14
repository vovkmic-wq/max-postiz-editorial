import json

import httpx
import pytest

from max_postiz_agent.config import AgentError
from max_postiz_agent.reddit import RedditClient, RedditQuality, eligible, slot_at, text_hash


def options():
    return {'access_approved': True, 'listing': 'https://oauth.reddit.com/r/popular/top',
            'max_candidates': 200, **{n + '_env': 'TEST_REDDIT_' + n.upper()
                                     for n in ('client_id', 'client_secret', 'refresh_token', 'user_agent')}}


def post(**changes):
    return {'id': 'abc123', 'title': 'A story about the discovery of a garden',
            'selftext': 'This is an interesting story about a gardener and the plants in a beautiful garden. '*8,
            'is_self': True, 'permalink': '/r/stories/comments/abc123/a_garden/', **changes}


def test_gate_never_contacts_reddit_without_approval():
    calls = []
    client = RedditClient({**options(), 'access_approved': False},
                          httpx.MockTransport(lambda r: calls.append(r)))
    with pytest.raises(AgentError, match='REDDIT_APPROVAL_REQUIRED'):
        client.candidates()
    assert not calls
    client.close()


@pytest.mark.parametrize('changes', [{'over_18': True}, {'is_self': False},
    {'selftext': '[removed]'}, {'promoted': True}, {'removed_by_category': 'moderator'},
    {'permalink': '//evil.example/path'}, {'id': '../bad'}])
def test_conservative_exclusions(changes):
    assert not eligible(post(**changes))


def test_oauth_pagination_and_popularity_order(monkeypatch):
    opts = options()
    for name in ('client_id', 'client_secret', 'refresh_token', 'user_agent'):
        monkeypatch.setenv(opts[name+'_env'], 'test-'+name)
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path.endswith('access_token'):
            assert b'grant_type=refresh_token' in request.content
            return httpx.Response(200, json={'access_token': 'opaque'})
        assert request.headers['authorization'] == 'Bearer opaque'
        rows = [post(id='first', score=2)] if 'after' not in request.url.params else [post(id='second', score=20)]
        return httpx.Response(200, json={'data': {'children': [{'kind':'t3','data':x} for x in rows],
                        'after': 't3_first' if len(calls)==2 else None}})

    client = RedditClient(opts, httpx.MockTransport(handler))
    assert [x['id'] for x in client.candidates()] == ['second','first']
    assert len(calls) == 3
    client.close()


def test_429_is_not_retried(monkeypatch):
    opts = options()
    for name in ('client_id', 'client_secret', 'refresh_token', 'user_agent'):
        monkeypatch.setenv(opts[name+'_env'], 'test-'+name)
    calls=[]

    def handler(request):
        calls.append(request)
        return httpx.Response(429, json={})

    client=RedditClient(opts,httpx.MockTransport(handler))
    with pytest.raises(AgentError,match='REDDIT_RATE_LIMIT'):
        client.candidates()
    assert len(calls)==1
    client.close()


def test_fixed_moscow_slots_and_hashes():
    value=slot_at(1791969999)
    assert value.hour in (0,4,8,12,16,20) and value.minute==0
    assert text_hash('A  garden\nSTORY')==text_hash('a garden story')


class Model:
    def __init__(self, value):
        self.value=value
        self.calls=0

    def generate_text(self, prompt):
        self.calls+=1
        return json.dumps(self.value,ensure_ascii=False), {}


def test_independent_review_and_spelling_are_both_required():
    translator=Model({'title':'Необычный сад', 'body':'Однажды садовник посадил цветы и рассказал интересную историю о своём прекрасном саде.'})
    reviewer=Model({'approved':True,'topic':'stories','issues':[]})
    quality=RedditQuality(translator,reviewer,
        transport=httpx.MockTransport(lambda r:httpx.Response(200,json={'matches':[]})))
    result=quality.prepare(post())
    assert result['url'] in result['body']
    assert 'с помощью нейросетей' in result['body']
    assert translator.calls==reviewer.calls==1
    quality.close()


def test_spelling_failure_cannot_be_approved_by_model():
    translator=Model({'title':'Необычный сад', 'body':'Садовник рассказал интересную историю о своём прекрасном саде и цветах.'})
    reviewer=Model({'approved':True,'topic':'stories','issues':[]})
    quality=RedditQuality(translator,reviewer,
        transport=httpx.MockTransport(lambda r:httpx.Response(200,json={'matches':[{'message':'Ошибка'}]})))
    with pytest.raises(AgentError,match='REDDIT_QUALITY_REJECTED'):
        quality.prepare(post())
    assert translator.calls==reviewer.calls==3
    quality.close()


def test_invalid_json_is_repaired_within_the_shared_attempt_budget():
    class RepairingModel(Model):
        def generate_text(self, prompt):
            if self.calls == 0:
                self.calls += 1
                return 'Not JSON', {}
            return super().generate_text(prompt)

    translator=RepairingModel({'title':'Необычный сад', 'body':'Садовник рассказал интересную историю о своём прекрасном саде и цветах.'})
    reviewer=Model({'approved':True,'topic':'stories','issues':[]})
    quality=RedditQuality(translator,reviewer,
        transport=httpx.MockTransport(lambda r:httpx.Response(200,json={'matches':[]})))
    assert quality.prepare(post())['topic']=='stories'
    assert translator.calls==2 and reviewer.calls==1
    quality.close()
