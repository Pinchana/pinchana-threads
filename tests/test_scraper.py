import json
import os

import pytest

from pinchana_threads import scraper as scraper_module
from pinchana_threads import main as main_module
from pinchana_threads.scraper import (
    HttpExtractionUnavailable,
    NotFoundError,
    RateLimitError,
    ThreadsCloakScraper,
    ThreadsHttpExtractor,
    ThreadsPostParser,
    ThreadsScraper,
)


SHORTCODE = "Example123"
POST = {
    "pk": "123456789",
    "code": SHORTCODE,
    "caption": {"text": "hello & goodbye"},
    "user": {"username": "pinchana"},
    "like_count": 12,
    "text_post_app_info": {
        "reply_count": 3,
        "repost_count": 2,
        "quote_count": 1,
    },
    "image_versions2": {
        "candidates": [{"url": "https://cdn.example/image.jpg"}],
    },
}


class FakeResponse:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text


class FakeSession:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []
        self.closed = False

    async def get(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.error:
            raise self.error
        return self.response

    async def close(self):
        self.closed = True


def make_http_extractor(session):
    extractor = ThreadsHttpExtractor.__new__(ThreadsHttpExtractor)
    extractor._session = session
    extractor._headers = {"Accept": "text/html"}
    return extractor


def post_html(post=POST):
    payload = json.dumps({"require": [["Payload", None, None, [{"thread_items": [{"post": post}]}]]]})
    return f'<html><script type="application/json" data-content-len="1" data-sjs>{payload}</script></html>'


def test_parse_server_rendered_post():
    parsed = ThreadsPostParser.parse_html(post_html(), SHORTCODE)

    assert parsed["code"] == SHORTCODE
    assert parsed["text"] == "hello & goodbye"
    assert parsed["username"] == "pinchana"
    assert parsed["reply_count"] == 3
    assert parsed["media"] == [
        {
            "type": "image",
            "url": "https://cdn.example/image.jpg",
            "width": None,
            "height": None,
        }
    ]


def test_parse_ignores_malformed_and_wrong_post_payloads():
    document = (
        '<script type="application/json" data-sjs>{broken</script>'
        '<script type="application/json" data-sjs>{"code":"Different","pk":"1"}</script>'
    )
    assert ThreadsPostParser.parse_html(document, SHORTCODE) is None


def test_video_is_detected_from_current_media_type_and_versions():
    video_post = {
        **POST,
        "media_type": 2,
        "video_versions": [{"type": 101, "url": "https://cdn.example/video.mp4"}],
    }

    parsed = ThreadsPostParser.parse_html(post_html(video_post), SHORTCODE)

    assert parsed["media"] == [
        {
            "type": "video",
            "url": "https://cdn.example/video.mp4",
            "width": None,
            "height": None,
            "thumbnail_url": "https://cdn.example/image.jpg",
        }
    ]


@pytest.mark.asyncio
async def test_http_extractor_returns_post_without_browser():
    session = FakeSession(FakeResponse(text=post_html()))
    extractor = make_http_extractor(session)

    parsed = await extractor.scrape_post(SHORTCODE)

    assert parsed["code"] == SHORTCODE
    assert len(session.calls) == 1


@pytest.mark.asyncio
async def test_video_download_includes_mp4_and_cover(monkeypatch, tmp_path):
    async def fake_download(url, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"media")
        return True

    monkeypatch.setattr(main_module.storage, "base_path", tmp_path)
    monkeypatch.setattr(main_module.storage, "download", fake_download)

    items = await main_module._download_media(
        SHORTCODE,
        [
            {
                "type": "video",
                "url": "https://cdn.example/video.mp4",
                "thumbnail_url": "https://cdn.example/cover.jpg",
            }
        ],
    )

    assert items[0].media_type == "video"
    assert items[0].video_url == f"/media/threads/{SHORTCODE}/media_0.mp4"
    assert items[0].thumbnail_url == f"/media/threads/{SHORTCODE}/media_0.jpg"


@pytest.mark.asyncio
async def test_download_uses_detected_webp_extension(monkeypatch, tmp_path):
    async def fake_download(url, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"RIFF\x04\x00\x00\x00WEBP")
        return True

    monkeypatch.setattr(main_module.storage, "base_path", tmp_path)
    monkeypatch.setattr(main_module.storage, "download", fake_download)

    items = await main_module._download_media(
        SHORTCODE,
        [{"type": "image", "url": "https://cdn.example/misleading.jpg"}],
    )

    assert items[0].thumbnail_url == f"/media/threads/{SHORTCODE}/media_0.webp"
    assert (tmp_path / SHORTCODE / "media_0.webp").exists()


@pytest.mark.asyncio
async def test_http_extractor_distinguishes_not_found_and_gating():
    not_found = make_http_extractor(FakeSession(FakeResponse(status_code=404)))
    with pytest.raises(NotFoundError):
        await not_found.scrape_post(SHORTCODE)

    gated = make_http_extractor(FakeSession(FakeResponse(status_code=200, text="<html>login</html>")))
    with pytest.raises(HttpExtractionUnavailable, match="missing_post_payload"):
        await gated.scrape_post(SHORTCODE)


class StubExtractor:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = 0
        self.closed = False

    async def scrape_post(self, shortcode):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_composite_uses_browser_only_when_http_is_unavailable():
    http = StubExtractor(error=HttpExtractionUnavailable("gated", 200))
    browser = StubExtractor(result={"code": SHORTCODE})
    scraper = ThreadsScraper(http, browser)

    assert await scraper.scrape_post(SHORTCODE) == {"code": SHORTCODE}
    assert http.calls == 1
    assert browser.calls == 1
    assert scraper.metrics_snapshot() == {
        "http_unavailable": 1,
        "fallback_gated": 1,
        "browser_success": 1,
    }

    await scraper.close()
    assert http.closed and browser.closed


class FakeNavigation:
    status = 200


class FakePage:
    def __init__(self):
        self.closed = False

    def on(self, event, callback):
        self.callback = callback

    async def goto(self, *args, **kwargs):
        return FakeNavigation()

    async def evaluate(self, script):
        return [{"nested": POST}]

    async def close(self):
        self.closed = True


class FakeBrowser:
    def __init__(self):
        self.pages = []
        self.closed = False

    async def new_page(self):
        page = FakePage()
        self.pages.append(page)
        return page

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_browser_process_is_reused_and_pages_are_closed(monkeypatch):
    browser = FakeBrowser()
    launches = 0

    async def fake_launch(**kwargs):
        nonlocal launches
        launches += 1
        return browser

    monkeypatch.setattr(scraper_module, "launch_async", fake_launch)
    fallback = ThreadsCloakScraper()

    assert (await fallback.scrape_post(SHORTCODE))["code"] == SHORTCODE
    assert (await fallback.scrape_post(SHORTCODE))["code"] == SHORTCODE
    assert launches == 1
    assert len(browser.pages) == 2
    assert all(page.closed for page in browser.pages)

    await fallback.close()
    assert browser.closed


@pytest.mark.asyncio
async def test_http_server_errors_preserve_rate_limit_classification():
    extractor = make_http_extractor(FakeSession(FakeResponse(status_code=503)))
    with pytest.raises(RateLimitError):
        await extractor.scrape_post(SHORTCODE)


@pytest.mark.asyncio
async def test_live_http_extraction_when_enabled():
    shortcode = os.getenv("PINCHANA_THREADS_LIVE_SHORTCODE")
    if not shortcode:
        pytest.skip("set PINCHANA_THREADS_LIVE_SHORTCODE to enable the live smoke test")

    extractor = ThreadsHttpExtractor()
    try:
        parsed = await extractor.scrape_post(shortcode)
    finally:
        await extractor.close()

    assert parsed["code"] == shortcode
