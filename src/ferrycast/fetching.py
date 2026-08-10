"""Shared HTTP fetching with polite retry behaviour."""

from __future__ import annotations

import time
from dataclasses import dataclass

import httpx


@dataclass
class FetchResult:
    ok: bool
    content: bytes | None = None
    text: str | None = None
    content_type: str = ""
    status_code: int | None = None
    error: str | None = None


def fetch(
    url: str,
    *,
    user_agent: str,
    timeout: float = 20.0,
    max_retries: int = 2,
    client: httpx.Client | None = None,
    backoff_seconds: float = 2.0,
    sleep=time.sleep,
) -> FetchResult:
    """GET `url`, retrying transient failures.

    Never raises: a failed fetch is data (a logged gap), not a crash. R1 requires the
    pipeline to survive an unreachable camera.
    """
    headers = {"User-Agent": user_agent, "Accept": "*/*"}
    owned = client is None
    client = client or httpx.Client(timeout=timeout, follow_redirects=True)
    last_error = "unknown error"
    try:
        for attempt in range(max_retries + 1):
            try:
                response = client.get(url, headers=headers)
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            else:
                if response.status_code == 200:
                    return FetchResult(
                        ok=True,
                        content=response.content,
                        text=response.text if _is_textual(response) else None,
                        content_type=response.headers.get("content-type", ""),
                        status_code=response.status_code,
                    )
                last_error = f"HTTP {response.status_code}"
                # 4xx other than 408/429 will not fix themselves; stop early.
                if 400 <= response.status_code < 500 and response.status_code not in (408, 429):
                    return FetchResult(
                        ok=False, status_code=response.status_code, error=last_error
                    )
            if attempt < max_retries:
                sleep(backoff_seconds * (2**attempt))
        return FetchResult(ok=False, error=last_error)
    finally:
        if owned:
            client.close()


def _is_textual(response: httpx.Response) -> bool:
    ctype = response.headers.get("content-type", "")
    return ctype.startswith("text/") or "json" in ctype or "xml" in ctype
