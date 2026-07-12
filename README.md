# 🧵 Pinchana Threads Scraper

**Pinchana Threads Scraper** extracts public post data and media from Meta's Threads platform with a fast HTTP-first strategy and a pooled stealth-browser fallback for gated responses.

---

## ✨ Key Features

- **🚀 Browserless Fast Path:** Fetches the server-rendered post page with a shared `curl-cffi` connection pool and extracts its `data-sjs` JSON payload.
- **🛡 Gating Fallback:** Reuses one lazily started CloakBrowser process when Threads returns a login, challenge, or incomplete payload.
- **🔄 Smart VPN Rotation:** Automatically detects rate limits (403/429) and signals the VPN (Gluetun) to rotate IPs.
- **💾 Local Caching:** Saves downloaded media and metadata to a persistent LRU cache.
- **🌐 Standalone Service:** Operates as a FastAPI service that can be easily integrated into the Pinchana Gateway.

---

## 🏗 Architecture

The scraper follows an "HTTP → Fallback → Download → Cache" workflow:
1. **HTTP extraction:** Fetches the canonical post page and searches server-rendered JSON for the requested shortcode.
2. **Browser fallback:** For gated or incomplete pages, opens an isolated page in a shared CloakBrowser process and inspects GraphQL/DOM JSON.
3. **Download:** Downloads media through the secure VPN tunnel.
4. **Storage:** Stores files and metadata under the configured cache path.

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
| `THREADS_HTTP_CONCURRENCY` | `20` | Maximum concurrent requests in the shared HTTP session. |
| `THREADS_BROWSER_CONCURRENCY` | `2` | Maximum simultaneous pages in the fallback browser. |

---

## 🛠 Development

Managed by `uv`.

```bash
uv sync
uv run uvicorn pinchana_threads.main:app --host 0.0.0.0 --port 8088
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
        "http://localhost:8088/scrape",
        json={"url": "https://www.threads.net/t/..."}
    ) as resp:
        data = await resp.json()
        # data["thumbnail_url"] → /media/threads/...
        # data["video_url"]   → /media/threads/... (if video)
        # data["carousel"]    → list of MediaItem
        # data["link"]        → link preview URL
```
