"""Threads scraper plugin — mounts as a FastAPI router."""

import asyncio
import logging
import os
import re
from typing import Optional
from fastapi import APIRouter, HTTPException, FastAPI
from fastapi.responses import FileResponse
from pinchana_core.models import ScrapeRequest, ScrapeResponse, MediaItem
from pinchana_core.storage import MediaStorage
from pinchana_core.vpn import GluetunController, VpnRotationError
from pinchana_core.plugins import ScraperPlugin, registry
from .scraper import ThreadsCloakScraper, RateLimitError, NotFoundError


class ThreadsScrapeResponse(ScrapeResponse):
    """Extended response for Threads including link preview and engagement."""
    link: Optional[str] = None
    username: Optional[str] = None
    like_count: Optional[int] = None
    reply_count: Optional[int] = None
    repost_count: Optional[int] = None
    quote_count: Optional[int] = None
    spoiler: bool = False
    text_spoiler: bool = False
    text_html: Optional[str] = None


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = APIRouter()
scraper = ThreadsCloakScraper()
gluetun = GluetunController()
storage = MediaStorage(
    base_path=os.getenv("CACHE_PATH", "./cache"),
    max_size_gb=float(os.getenv("CACHE_MAX_SIZE_GB", "10.0")),
)


def _media_url_to_path(url: str | None):
    if not url:
        return None
    url = str(url)
    if not url.startswith("/media/"):
        return None
    path_part = url.split("?", 1)[0][len("/media/"):]
    parts = path_part.split("/", 2)
    if len(parts) < 3:
        return None
    platform, post_id, filename = parts[0], parts[1], parts[2]
    if platform != "threads" or not post_id or not filename:
        return None
    return storage.base_path / post_id / filename


def _cached_media_ready(metadata: dict) -> bool:
    if not isinstance(metadata, dict):
        return False

    urls: list[str] = []
    for key in ("thumbnail_url", "video_url"):
        url = metadata.get(key)
        if url:
            urls.append(url)

    carousel = metadata.get("carousel") or []
    if isinstance(carousel, list):
        for item in carousel:
            if not isinstance(item, dict):
                continue
            for key in ("thumbnail_url", "video_url"):
                url = item.get(key)
                if url:
                    urls.append(url)

    for url in urls:
        path = _media_url_to_path(url)
        if not path or not path.exists():
            return False

    return True


def extract_post_id(url: str) -> str:
    """Extract the Threads post shortcode from a URL.

    Supports both /t/ and /post/ paths:
      https://www.threads.com/t/ABC123
      https://www.threads.com/@user/post/ABC123
    """
    match = re.search(r"/(?:t|post)/([^/?#&]+)", str(url))
    if not match:
        raise HTTPException(status_code=400, detail="Invalid Threads URL format.")
    return match.group(1)


async def _download_media(post_id: str, media_list: list[dict]) -> list[MediaItem]:
    """Download all media for a post and return MediaItem descriptors."""
    storage.prepare_post_dir(post_id)
    tasks = []
    mapping: list[tuple[int, str]] = []

    for idx, item in enumerate(media_list):
        media_url = item.get("url")
        if not media_url:
            continue
        ext = "mp4" if item.get("type") == "video" else "jpg"
        dest = storage.base_path / post_id / f"media_{idx}.{ext}"
        tasks.append(storage.download(media_url, dest))
        mapping.append((idx, ext))

    results = await asyncio.gather(*tasks, return_exceptions=True)
    for r in results:
        if isinstance(r, Exception):
            logger.error(f"Download error: {r}")

    items = []
    for idx, ext in mapping:
        path = storage.base_path / post_id / f"media_{idx}.{ext}"
        items.append(
            MediaItem(
                index=idx,
                media_type="video" if ext == "mp4" else "image",
                thumbnail_url=f"/media/threads/{post_id}/media_{idx}.jpg" if ext == "jpg" else None,
                video_url=f"/media/threads/{post_id}/media_{idx}.mp4" if ext == "mp4" else None,
            )
        )
    return items


