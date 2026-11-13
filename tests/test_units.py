import asyncio

import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from app.auth import TokenAuthMiddleware
from app.cache import TranscriptCache
from app.downloader import AudioChunk
from app.errors import InvalidUrlError
from app.ratelimit import SlidingWindowRateLimiter
from app.transcriber import OpenRouterTranscriber
from app.urls import UrlPolicy, ensure_public_host, media_key

TOKEN = "t" * 40


@pytest.fixture(scope="module")
def policy():
    return UrlPolicy()


@pytest.mark.parametrize(
    "url,extractor,media_id,hint",
    [
        ("https://x.com/jack/status/20", "Twitter", "20", None),
        ("https://twitter.com/jack/status/20?s=46&t=abc", "Twitter", "20", None),
        ("x.com/jack/status/20", "Twitter", "20", None),
        ("https://x.com/jack/status/20/video/2", "Twitter", "20", 1),
        ("https://www.youtube.com/watch?v=jNQXAC9IVRw&t=30", "Youtube", "jNQXAC9IVRw", None),
        ("https://youtu.be/jNQXAC9IVRw?si=xyz", "Youtube", "jNQXAC9IVRw", None),
        ("https://www.youtube.com/shorts/jNQXAC9IVRw", "Youtube", "jNQXAC9IVRw", None),
        ("https://www.tiktok.com/@scout2015/video/6718335390845095173", "TikTok", "6718335390845095173", None),
        ("https://example.com/podcast/episode.mp3", "Generic", None, None),
    ],
)
def test_policy_valid(policy, url, extractor, media_id, hint):
    ref = policy.check(url)
    assert (ref.extractor, ref.media_id, ref.index_hint) == (extractor, media_id, hint)
    assert ref.request_key == (f"{extractor}:{media_id}" if media_id else None)
    assert "#" not in ref.url
    if extractor == "Twitter":
        assert "/video/" not in ref.url


@pytest.mark.parametrize(
    "url",
    [
        "",
        "file:///etc/passwd",
        "ftp://example.com/a.mp3",
        "https://user:pw@youtube.com/watch?v=jNQXAC9IVRw",
        "https://youtube.com:8080/watch?v=jNQXAC9IVRw",
        "http://192.168.1.1/video.mp4",
        "http://127.0.0.1/a.mp3",
        "http://[::1]/a.mp3",
        "http://169.254.169.254/latest/meta-data/",
        "http://localhost/a.mp3",
    ],
)
def test_policy_invalid(policy, url):
    with pytest.raises(InvalidUrlError):
        policy.check(url)


def test_policy_restricted_sites():
    p = UrlPolicy(["youtube", "twitter"], allow_generic=False)
    assert p.check("https://youtu.be/jNQXAC9IVRw").extractor == "Youtube"
    assert p.check("https://x.com/jack/status/20").extractor == "Twitter"
    with pytest.raises(InvalidUrlError):
        p.check("https://www.tiktok.com/@scout2015/video/6718335390845095173")
    with pytest.raises(InvalidUrlError):
        p.check("https://example.com/a.mp3")


def test_no_generic():
    p = UrlPolicy(allow_generic=False)
    with pytest.raises(InvalidUrlError):
        p.check("https://example.com/a.mp3")


def test_ensure_public_host(monkeypatch):
    import socket

    def fake(host, *a, **k):
        return [(0, 0, 0, "", ("10.0.0.5" if host == "evil.example" else "93.184.216.34", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    ensure_public_host("https://ok.example/a.mp3")
    with pytest.raises(InvalidUrlError):
        ensure_public_host("https://evil.example/a.mp3")


def test_media_key():
    assert media_key({"extractor_key": "Youtube", "id": "abc"}) == "Youtube:abc"
    a = media_key({"extractor_key": "Generic", "id": "episode", "webpage_url": "https://a.example/episode.mp3"})
    b = media_key({"extractor_key": "Generic", "id": "episode", "webpage_url": "https://b.example/episode.mp3"})
    assert a != b and a.startswith("Generic:episode-")


def test_cache_migration_and_alias(tmp_path):
    import json
    import sqlite3

    db = tmp_path / "c.sqlite3"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE transcripts (tweet_id TEXT, video_index INTEGER, language TEXT, created_at INTEGER, "
        "model TEXT, payload TEXT, PRIMARY KEY (tweet_id, video_index, language))"
    )
    payload = {"meta": {"tweet_id": "20", "author_name": "jack", "text": "hi"}, "transcript": {"text": "x"}}
    conn.execute("INSERT INTO transcripts VALUES ('20', 0, 'auto', 1, 'm', ?)", (json.dumps(payload),))
    conn.commit()
    conn.close()
    cache = TranscriptCache(db)
    got = cache.get("Twitter:20", None)
    assert got["meta"]["platform"] == "Twitter" and got["meta"]["author"] == "jack"
    cache.put(["Youtube:a", "Youtube:a", "Youtube:b"], "fr", "m", {"meta": {}, "transcript": {"language": "fr"}})
    assert cache.get("Youtube:b", "fr") and cache.get("Youtube:a", None) is None
    cache.close()


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
