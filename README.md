# Pinchana Threads

This FastAPI module extracts public Threads posts from both `threads.net` and `threads.com`. It uses an HTTP-first strategy and a pooled browser fallback when the server-rendered response is gated or incomplete.

## Processing flow

1. Normalize the post URL and resolve its shortcode.
2. Fetch the canonical page through a shared `curl-cffi` session.
3. Parse the server-rendered `data-sjs` payload for the requested post.
4. Use an isolated page in the shared browser process only when the HTTP response is gated or incomplete.
5. Download images, videos, covers, and supported music-preview assets into the shared cache.

The result can contain one image, one video, an ordered carousel, text-only metadata, or a Threads music preview. The gateway v1 contract represents music previews as a 30-second `soundtrack` asset with title and artist metadata plus a separate `cover` asset. Engagement fields are optional and must not be treated as guaranteed.

## API

- `POST /scrape` accepts `{"url":"https://www.threads.com/@account/post/SHORTCODE"}` and equivalent `threads.net` URLs.
- `GET /health` reports service, VPN, HTTP-session, and browser readiness.

External clients, including Pinchana Bot, must use the gateway's authenticated `POST /v1/scrape` route. They must not call this module directly or depend on its legacy flat response.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `CACHE_PATH` | `./cache` | Base media cache path |
| `CACHE_MAX_SIZE_GB` | `10.0` | Maximum cache size before eviction |
| `GLUETUN_CONTROL_URL` | `http://localhost:8000` | Private Gluetun control endpoint |
| `THREADS_HTTP_CONCURRENCY` | `20` | Maximum concurrent HTTP operations |
| `THREADS_BROWSER_CONCURRENCY` | `2` | Maximum simultaneous fallback pages |

## Development and diagnostics

```sh
uv sync --frozen
uv run uvicorn pinchana_threads.main:app --host 0.0.0.0 --port 8088 --reload
```

When the gateway rejects a valid Threads URL, correlate its `request_id` across `scrape_request`, `scrape_route_selected`, `scrape_forward`, and `scrape_rejected`. Confirm that both the gateway route patterns and this module accept the submitted hostname before changing the client.

```sh
# Run from the parent pinchana-api directory.
docker build --file pinchana-threads/Dockerfile --tag pinchana-threads:local .
```
