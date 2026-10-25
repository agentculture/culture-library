"""Judge configuration, the locality gate, and a stdlib OpenAI-compatible chat client."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Iterable

from culture_library.gateway import open_no_redirect

from .errors import EvalApiError, EvalPolicyError

DEFAULT_JUDGE_MODEL = "cortex"
DEFAULT_GATEWAY_URL = "http://127.0.0.1:8001"
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "host.docker.internal"})


def is_local(url: str) -> bool:
    """True only for loopback / host.docker.internal URLs."""
    try:
        host = urllib.parse.urlsplit(url if "//" in url else "//" + url).hostname
    except ValueError:
        return False
    return (host or "").lower() in LOCAL_HOSTS


@dataclass(frozen=True)
class JudgeConfig:
    url: str
    model: str = DEFAULT_JUDGE_MODEL
    api_key: str = field(default="", repr=False)

    @classmethod
    def from_env(cls, env: dict | None = None) -> JudgeConfig:
        e = os.environ if env is None else env
        url = e.get("CULTURE_LIBRARY_EVAL_LLM_URL") or e.get("LOBES_GATEWAY_URL") or ""
        url = url or DEFAULT_GATEWAY_URL
        # The lobes gateway key only goes to the gateway (or a local judge); any other judge
        # gets CULTURE_LIBRARY_EVAL_LLM_KEY or nothing.
        to_gateway = is_local(url) or url == e.get("LOBES_GATEWAY_URL")
        key = e.get("CULTURE_LIBRARY_EVAL_LLM_KEY") or (
            e.get("GATEWAY_API_KEY", "") if to_gateway else ""
        )
        return cls(
            url=url,
            model=e.get("CULTURE_LIBRARY_EVAL_LLM_MODEL") or DEFAULT_JUDGE_MODEL,
            api_key=key,
        )

    @property
    def is_local(self) -> bool:
        return is_local(self.url)


def guard_text_egress(items: Iterable, judge: JudgeConfig) -> None:
    """Raise EvalPolicyError if a non-local judge would see any non-public item.

    ``items`` are dicts or objects with a ``visibility`` attribute/key (item metadata only;
    call this before any item text is fetched). A missing visibility counts as non-public.
    """
    if judge.is_local:
        return
    for it in items:
        vis = it.get("visibility") if isinstance(it, dict) else getattr(it, "visibility", None)
        if vis != "public":
            ident = it.get("id") if isinstance(it, dict) else getattr(it, "id", "?")
            raise EvalPolicyError(
                f"item {ident} is not public; refusing to send its text to non-local judge "
                f"{judge.url!r} (use a local judge URL or make the item public)"
            )


class OpenAIChat:
    """Stdlib ``ChatLLM`` hitting ``{url}/v1/chat/completions``."""

    def __init__(self, judge: JudgeConfig, timeout: float = 120, temperature: float = 0.0):
        self.judge = judge
        self.timeout = timeout
        self.temperature = temperature

    def __repr__(self) -> str:
        return f"OpenAIChat(url={self.judge.url!r}, model={self.judge.model!r})"

    def complete(self, messages: list[dict]) -> str:
        body = json.dumps(
            {"model": self.judge.model, "messages": messages, "temperature": self.temperature}
        ).encode()
        headers = {"Content-Type": "application/json"}
        if self.judge.api_key:
            headers["Authorization"] = f"Bearer {self.judge.api_key}"
        req = urllib.request.Request(  # noqa: S310 - operator-configured judge URL
            self.judge.url.rstrip("/") + "/v1/chat/completions",
            data=body,
            headers=headers,
            method="POST",
        )
        try:
            with open_no_redirect(req, timeout=self.timeout) as resp:
                payload = json.loads(resp.read())
            return payload["choices"][0]["message"]["content"] or ""
        except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
            raise EvalApiError(f"judge call failed: {exc}") from exc
