"""Groq client for the analysis layer (summaries, sentiment, Q&A).

Wraps the official `groq` SDK. The default model comes from config; callers
can override per request. `ping()` does a tiny completion to verify the key.
"""
from __future__ import annotations

from groq import Groq

from financial_analysis_agent.utils import config


class GroqClient:
    def __init__(self, api_key: str | None = None, model: str | None = None):
        self.api_key = api_key or config.require("GROQ_API_KEY")
        self.model = model or config.GROQ_MODEL
        self.client = Groq(api_key=self.api_key)

    def chat(
        self,
        prompt: str,
        *,
        system: str | None = None,
        model: str | None = None,
        temperature: float = 0.2,
        max_tokens: int = 1024,
        json_mode: bool = False,
    ) -> str:
        """Single-turn completion; returns the assistant text.

        json_mode=True forces syntactically valid JSON (response_format json_object),
        so the model can't emit unescaped quotes or literal control characters that
        would break parsing. Requires the word "json" in the prompt/system (caller's
        responsibility -- the API rejects the request otherwise).
        """
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        kwargs = {"response_format": {"type": "json_object"}} if json_mode else {}
        resp = self.client.chat.completions.create(
            model=model or self.model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            **kwargs,
        )
        return resp.choices[0].message.content or ""

    def ping(self) -> tuple[bool, str]:
        """Connectivity check: minimal completion. Returns (ok, detail)."""
        try:
            out = self.chat(
                "Reply with the single word: pong",
                max_tokens=5,
                temperature=0.0,
            )
            return True, f"model={self.model} -> {out.strip()[:40]!r}"
        except Exception as e:  # noqa: BLE001
            return False, f"{type(e).__name__}: {e}"
