"""Authenticated, bounded, streaming proxy for a shared native model server."""
import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import secrets

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

ALLOWED = {
    ("GET", "models"),
    ("POST", "chat/completions"),
    ("POST", "completions"),
    ("POST", "responses"),
    ("POST", "embeddings"),
}
BODY_LIMIT = 8 * 1024 * 1024


def create_app(config=None, transport=None):
    @asynccontextmanager
    async def lifespan(app):
        settings = config or json.loads(
            Path(os.environ["GATEWAY_CONFIG"]).read_text()
        )
        app.state.settings = settings
        app.state.active = {}
        app.state.total = 0
        app.state.lock = asyncio.Lock()
        app.state.client = httpx.AsyncClient(
            base_url=settings["upstream"].rstrip("/") + "/",
            headers={"Authorization": "Bearer " + settings["upstream_key"]},
            # Long-context prefill on a shared Mac can take many minutes.
            timeout=httpx.Timeout(3600.0, connect=10.0),
            transport=transport,
            trust_env=False,
        )
        try:
            yield
        finally:
            await app.state.client.aclose()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz")
    async def health():
        return {"status": "ok"}

    @app.get("/readyz")
    async def ready():
        try:
            r = await app.state.client.get("health")
            r.raise_for_status()
        except httpx.HTTPError:
            return JSONResponse({"status": "model_unavailable"}, status_code=503)
        return {"status": "ready"}

    @app.api_route("/v1/{endpoint:path}", methods=["GET", "POST"])
    async def proxy(endpoint: str, request: Request):
        supplied = request.headers.get("authorization", "")
        if not supplied.startswith("Bearer "):
            raise HTTPException(401, "A developer API key is required")
        token = supplied[7:]
        user = next(
            (name for name, key in app.state.settings["users"].items()
             if secrets.compare_digest(token.encode(), key.encode())),
            None,
        )
        if user is None:
            raise HTTPException(401, "Invalid developer API key")
        if (request.method, endpoint) not in ALLOWED:
            raise HTTPException(404, "Endpoint is not enabled")
        # Query strings are not part of the upstream path and are passed separately.
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > BODY_LIMIT:
                raise HTTPException(413, "Request body exceeds 8 MiB")
            chunks.append(chunk)
        if request.method == "POST":
            try:
                payload = json.loads(b"".join(chunks))
            except (ValueError, UnicodeError):
                raise HTTPException(400, "Request must contain JSON") from None
            if not isinstance(payload, dict):
                raise HTTPException(400, "Request must be a JSON object")

        async with app.state.lock:
            settings = app.state.settings
            if (app.state.active.get(user, 0) >= settings.get("per_user_concurrency", 2)
                    or app.state.total >= settings.get("global_concurrency", 4)):
                raise HTTPException(429, "Concurrent request limit reached",
                                    headers={"Retry-After": "2"})
            app.state.active[user] = app.state.active.get(user, 0) + 1
            app.state.total += 1

        async def release():
            async with app.state.lock:
                app.state.active[user] -= 1
                app.state.total -= 1

        upstream = None
        try:
            outgoing = app.state.client.build_request(
                request.method, "v1/" + endpoint,
                params=request.query_params,
                content=b"".join(chunks),
                headers={"Content-Type": "application/json", "Accept-Encoding": "identity"},
            )
            upstream = await app.state.client.send(outgoing, stream=True)
        except httpx.TimeoutException:
            await release()
            raise HTTPException(504, "Model server timed out") from None
        except httpx.HTTPError:
            await release()
            raise HTTPException(502, "Model server is unavailable") from None
        except BaseException:
            await release()
            raise

        async def content():
            try:
                async for chunk in upstream.aiter_bytes():
                    yield chunk
            finally:
                # Complete cleanup even when an SSE client disconnects.
                await asyncio.shield(upstream.aclose())
                await asyncio.shield(release())

        headers = {"Cache-Control": "no-store", "X-Accel-Buffering": "no"}
        if "content-type" in upstream.headers:
            headers["Content-Type"] = upstream.headers["content-type"]
        if "retry-after" in upstream.headers:
            headers["Retry-After"] = upstream.headers["retry-after"]
        return StreamingResponse(content(), status_code=upstream.status_code, headers=headers)

    return app


app = create_app()

