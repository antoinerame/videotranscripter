import asyncio

import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from app.auth import TokenAuthMiddleware
from app.downloader import AudioChunk
from app.errors import InvalidUrlError
from app.ratelimit import SlidingWindowRateLimiter
from app.transcriber import OpenRouterTranscriber
from app.xurl import parse_tweet_url

TOKEN = "t" * 40


@pytest.mark.parametrize(
    "url,tweet_id,media",
    [
        ("https://x.com/jack/status/20", "20", None),
        ("https://twitter.com/jack/status/20?s=46&t=abc", "20", None),
        ("x.com/jack/status/20", "20", None),
        ("https://mobile.twitter.com/jack/status/20/", "20", None),
        ("https://x.com/i/status/20", "20", None),
        ("https://x.com/i/web/status/20", "20", None),
        ("https://x.com/jack/status/20/video/2", "20", 2),
        ("https://www.x.com/jack/status/20/photo/1", "20", None),
    ],
)
def test_parse_valid(url, tweet_id, media):
    ref = parse_tweet_url(url)
    assert ref.tweet_id == tweet_id
    assert ref.media_number == media
    assert ref.canonical_url == f"https://x.com/i/status/{tweet_id}"


@pytest.mark.parametrize(
    "url",
    [
        "",
        "https://youtube.com/watch?v=x",
        "https://x.com.evil.com/jack/status/20",
        "https://evilx.com/jack/status/20",
        "https://user:pw@x.com/jack/status/20",
        "https://x.com:8080/jack/status/20",
        "file:///etc/passwd",
        "https://x.com/jack",
        "https://x.com/jack/status/abc",
        "https://api.x.com/jack/status/20",
    ],
)
def test_parse_invalid(url):
    with pytest.raises(InvalidUrlError):
        parse_tweet_url(url)


def test_rate_limiter():
    now = [0.0]
    rl = SlidingWindowRateLimiter(2, 3600, clock=lambda: now[0])
    assert rl.try_acquire()[0]
    assert rl.try_acquire()[0]
    ok, wait = rl.try_acquire()
    assert not ok and wait == pytest.approx(3600)
    now[0] = 3600.0
    assert rl.try_acquire()[0]
    rl.refund()
    assert rl.remaining == 2


def _client():
    async def endpoint(request):
        return PlainTextResponse(f"inner:{request.url.path}")

    inner = Starlette(routes=[Route("/mcp", endpoint, methods=["GET", "POST"])])
    return TestClient(TokenAuthMiddleware(inner, TOKEN))


def test_auth_path_token():
    c = _client()
    r = c.post(f"/mcp/{TOKEN}")
    assert r.status_code == 200 and r.text == "inner:/mcp"
    assert c.post(f"/mcp/{TOKEN}/").status_code == 200
    assert c.post("/mcp/wrong").status_code == 401
    assert c.post(f"/mcp/{TOKEN}x").status_code == 401
    assert c.post(f"/mcp/{TOKEN}/extra").status_code == 401


def test_auth_bearer():
    c = _client()
    assert c.post("/mcp", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200
    assert c.post("/mcp", headers={"Authorization": f"bearer {TOKEN}"}).status_code == 200
    assert c.post("/mcp", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert c.post("/mcp").status_code == 401


def test_auth_other_paths():
    c = _client()
    assert c.get("/health").status_code == 200
    assert c.get("/.well-known/oauth-authorization-server").status_code == 404
    assert c.get("/").status_code == 404


def test_merge_offsets_chunks():
    tr = OpenRouterTranscriber("k", "m", base_url="http://127.0.0.1:1")
    tr._price_per_second = 0.001
    tr._price_fetched_at = 1e18
    chunks = [AudioChunk(None, 0.0, 100.0), AudioChunk(None, 100.0, 50.0)]
    results = [
        {"text": "a b", "language": "french", "segments": [{"start": 0, "end": 5, "text": " a"}, {"start": 5, "end": 9, "text": " b"}], "usage": {"seconds": 100, "cost": 0.1}},
        {"text": "c", "segments": [{"start": 1.5, "end": 4, "text": " c"}], "usage": {"seconds": 50}},
    ]
    res = asyncio.run(tr._merge(chunks, results, None))
    assert [(s.start, s.text) for s in res.segments] == [(0.0, "a"), (5.0, "b"), (101.5, "c")]
    assert res.timestamps and res.language == "french"
    assert res.billed_seconds == 150 and res.estimated_cost == pytest.approx(0.15)
    assert res.reported_cost == pytest.approx(0.1)
    assert res.duration == 150


def test_merge_without_segments():
    tr = OpenRouterTranscriber("k", "m", base_url="http://127.0.0.1:1")
    tr._price_per_second = 0.0
    tr._price_fetched_at = 1e18
    res = asyncio.run(tr._merge([AudioChunk(None, 0.0, 10.0)], [{"text": "hello"}], "en"))
    assert not res.timestamps and res.segments == [] and res.text == "hello" and res.language == "en"


def test_merge_cleans_whitespace():
    tr = OpenRouterTranscriber("k", "m", base_url="http://127.0.0.1:1")
    tr._price_per_second = 0.0
    tr._price_fetched_at = 1e18
    data = {"text": "a  b", "segments": [{"start": 0, "end": 1, "text": "  a  b "}]}
    res = asyncio.run(tr._merge([AudioChunk(None, 0.0, 1.0)], [data], None))
    assert res.text == "a b" and res.segments[0].text == "a b"


def test_provider_routing_config():
    tr = OpenRouterTranscriber("k", "m", provider_order=["primeintellect"], provider_ignore=["deepinfra"])
    assert tr._provider == {"order": ["primeintellect"], "allow_fallbacks": True, "ignore": ["deepinfra"]}
    assert OpenRouterTranscriber("k", "m")._provider == {}
