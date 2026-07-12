"""HTTP-first Threads scraper with a pooled stealth-browser fallback."""

import asyncio
import html
import json
import logging
import os
import time
from collections import Counter
from html.parser import HTMLParser
from typing import Optional

from curl_cffi.requests import AsyncSession
from cloakbrowser import launch_async

logger = logging.getLogger(__name__)


class ScraperError(Exception):
    """Base exception for extraction logic failures."""
    pass


class RateLimitError(ScraperError):
    """Exception indicating network-level blocking (429/403/timeout)."""
    pass


class NotFoundError(ScraperError):
    """Exception indicating the requested resource does not exist."""
    pass


class HttpExtractionUnavailable(ScraperError):
    """The page could not be extracted safely without rendering JavaScript."""

    def __init__(self, reason: str, status_code: int | None = None):
        super().__init__(reason)
        self.reason = reason
        self.status_code = status_code


class _DataSjsParser(HTMLParser):
    """Collect JSON bodies from Threads' server-rendered data-sjs scripts."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._capturing = False
        self._parts: list[str] = []
        self.payloads: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if (
            tag.lower() == "script"
            and attributes.get("type") == "application/json"
            and "data-sjs" in attributes
        ):
            self._capturing = True
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._capturing:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self._capturing:
            self.payloads.append("".join(self._parts))
            self._capturing = False
            self._parts = []


class ThreadsPostParser:
    """Parse post nodes shared by the HTTP and browser extractors."""

    @classmethod
    def find_post_in_json(cls, obj, shortcode: str) -> Optional[dict]:
        if isinstance(obj, dict):
            code = obj.get("code") or obj.get("shortcode")
            if code == shortcode and ("pk" in obj or "id" in obj or "user" in obj):
                return obj
            for value in obj.values():
                found = cls.find_post_in_json(value, shortcode)
                if found:
                    return found
        elif isinstance(obj, list):
            for item in obj:
                found = cls.find_post_in_json(item, shortcode)
                if found:
                    return found
        return None

    @classmethod
    def parse_html(cls, document: str, shortcode: str) -> Optional[dict]:
        parser = _DataSjsParser()
        parser.feed(document)
        parser.close()

        for payload in parser.payloads:
            try:
                data = json.loads(payload)
            except (TypeError, json.JSONDecodeError):
                continue
            candidate = cls.find_post_in_json(data, shortcode)
            if candidate:
                return ThreadsCloakScraper.parse_thread_item(candidate)
        return None


class ThreadsHttpExtractor:
    """Fetch the server-rendered post payload without launching a browser."""

    BASE_URL = "https://www.threads.com"

    def __init__(self, impersonate: str = "chrome"):
        self._session = AsyncSession(
            impersonate=impersonate,
            max_clients=int(os.getenv("THREADS_HTTP_CONCURRENCY", "20")),
        )
        self._headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Cache-Control": "no-cache",
        }

    async def scrape_post(self, shortcode: str) -> dict:
        url = f"{self.BASE_URL}/t/{shortcode}"
        started = time.monotonic()
        try:
            response = await self._session.get(
                url,
                headers={**self._headers, "Referer": f"{self.BASE_URL}/"},
                timeout=15,
            )
        except Exception as exc:
            logger.warning("Threads HTTP fetch failed for %s: %s", shortcode, exc)
            raise HttpExtractionUnavailable("transport_error") from exc

        elapsed_ms = round((time.monotonic() - started) * 1000)
        status = response.status_code
        logger.info("Threads HTTP fetch code=%s status=%s duration_ms=%s", shortcode, status, elapsed_ms)

        if status == 404:
            raise NotFoundError(f"Post {shortcode} not found or is private")
        if status in (401, 403, 429):
            raise HttpExtractionUnavailable(f"http_{status}", status)
        if status >= 500:
            raise RateLimitError(f"Threads returned HTTP {status}")
        if status >= 400:
            raise HttpExtractionUnavailable(f"http_{status}", status)

        parsed = ThreadsPostParser.parse_html(response.text, shortcode)
        if parsed:
            logger.info("Threads extraction path=http code=%s duration_ms=%s", shortcode, elapsed_ms)
            return parsed
        raise HttpExtractionUnavailable("missing_post_payload", status)

    async def close(self) -> None:
        await self._session.close()


class ThreadsCloakScraper:
    """
    Scrapes Threads.net via CloakBrowser (stealth Chromium).

    Uses a real browser to render the SPA, intercepts GraphQL responses,
    and falls back to hidden DOM JSON if interception fails.
    """

    BASE_URL = "https://www.threads.com"

    def __init__(self):
        self._impersonate = "chrome"
        self._browser = None
        self._launch_lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(int(os.getenv("THREADS_BROWSER_CONCURRENCY", "2")))

    async def _get_browser(self):
        if self._browser is not None:
            return self._browser

        async with self._launch_lock:
            if self._browser is not None:
                return self._browser
            try:
                logger.info("Launching shared CloakBrowser")
                self._browser = await launch_async(
                    headless=True,
                    humanize=True,
                    args=[
                        "--disable-gpu",
                        "--window-size=1280,720",
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                    ],
                )
            except Exception as exc:
                logger.error("CloakBrowser launch failed: %s", exc)
                raise RateLimitError(f"Browser launch failed: {exc}") from exc
            return self._browser

    async def scrape_post(self, shortcode: str) -> dict:
        """
        Scrape a Threads post by its URL shortcode.

        Returns a flattened dict compatible with parse_thread_item().
        """
        url = f"{self.BASE_URL}/t/{shortcode}"
        async with self._semaphore:
            return await self._scrape_with_page(shortcode, url)

    async def _scrape_with_page(self, shortcode: str, url: str) -> dict:
        browser = await self._get_browser()
        page = None
        started = time.monotonic()
        try:
            page = await browser.new_page()
            graphql_responses: list[dict] = []
            response_tasks: list[asyncio.Task] = []

            async def _capture(response):
                try:
                    if "api/graphql" in response.url:
                        body = await response.json()
                        graphql_responses.append(body)
                except Exception:
                    pass

            def _on_response(response):
                task = asyncio.create_task(_capture(response))
                response_tasks.append(task)

            page.on("response", _on_response)

            logger.info("Navigating to %s", url)
            nav_response = await page.goto(url, wait_until="networkidle", timeout=30000)

            # Playwright does not raise on HTTP 403/429 — it loads the blocked
            # page normally. A clean IP-block therefore looked like "post not
            # found" and never triggered VPN rotation. Check the nav status
            # explicitly so the endpoint retry loop + rotation fires.
            if nav_response is not None:
                status = nav_response.status
                if status in (401, 403, 429) or status >= 500:
                    raise RateLimitError(f"Threads returned HTTP {status} (IP block detected)")

            # Let callbacks run without imposing the previous fixed delay.
            await asyncio.sleep(0)
            if response_tasks:
                await asyncio.gather(*response_tasks, return_exceptions=True)

            # 1. Try intercepted GraphQL responses
            thread_data: Optional[dict] = None
            for resp in graphql_responses:
                candidate = self._extract_post_from_graphql(resp, shortcode)
                if candidate:
                    thread_data = candidate
                    logger.info("Extracted post %s from GraphQL interception", shortcode)
                    break

            # 2. Fallback: hidden JSON in DOM
            if not thread_data:
                logger.info("GraphQL interception empty; trying DOM fallback for %s", shortcode)
                thread_data = await self._extract_from_dom(page, shortcode)

            if not thread_data:
                raise NotFoundError(f"Post {shortcode} not found or is private")

            logger.info(
                "Threads extraction path=browser code=%s duration_ms=%s",
                shortcode,
                round((time.monotonic() - started) * 1000),
            )
            return thread_data

        except (NotFoundError, RateLimitError):
            raise
        except Exception as e:
            error_msg = str(e).lower()
            if any(x in error_msg for x in ("timeout", "timed out", "net::")):
                raise RateLimitError(f"Page load error: {e}") from e
            raise ScraperError(f"Extraction failed: {e}") from e
        finally:
            if page is not None:
                try:
                    await page.close()
                except Exception as exc:
                    logger.warning("Browser page close error: %s", exc)

    async def close(self) -> None:
        async with self._launch_lock:
            browser, self._browser = self._browser, None
            if browser is not None:
                try:
                    await browser.close()
                except Exception as exc:
                    logger.warning("Browser close error: %s", exc)

    # ------------------------------------------------------------------
    # Extraction helpers
    # ------------------------------------------------------------------

    def _extract_post_from_graphql(self, resp: dict, shortcode: str) -> Optional[dict]:
        """Try to extract the post node from a captured GraphQL response."""
        try:
            data = resp.get("data", {})

            # Shape A: mediaData.threads (post detail)
            candidate = (
                data.get("mediaData", {}).get("threads")
                or data.get("mediaData")
            )
            if candidate:
                if isinstance(candidate, list):
                    candidate = candidate[0]
                if isinstance(candidate, dict) and "thread_items" in candidate:
                    candidate = candidate["thread_items"][0].get("post", {})
                code = candidate.get("code") or candidate.get("shortcode")
                if code == shortcode:
                    return self.parse_thread_item(candidate)

            # Shape B: thread_items array
            items = data.get("thread_items") or []
            for item in items:
                post = item.get("post") if isinstance(item, dict) else None
                if post and (post.get("code") == shortcode or post.get("shortcode") == shortcode):
                    return self.parse_thread_item(post)

            # Shape C: data contains the post directly
            code = data.get("code") or data.get("shortcode")
            if code == shortcode:
                return self.parse_thread_item(data)

        except Exception:
            pass
        return None

    async def _extract_from_dom(self, page, shortcode: str) -> Optional[dict]:
        """Extract post data from hidden <script data-sjs> JSON tags."""
        try:
            scripts_data = await page.evaluate("""() => {
                const nodes = document.querySelectorAll('script[type="application/json"][data-sjs]');
                const out = [];
                nodes.forEach(n => {
                    try { out.push(JSON.parse(n.innerText)); } catch(e) {}
                });
                return out;
            }""")
        except Exception as e:
            logger.warning("DOM script extraction failed: %s", e)
            return None

        for data in scripts_data:
            candidate = self._find_post_in_json(data, shortcode)
            if candidate:
                logger.info("Extracted post %s from DOM JSON", shortcode)
                return self.parse_thread_item(candidate)
        return None

    def _find_post_in_json(self, obj, shortcode: str) -> Optional[dict]:
        """Recursively search a JSON object for a post node matching the shortcode."""
        if isinstance(obj, dict):
            code = obj.get("code") or obj.get("shortcode")
            if code == shortcode:
                # Verify it looks like a post node (has pk or id)
                if "pk" in obj or "id" in obj or "user" in obj:
                    return obj
            for v in obj.values():
                found = self._find_post_in_json(v, shortcode)
                if found:
                    return found
        elif isinstance(obj, list):
            for item in obj:
                found = self._find_post_in_json(item, shortcode)
                if found:
                    return found
        return None

    # ------------------------------------------------------------------
    # Parsers
    # ------------------------------------------------------------------

    @staticmethod
    def parse_thread_item(raw: dict) -> dict:
        """Flatten a single thread/post node."""
        text_post_app_info = raw.get("text_post_app_info") or {}
        user = raw.get("user") or {}
        caption = raw.get("caption")

        # Extract link preview attachment URL if present
        link_preview = text_post_app_info.get("link_preview_attachment") or {}
        link_url = link_preview.get("url")

        caption_text = caption.get("text") if caption else None
        caption_html, text_spoiler = ThreadsCloakScraper._build_caption_html(text_post_app_info, caption_text)

        return {
            "post_id": raw.get("pk"),
            "code": raw.get("code"),
            "url": f"https://www.threads.com/t/{raw.get('code')}" if raw.get("code") else None,
            "text": caption_text,
            "text_html": caption_html,
            "text_spoiler": text_spoiler,
            "taken_at": raw.get("taken_at"),
            "like_count": raw.get("like_count"),
            "reply_count": text_post_app_info.get("reply_count") or text_post_app_info.get("direct_reply_count"),
            "repost_count": text_post_app_info.get("repost_count"),
            "quote_count": text_post_app_info.get("quote_count"),
            "username": user.get("username"),
            "link": link_url,
            "spoiler": bool(text_post_app_info.get("is_spoiler_media")),
            "media": ThreadsCloakScraper._parse_media_items(raw),
        }

    @staticmethod
    def _build_caption_html(text_post_app_info: dict, fallback_text: str | None) -> tuple[str | None, bool]:
        """Build Telegram-safe caption HTML with spoiler fragments preserved."""
        fragments = (text_post_app_info.get("text_fragments") or {}).get("fragments") or []
        if not fragments:
            return (html.escape(fallback_text) if fallback_text else None, False)

        parts: list[str] = []
        has_spoiler = False

        for fragment in fragments:
            if not isinstance(fragment, dict):
                continue

            text = fragment.get("plaintext")
            if text is None:
                # Fallback for non-plaintext fragments
                text = fragment.get("linkified_web_url") or ""

            escaped = html.escape(str(text))
            style = fragment.get("styling_info") or {}
            if style.get("is_spoiler"):
                has_spoiler = True
                parts.append(f"<tg-spoiler>{escaped}</tg-spoiler>")
            else:
                parts.append(escaped)

        caption_html = "".join(parts).strip() or (html.escape(fallback_text) if fallback_text else None)
        return caption_html, has_spoiler

    @staticmethod
    def _parse_media_items(raw: dict) -> list[dict]:
        """Extract image / video URLs from a post node."""
        items = []
        carousel = raw.get("carousel_media") or []
        if carousel:
            for node in carousel:
                parsed = ThreadsCloakScraper._media_node(node)
                if parsed.get("url"):
                    items.append(parsed)
        else:
            parsed = ThreadsCloakScraper._media_node(raw)
            if parsed.get("url"):
                items.append(parsed)
        return items

    @staticmethod
    def _media_node(node: dict) -> dict:
        """Single media item parser."""
        video_url = node.get("video_url")
        if not video_url:
            video_versions = node.get("video_versions") or []
            if isinstance(video_versions, list) and video_versions:
                first = video_versions[0] or {}
                video_url = first.get("url") if isinstance(first, dict) else None

        # Current server-rendered payloads identify videos with media_type=2
        # and video_versions, but often omit the legacy is_video boolean.
        is_video = bool(node.get("is_video") or node.get("media_type") == 2 or video_url)

        image_url = None
        image_versions = node.get("image_versions2") or {}
        candidates = image_versions.get("candidates") or []
        if isinstance(candidates, list) and candidates:
            first = candidates[0] or {}
            if isinstance(first, dict):
                image_url = first.get("url")

        parsed = {
            "type": "video" if is_video else "image",
            "url": video_url if is_video else image_url,
            "width": node.get("original_width"),
            "height": node.get("original_height"),
        }
        if is_video:
            parsed["thumbnail_url"] = image_url
        return parsed

    # ------------------------------------------------------------------
    # Direct media download (kept for compatibility)
    # ------------------------------------------------------------------

    async def download_media(self, url: str, dest: str) -> None:
        """Download a single media file via curl_cffi through the VPN."""
        async with AsyncSession(impersonate=self._impersonate) as session:
            resp = await session.get(url, timeout=30)
            resp.raise_for_status()
            with open(dest, "wb") as f:
                f.write(resp.content)


class ThreadsScraper:
    """Use server-rendered JSON first and render only gated responses."""

    def __init__(
        self,
        http_extractor: ThreadsHttpExtractor | None = None,
        browser_fallback: ThreadsCloakScraper | None = None,
    ):
        self.http = http_extractor or ThreadsHttpExtractor()
        self.browser = browser_fallback or ThreadsCloakScraper()
        self._metrics: Counter[str] = Counter()

    async def scrape_post(self, shortcode: str) -> dict:
        try:
            result = await self.http.scrape_post(shortcode)
            self._metrics["http_success"] += 1
            return result
        except HttpExtractionUnavailable as exc:
            self._metrics["http_unavailable"] += 1
            self._metrics[f"fallback_{exc.reason}"] += 1
            logger.warning(
                "Threads extraction fallback code=%s reason=%s status=%s",
                shortcode,
                exc.reason,
                exc.status_code,
            )
            try:
                result = await self.browser.scrape_post(shortcode)
                self._metrics["browser_success"] += 1
                return result
            except Exception as browser_exc:
                self._metrics[f"browser_error_{type(browser_exc).__name__}"] += 1
                raise
        except Exception as exc:
            self._metrics[f"http_error_{type(exc).__name__}"] += 1
            raise

    def metrics_snapshot(self) -> dict[str, int]:
        return dict(self._metrics)

    async def close(self) -> None:
        await asyncio.gather(self.http.close(), self.browser.close())
