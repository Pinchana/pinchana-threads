"""
Threads GraphQL scraper — lightweight, no-browser extraction.

Uses curl_cffi with JA3 impersonation to hit Threads' internal
Barcelona GraphQL endpoints after bootstrapping session cookies.
"""

import json
import logging
import urllib.parse
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from curl_cffi.requests import AsyncSession
from pinchana_core.vpn import GluetunController, VpnRotationError

logger = logging.getLogger(__name__)


class ScraperError(Exception):
    """Base exception for extraction logic failures."""
    pass


class RateLimitError(ScraperError):
    """Exception indicating network-level blocking (429/403)."""
    pass


class NotFoundError(ScraperError):
    """Exception indicating the requested resource does not exist."""
    pass


gluetun = GluetunController()


async def trigger_rotation(retry_state):
    """Trigger VPN IP rotation before each retry."""
    logger.warning(f"Retry attempt {retry_state.attempt_number}. Rotating VPN IP...")
    try:
        await gluetun.rotate_ip()
    except VpnRotationError as e:
        logger.warning(f"VPN rotation failed: {e}")
        raise RateLimitError(str(e))


class ThreadsGraphScraper:
    """
    Scrapes Threads.net via its internal GraphQL API.

    The web app uses Relay with `doc_id` parameters.  Public data
    (profiles, posts, replies) can be fetched without authentication
    by setting the correct `x-ig-app-id` header and bootstrapping a
    session from the homepage to obtain a CSRF token.
    """

    BASE_URL = "https://www.threads.net"
    GRAPHQL_ENDPOINT = f"{BASE_URL}/api/graphql"

    # IG App ID required by Threads web backend.
    IG_APP_ID = "238260118697367"

    # Known doc_ids (volatile — Meta rotates these).
    DOC_IDS = {
        "user_profile": "23996318473300828",
        "user_threads": "6232751443445612",
        "user_replies": "6307072669391286",
        "post_detail": "5587632691339264",
        "post_media": "9360915773983802",
    }

    def __init__(self):
        self.base_headers = {
            "x-ig-app-id": self.IG_APP_ID,
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Origin": self.BASE_URL,
            "Referer": f"{self.BASE_URL}/",
            "x-requested-with": "XMLHttpRequest",
        }

    def _is_network_timeout(self, e: Exception) -> bool:
        error_msg = str(e).lower()
        return any(x in error_msg for x in ("timeout", "timed out", "connection", "curl: (28)"))

    async def _bootstrap_session(self, session: AsyncSession):
        """
        Harvest CSRF token and session cookies from Threads homepage.

        This is the critical anti-detection step: we warm up the TCP/TLS
        connection and collect the cookies the backend expects on GraphQL
        requests, all without launching a browser.
        """
        try:
            response = await session.get(self.BASE_URL, timeout=15)
        except Exception as e:
            if self._is_network_timeout(e):
                raise RateLimitError(f"Network timeout during bootstrap: {e}")
            raise
        response.raise_for_status()

        csrf_token = session.cookies.get("csrftoken")
        if csrf_token:
            self.base_headers["x-csrftoken"] = csrf_token
        else:
            logger.warning("Failed to extract CSRF token during bootstrap.")

    def _build_payload(self, doc_id: str, variables: dict) -> str:
        """Encode GraphQL variables and doc_id for application/x-www-form-urlencoded."""
        payload = {
            "doc_id": doc_id,
            "variables": json.dumps(variables),
        }
        return urllib.parse.urlencode(payload)

    @retry(
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=1.5, min=4, max=30),
        retry=retry_if_exception_type(RateLimitError),
        before_sleep=trigger_rotation,
    )
    async def _graphql_request(self, doc_id: str, variables: dict) -> dict:
        """Execute a single GraphQL request with impersonation and retry logic."""
        async with AsyncSession(impersonate="chrome124") as session:
            await self._bootstrap_session(session)

            headers = self.base_headers.copy()
            headers["Content-Type"] = "application/x-www-form-urlencoded"

            data = self._build_payload(doc_id, variables)

            try:
                response = await session.post(
                    self.GRAPHQL_ENDPOINT,
                    headers=headers,
                    data=data,
                    timeout=15,
                )
            except Exception as e:
                if self._is_network_timeout(e):
                    raise RateLimitError(f"Network timeout, will retry: {e}")
                raise

            if response.status_code in {401, 403, 429}:
                raise RateLimitError(f"HTTP {response.status_code}: IP restriction detected.")

            response.raise_for_status()
            return response.json()

    # ------------------------------------------------------------------
    # Public extraction helpers
    # ------------------------------------------------------------------

    async def get_user_profile(self, user_id: str) -> dict:
        """Fetch profile metadata for a numeric user ID."""
        data = await self._graphql_request(
            self.DOC_IDS["user_profile"],
            {"userID": user_id},
        )
        user = data.get("data", {}).get("userData", {}).get("user")
        if user is None:
            raise NotFoundError(f"User {user_id} not found or is private.")
        return user

    async def get_user_threads(self, user_id: str, after: str | None = None) -> dict:
        """Fetch a page of threads (posts) for a user."""
        variables: dict = {"userID": user_id}
        if after:
            variables["after"] = after
        return await self._graphql_request(self.DOC_IDS["user_threads"], variables)

    async def get_user_replies(self, user_id: str, after: str | None = None) -> dict:
        """Fetch a page of replies for a user."""
        variables: dict = {"userID": user_id}
        if after:
            variables["after"] = after
        return await self._graphql_request(self.DOC_IDS["user_replies"], variables)

    async def get_post(self, post_id: str) -> dict:
        """Fetch full post detail by numeric post ID."""
        data = await self._graphql_request(
            self.DOC_IDS["post_detail"],
            {"postID": post_id},
        )
        post = data.get("data", {}).get("mediaData", {}).get("threads")
        if post is None:
            raise NotFoundError(f"Post {post_id} not found or is private.")
        return post

    # ------------------------------------------------------------------
    # Parsers
    # ------------------------------------------------------------------

    @staticmethod
    def parse_user_profile(raw: dict) -> dict:
        """Flatten a raw user profile GraphQL node."""
        return {
            "user_id": raw.get("pk"),
            "username": raw.get("username"),
            "full_name": raw.get("full_name"),
            "biography": raw.get("biography"),
            "profile_pic_url": raw.get("profile_pic_url"),
            "follower_count": raw.get("follower_count"),
            "following_count": raw.get("following_count"),
            "is_verified": raw.get("is_verified"),
            "is_private": raw.get("is_private"),
        }

    @staticmethod
    def parse_thread_item(raw: dict) -> dict:
        """Flatten a single thread/post node."""
        text_post_app_info = raw.get("text_post_app_info") or {}
        user = raw.get("user") or {}
        caption = raw.get("caption")

        # Extract link preview attachment URL if present
        link_preview = text_post_app_info.get("link_preview_attachment") or {}
        link_url = link_preview.get("url")

        return {
            "post_id": raw.get("pk"),
            "code": raw.get("code"),
            "url": f"https://www.threads.net/t/{raw.get('code')}" if raw.get("code") else None,
            "text": caption.get("text") if caption else None,
            "taken_at": raw.get("taken_at"),
            "like_count": raw.get("like_count"),
            "reply_count": text_post_app_info.get("reply_count"),
            "repost_count": text_post_app_info.get("repost_count"),
            "quote_count": text_post_app_info.get("quote_count"),
            "username": user.get("username"),
            "user_pic": user.get("profile_pic_url"),
            "link": link_url,
            "media": ThreadsGraphScraper._parse_media_items(raw),
        }

    @staticmethod
    def _parse_media_items(raw: dict) -> list[dict]:
        """Extract image / video URLs from a post node."""
        items = []
        carousel = raw.get("carousel_media") or []
        if carousel:
            for node in carousel:
                items.append(ThreadsGraphScraper._media_node(node))
        else:
            items.append(ThreadsGraphScraper._media_node(raw))
        return items

    @staticmethod
    def _media_node(node: dict) -> dict:
        """Single media item parser."""
        is_video = node.get("is_video", False)
        return {
            "type": "video" if is_video else "image",
            "url": node.get("video_url") if is_video else node.get("image_versions2", {}).get("candidates", [{}])[0].get("url"),
            "width": node.get("original_width"),
            "height": node.get("original_height"),
        }
