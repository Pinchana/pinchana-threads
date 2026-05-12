# 🧵 Pinchana Threads Scraper

**Pinchana Threads Scraper** is a high-performance module for extracting posts, profiles, and engagement data from Meta's Threads platform using a lightweight GraphQL-only strategy — no browser required.

---

## ✨ Key Features

- **🚀 GraphQL-Only Scraping:** Direct queries to Threads' internal Barcelona GraphQL API using `curl-cffi` with JA3/TLS fingerprint impersonation.
- **🔐 Cookie + Session Reuse:** Harvests session cookies from an initial homepage bootstrap, then reuses them for sustained scraping without full browser automation.
- **🔄 Smart VPN Rotation:** Automatically detects rate limits (403/429) and signals the VPN (Gluetun) to rotate IPs.
- **💾 Local Caching:** Saves downloaded media and metadata to a persistent LRU cache.
- **🌐 Standalone Service:** Operates as a FastAPI service that can be easily integrated into the Pinchana Gateway.

---

## 🏗 Architecture

The scraper follows an "Bootstrap → Extract → Download → Cache" workflow:
1. **Bootstrap:** Hits `threads.net` homepage to harvest `csrftoken` and session cookies.
2. **Extraction:** Uses Threads GraphQL `doc_id`-based queries with robust retry and anti-block handling.
3. **Download:** Media is downloaded through the secure VPN tunnel.
4. **Storage:** Files are organized under `/app/cache/threads/{post_id}`.

---

## 📡 API Reference

### `POST /scrape`
Extracts and downloads media for a given Threads URL (post or profile).
```json
{
  "url": "https://www.threads.net/t/CuX_UYABrr7"
}
```

### `GET /health`
Checks service health, VPN connectivity, and scraper status.

---

## ⚙️ Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `CACHE_PATH` | `./cache` | Base path for media storage. |
| `CACHE_MAX_SIZE_GB` | `10.0` | Max size for the LRU cache. |
| `GLUETUN_CONTROL_URL` | `http://localhost:8000` | URL for the Gluetun control API. |

---

## 🛠 Development

Managed by `uv`.

```bash
uv sync
uv run uvicorn src.pinchana_threads.main:app --host 0.0.0.0 --port 8083
```

---

## 📜 License

MIT

---

## 🤖 Bot Integration

The bot (`pinchana-bot`) previously consumed an external API with this shape:
```json
{
  "thread": {
    "url": "...", "text": "...", "username": "...",
    "like_count": 0, "reply_count": 0,
    "link": "...",
    "videos": ["..."], "images": ["..."]
  }
}
```

Our Pinchana module now returns a **unified `/media/` format** via `ThreadsScrapeResponse`:
```json
{
  "shortcode": "CuX_UYABrr7",
  "caption": "Hello world",
  "author": "username",
  "media_type": "image",
  "thumbnail_url": "/media/threads/CuX_UYABrr7/media_0.jpg",
  "video_url": null,
  "carousel": null,
  "link": "https://example.com",
  "username": "username",
  "like_count": 42,
  "reply_count": 5,
  "repost_count": 2,
  "quote_count": 1
}
```

### Field coverage vs. bot requirements

| Bot field | Our field | Status |
|-----------|-----------|--------|
| `thread.text` | `caption` | ✅ |
| `thread.username` | `author` / `username` | ✅ |
| `thread.like_count` | `like_count` | ✅ |
| `thread.reply_count` | `reply_count` | ✅ |
| `thread.link` | `link` | ✅ |
| `thread.url` | construct from `shortcode` | ✅ |
| `thread.videos` / `thread.images` | unified `carousel` + `thumbnail_url` / `video_url` | ✅ `/media/` standard |
| `thread.user_pic` | — | ❌ intentionally omitted |

To consume from the bot, replace the external API call with:
```python
async with aiohttp.ClientSession() as session:
    async with session.post(
        "http://localhost:8083/scrape",
        json={"url": "https://www.threads.net/t/..."}
    ) as resp:
        data = await resp.json()
        # data["thumbnail_url"] → /media/threads/...
        # data["video_url"]   → /media/threads/... (if video)
        # data["carousel"]    → list of MediaItem
        # data["link"]        → link preview URL
```
