"""ESO uses its exact publisher archive, without a retry or a generic mirror."""

import pytest
from max_postiz_agent.article_images import image_candidate
from max_postiz_agent.config import AgentError

pytest_plugins = ["test_article_images"]

URL = "https://www.eso.org/public/images/potw2641a/"
CDN = "https://cdn.eso.org/images/wallpaper5/potw2641a.jpg"
ORIGIN = "https://www.eso.org/public/archives/images/wallpaper5/potw2641a.jpg"


def page(href=CDN):
    return (
        '<div class="left-column"><div class="credit">ESO</div></div>'
        + f'<a href="{href}">2048x1536</a>'
    )


def test_eso_selects_primary_official_origin_before_download():
    assert image_candidate("eso_potw", URL, page())["image_url"] == ORIGIN


@pytest.mark.parametrize(
    "href",
    [CDN + "?x=1", CDN.replace("2641", "2642"), CDN.replace("cdn.eso.org", "evil.example"), ORIGIN],
)
def test_only_exact_publisher_cdn_link_authorizes_origin_mapping(href):
    with pytest.raises(AgentError, match="ARTICLE_IMAGE_DOWNLOAD_REQUIRED"):
        image_candidate("eso_potw", URL, page(href))


def test_existing_hubble_image_route_is_unchanged():
    url = "https://esahubble.org/images/potw2641a/"
    image = "https://cdn.esahubble.org/archives/images/wallpaper5/potw2641a.jpg"
    assert image_candidate("esahubble_potw", url, page(image))["image_url"] == image


@pytest.mark.parametrize("status", [200, 206, 302, 403, 429])
def test_full_origin_download_is_one_bounded_request_without_fallback(tmp_path, status):
    import httpx
    import json
    from max_postiz_agent.article_images import _download, MAX_BYTES

    calls = []

    def get(request):
        calls.append(str(request.url))
        assert "range" not in request.headers
        return httpx.Response(
            status, content=b"jpeg-fixture", headers={"content-type": "image/jpeg", "location": CDN}
        )

    claim = tmp_path / "claim.json"
    args = (ORIGIN, tmp_path / "image.jpg", claim)
    options = {"transport": httpx.MockTransport(get), "request_mode": "full-v1"}
    if status == 200:
        result = _download(*args, **options)
        assert result["contract"] == "image-http-full-v1"
        assert result["bytes"] == len(b"jpeg-fixture")
    else:
        with pytest.raises(AgentError):
            _download(*args, **options)
    assert calls == [ORIGIN]
    assert (
        json.loads((tmp_path / "claim.json.part-0.local.json").read_text())["stop"] == MAX_BYTES - 1
    )


@pytest.mark.parametrize("tamper", [False, True])
def test_full_request_mode_is_bound_to_native_proof(illustration, monkeypatch, tamper):
    import json
    import httpx
    from max_postiz_agent.article_images import (
        REGISTRY,
        prepare_article_image,
        validate_article_image,
    )
    from max_postiz_agent.config import fingerprint
    from test_article_images import jpeg

    value = illustration
    monkeypatch.setattr(
        REGISTRY[value.source.source_id], "primary_image_url", lambda url: url, raising=False
    )

    def get(request):
        assert "range" not in request.headers
        return httpx.Response(200, content=jpeg(), headers={"content-type": "image/jpeg"})

    proof = prepare_article_image(
        value.core, value.article, value.record, value.policy, transport=httpx.MockTransport(get)
    )
    assert proof["image_request_mode"] == "full-v1"
    value.record.update(
        illustration=proof, image_credit=proof["credit"], illustration_proof_hash=fingerprint(proof)
    )
    validate_article_image(value.core, value.record)
    if tamper:
        proof["image_request_mode"] = "ranges-v1"
        value.record["illustration_proof_hash"] = fingerprint(proof)
        directory = value.root / "data/article-images"
        (directory / (value.record["id"] + ".proof.local.json")).write_text(json.dumps(proof))
        claim_path = directory / (value.record["id"] + ".claim.local.json")
        claim = json.loads(claim_path.read_text())
        claim["input"]["image_request_mode"] = "ranges-v1"
        claim_path.write_text(json.dumps(claim))
        with pytest.raises(AgentError, match="ARTICLE_IMAGE_PROOF_INVALID"):
            validate_article_image(value.core, value.record)
