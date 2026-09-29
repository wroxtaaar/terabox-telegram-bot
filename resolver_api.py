from __future__ import annotations

import hmac
import os
import traceback

from fastapi import FastAPI, Header, HTTPException, Query

from app.terabox import resolve_terabox_url

app = FastAPI(title="TeraBox Resolver")

RESOLVER_SECRET = os.getenv("RESOLVER_SECRET", "").strip()


def _authorized(value: str | None) -> bool:
    if not RESOLVER_SECRET:
        return True
    return bool(value) and hmac.compare_digest(value, RESOLVER_SECRET)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/resolve")
async def resolve(
    url: str = Query(..., min_length=8),
    x_resolver_secret: str | None = Header(default=None),
):
    if not _authorized(x_resolver_secret):
        raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        result = await resolve_terabox_url(url)
        return {
            "success": True,
            "surl": result.get("surl"),
            "file_name": result.get("file_name"),
            "size": result.get("size", 0),
            "fs_id": result.get("fs_id"),
            "direct_url": result.get("direct_url"),
            "files": result.get("files", []),
            "thumbnail": result.get("thumbnail", ""),
        }
    except Exception as exc:
        # Keep the public response useful for diagnosis without exposing
        # cookies, headers, or browser state.
        return {
            "success": False,
            "error": str(exc),
            "error_type": type(exc).__name__,
            "error_repr": repr(exc),
            "trace_tail": traceback.format_exc().splitlines()[-6:],
        }
