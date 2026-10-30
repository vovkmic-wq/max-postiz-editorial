"""One bounded native edit retains the existing post without scheduling it."""
import copy
import json

import httpx
import pytest

from max_postiz_agent.config import AgentError
from max_postiz_agent.max_bridge import MaxBridge
from max_postiz_agent.postiz import PostizClient


@pytest.fixture
def edit_client(monkeypatch):
    monkeypatch.setenv("POSTIZ_BASE_URL", "http://127.0.0.1:9999/public/v1")
    monkeypatch.setenv("POSTIZ_API_KEY", "fixture-key")
    remote = {"id": "same-post", "state": "DRAFT", "content": "Old text",
              "image": [], "childrenPost": [], "publishDate": "2026-10-20T21:05:00Z",
              "group": "old-group", "integration": {"id": "max-integration"},
              "settings": json.dumps({"__type": "max-browser", "fixture": "preserved"})}
    calls, result = [], [[{"postId": "same-post"}]]

    def respond(request):
        calls.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"posts": [copy.deepcopy(remote)]})
        if isinstance(result[0], Exception):
            raise result[0]
        return httpx.Response(200, json=result[0], headers={
            "X-Max-Postiz-CAS-Verified": request.headers.get("X-Max-Postiz-Expected-Hash", "")})

    client = PostizClient({}, httpx.MockTransport(respond))
    yield client, remote, calls, result
    client.close()


def test_native_edit_preserves_same_draft_and_escapes_literal_text(edit_client):
    client, remote, calls, _ = edit_client
    expected_hash = MaxBridge.normalize(remote)[3]
    assert client.update_existing_draft("same-post", "Old text", "New <text> & facts\nNext line",
                                         expected_hash=expected_hash) == "same-post"
    assert [call.method for call in calls] == ["GET", "POST"]
    assert calls[-1].headers["X-Max-Postiz-Expected-Hash"] == expected_hash
    payload = json.loads(calls[-1].content)
    assert payload["type"] == "draft" and payload["republish"] is False
    assert payload["date"] == remote["publishDate"]
    assert payload["posts"] == [{"integration": remote["integration"], "group": remote["group"],
        "settings": json.loads(remote["settings"]), "value": [{"id": "same-post",
        "content": "New &lt;text&gt; &amp; facts\nNext line", "image": []}]}]
    after = {**remote, "content": payload["posts"][0]["value"][0]["content"]}
    assert MaxBridge.normalize(after)[0] == "New <text> & facts\nNext line"


@pytest.mark.parametrize("change", [
    {"state": "QUEUE"}, {"state": "PUBLISHED"}, {"content": "Concurrent edit"},
    {"image": [{"id": "media"}]}, {"settings": "malformed"},
    {"settings": {"__type": "telegram"}}, {"publishDate": "2026-10-20T22:05:00Z"},
    {"childrenPost": [{"id": "thread"}]}, {"intervalInDays": 1},
])
def test_changed_or_unsupported_remote_never_posts(edit_client, change):
    client, remote, calls, _ = edit_client
    expected_hash = MaxBridge.normalize(remote)[3]
    remote.update(change)
    with pytest.raises(AgentError):
        client.update_existing_draft("same-post", "Old text", "Corrected text", expected_hash=expected_hash)
    assert [call.method for call in calls] == ["GET"]


@pytest.mark.parametrize("result", [[{"postId": "different"}], [], {},
    httpx.ReadTimeout("opaque timeout")])
def test_unknown_result_is_not_retried(edit_client, result):
    client, remote, calls, answer = edit_client
    answer[0] = result
    with pytest.raises(AgentError, match="POSTIZ_UNKNOWN"):
        client.update_existing_draft("same-post", "Old text", "Corrected text",
                                     expected_hash=MaxBridge.normalize(remote)[3])
    assert [call.method for call in calls] == ["GET", "POST"]


@pytest.mark.parametrize("post_id,body", [("../other", "Changed"), ("same-post", "Old text"),
                                        ("same-post", " ")])
def test_invalid_or_unchanged_candidate_never_calls_api(edit_client, post_id, body):
    client, _, calls, _ = edit_client
    with pytest.raises(AgentError, match="ARTICLE_CORRECTION_INVALID"):
        client.update_existing_draft(post_id, "Old text", body, expected_hash="0" * 64)
    assert not calls


def test_missing_native_cas_ack_is_unknown_without_retry(edit_client):
    client, remote, calls, _ = edit_client
    expected_hash = MaxBridge.normalize(remote)[3]
    client.client.close()

    def respond(request):
        calls.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"posts": [remote]})
        return httpx.Response(200, json=[{"postId": "same-post"}])

    client.client = httpx.Client(transport=httpx.MockTransport(respond))
    with pytest.raises(AgentError, match="POSTIZ_UNKNOWN"):
        client.update_existing_draft("same-post", "Old text", "Corrected text", expected_hash=expected_hash)
    assert [call.method for call in calls] == ["GET", "POST"]
