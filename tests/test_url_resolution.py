import httpx
import pytest

from pinchana_threads.main import extract_post_id, resolve_post_id


def test_extract_post_id_accepts_canonical_threads_urls():
    assert extract_post_id("https://www.threads.com/t/Db5DAcNiDZo") == "Db5DAcNiDZo"
    assert (
        extract_post_id("https://www.threads.com/@_phoenix_1996/post/Db5DAcNiDZo")
        == "Db5DAcNiDZo"
    )


@pytest.mark.asyncio
async def test_resolve_post_id_follows_threads_share_redirect():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/share/_lPHF4BP-":
            if request.method == "HEAD":
                return httpx.Response(200)
            return httpx.Response(
                302,
                headers={
                    "Location": (
                        "https://www.threads.com/@_phoenix_1996/post/Db5DAcNiDZo"
                        "?xmt=tracking"
                    )
                },
            )
        return httpx.Response(200)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
    ) as client:
        post_id = await resolve_post_id(
            "https://www.threads.com/share/_lPHF4BP-",
            client=client,
        )

    assert post_id == "Db5DAcNiDZo"