async def _scrape_post(code: str) -> ThreadsScrapeResponse:
    """Scrape a single Threads post by its URL shortcode."""
    parsed = await scraper.scrape_post(code)

    media_items = await _download_media(code, parsed.get("media") or [])

    response = ThreadsScrapeResponse(
        shortcode=code,
        caption=parsed.get("text") or "",
        author=parsed.get("username") or "",
        media_type=("video" if any(m.media_type == "video" for m in media_items) else ("image" if media_items else "text")),
        thumbnail_url=media_items[0].thumbnail_url if media_items else "",
        video_url=media_items[0].video_url if media_items else None,
        carousel=media_items if len(media_items) > 1 else None,
        link=parsed.get("link"),
        username=parsed.get("username"),
        like_count=parsed.get("like_count"),
        reply_count=parsed.get("reply_count"),
        repost_count=parsed.get("repost_count"),
        quote_count=parsed.get("quote_count"),
        spoiler=bool(parsed.get("spoiler")),
        text_spoiler=bool(parsed.get("text_spoiler")),
        text_html=parsed.get("text_html"),
    )
    storage.save_metadata(code, response.model_dump())
    return response


async def _process_scrape_request(request: ScrapeRequest):
    code = extract_post_id(str(request.url))

    if storage.is_cached(code):
        cached = storage.load_metadata(code)
        if cached and _cached_media_ready(cached):
            logger.info("Cache hit for %s", code)
            return ThreadsScrapeResponse(**cached)
        logger.info("Cache invalid for %s, missing media; re-scraping", code)

    logger.info("Scraping Threads post: %s", code)
    last_error = None

    for attempt in range(1, 4):
        try:
            return await _scrape_post(code)
        except NotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except RateLimitError as e:
            last_error = e
            logger.warning(f"Attempt {attempt} rate-limited: {e}")
            if attempt < 3:
                try:
                    await gluetun.rotate_ip()
                except VpnRotationError as ve:
                    logger.warning(f"VPN rotation failed: {ve}")
                await asyncio.sleep(15)
        except VpnRotationError as e:
            last_error = e
            logger.warning(f"Attempt {attempt} VPN rotation failed: {e}")
            if attempt < 3:
                await asyncio.sleep(30)
        except Exception as e:
            last_error = e
            logger.error(f"Attempt {attempt} failed: {e}")
            if attempt < 3:
                await asyncio.sleep(15)

    raise HTTPException(
        status_code=503 if isinstance(last_error, RateLimitError) else 500,
        detail=str(last_error),
    )


@router.post("/scrape", response_model=ThreadsScrapeResponse)
async def process_scrape_request(request: ScrapeRequest):
    code = extract_post_id(str(request.url))
    return await storage.singleflight(code, lambda: _process_scrape_request(request))


@router.get("/media/{platform}/{post_id}/{filename:path}")
async def serve_media(platform: str, post_id: str, filename: str):
    if platform != "threads":
        raise HTTPException(status_code=404, detail="Invalid platform")
    if ".." in filename or filename.startswith("/"):
        raise HTTPException(status_code=404, detail="Invalid path")

    file_path = storage.base_path / post_id / filename
    resolved = file_path.resolve()
    base_resolved = storage.base_path.resolve()
    if not str(resolved).startswith(str(base_resolved)):
        raise HTTPException(status_code=404, detail="Invalid path")

    if not resolved.exists() or not resolved.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    return FileResponse(resolved)


@router.get("/health")
async def health_check():
    try:
        status = await gluetun.get_vpn_status()
        vpn_status = status.get("status", "").lower()
        if gluetun.enabled and vpn_status != "running":
            raise HTTPException(status_code=503, detail=f"VPN not running: {vpn_status}")
        return {"status": "healthy", "service": "threads", "vpn": status}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"VPN check failed: {e}")


# Register with the global plugin registry on import.
registry.register(
    ScraperPlugin(
        name="threads",
        router=router,
        route_patterns=["threads.com"],
    )
)

# Standalone FastAPI app for container mode
app = FastAPI(title="Pinchana Threads", version="0.1.0")
app.include_router(router)


@app.on_event("shutdown")
async def close_storage_client():
    await storage.close()
