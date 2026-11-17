"""Model providers. Each one keeps its own per-chat history and raises
ProviderUnavailable when it can't answer (no quota, bad key, offline), so the
router can move on to the next provider."""
from __future__ import annotations

import json
import logging
import os
from collections import defaultdict
from typing import Any

import anthropic

from .config import CONFIG
from .prompt import build_system_prompt
from .tools import OPENAI_TOOL_SCHEMAS, TOOL_SCHEMAS, ToolContext, run_tool
from . import tools_more  # noqa: F401  (registers the extra tools)

log = logging.getLogger("zolt.providers")

MAX_TOOL_ROUNDS = 25
MAX_HISTORY_MESSAGES = 60  # start a fresh conversation after this many messages
STEP_LIMIT_REPLY = "⚠️ কাজটা অনেক ধাপ নিচ্ছে, তাই থামলাম। ছোট ছোট করে আবার বলুন।"


class ProviderUnavailable(Exception):
    pass


class Provider:
    name: str = "?"
    label: str = "?"

    def __init__(self) -> None:
        self.histories: dict[int, list[dict[str, Any]]] = defaultdict(list)

    def available(self) -> bool:
        raise NotImplementedError

    def reset(self, chat_id: int) -> None:
        self.histories.pop(chat_id, None)

    def _history(self, chat_id: int) -> list[dict[str, Any]]:
        history = self.histories[chat_id]
        if len(history) > MAX_HISTORY_MESSAGES:
            history.clear()
        return history

    async def handle(self, chat_id: int, content: list[dict[str, Any]], ctx: ToolContext) -> str:
        raise NotImplementedError


# ---------------------------------------------------------------- Claude (official SDK)

class ClaudeProvider(Provider):
    name, label = "claude", "Claude"

    def __init__(self) -> None:
        super().__init__()
        self._client: anthropic.AsyncAnthropic | None = None

    def available(self) -> bool:
        return bool(os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN"))

    @property
    def client(self) -> anthropic.AsyncAnthropic:
        if self._client is None:
            self._client = anthropic.AsyncAnthropic()
        return self._client

    async def handle(self, chat_id, content, ctx):
        history = self._history(chat_id)
        history.append({"role": "user", "content": content})
        for _ in range(MAX_TOOL_ROUNDS):
            try:
                response = await self.client.beta.messages.create(
                    model=CONFIG.model,
                    max_tokens=16000,
                    system=build_system_prompt(),
                    tools=TOOL_SCHEMAS,
                    messages=history,
                    output_config={"effort": CONFIG.effort},
                    cache_control={"type": "ephemeral"},
                    betas=["server-side-fallback-2026-07-01"],
                    fallbacks="default",
                )
            except (anthropic.AuthenticationError, anthropic.PermissionDeniedError,
                    anthropic.RateLimitError, anthropic.APIConnectionError,
                    anthropic.InternalServerError) as e:
                raise ProviderUnavailable(f"{type(e).__name__}: {e}") from e
            except anthropic.APIStatusError as e:
                # e.g. out of credit (400 billing) - let another model take over
                raise ProviderUnavailable(f"{e.status_code}: {e.message}") from e

            history.append({"role": "assistant", "content": response.content})
            if response.stop_reason == "pause_turn":
                continue
            if response.stop_reason == "refusal":
                self.reset(chat_id)
                return "🙏 দুঃখিত ম্যাম, এই কাজটা আমি করতে পারব না।"
            if response.stop_reason != "tool_use":
                text = "\n".join(b.text for b in response.content if b.type == "text").strip()
                if response.stop_reason == "max_tokens":
                    text += "\n\n(উত্তর অনেক লম্বা হয়ে কেটে গেছে।)"
                return text or "✅ কাজ শেষ।"

            results = []
            for block in response.content:
                if block.type == "tool_use":
                    output, is_error = await run_tool(block.name, block.input, ctx)
                    results.append({"type": "tool_result", "tool_use_id": block.id,
                                    "content": output[:20000], "is_error": is_error})
            history.append({"role": "user", "content": results})

        self.reset(chat_id)
        return STEP_LIMIT_REPLY


# ---------------------------------------------------------------- Everyone else (LiteLLM)

def _to_openai_content(content: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for block in content:
        if block["type"] == "text":
            out.append({"type": "text", "text": block["text"]})
        elif block["type"] == "image":
            src = block["source"]
            out.append({"type": "image_url",
                        "image_url": {"url": f"data:{src['media_type']};base64,{src['data']}"}})
    return out


class LiteLLMProvider(Provider):
    """Gemini, Groq, OpenRouter, Mistral, or any OpenAI-compatible service."""

    def __init__(self, name: str, label: str, key_env: str, model_env: str,
                 default_model: str = "", base_env: str | None = None,
                 model_prefix: str = "") -> None:
        super().__init__()
        self.name, self.label = name, label
        self.key_env, self.model_env, self.base_env = key_env, model_env, base_env
        self.default_model, self.model_prefix = default_model, model_prefix

    @property
    def models(self) -> list[str]:
        """MODEL env may list several, comma-separated; they are tried in order."""
        raw = os.getenv(self.model_env, "").strip() or self.default_model
        out = []
        for m in (x.strip() for x in raw.split(",")):
            if m and self.model_prefix and not m.startswith(self.model_prefix):
                m = self.model_prefix + m
            if m:
                out.append(m)
        return out

    @property
    def model(self) -> str:
        return self.models[0] if self.models else ""

    def available(self) -> bool:
        if not (os.getenv(self.key_env, "").strip() and self.model):
            return False
        return not self.base_env or bool(os.getenv(self.base_env, "").strip())

    async def handle(self, chat_id, content, ctx):
        import litellm  # imported lazily: it is slow to load

        litellm.suppress_debug_info = True

        history = self._history(chat_id)
        history.append({"role": "user", "content": _to_openai_content(content)})
        kwargs: dict[str, Any] = {"api_key": os.getenv(self.key_env, "").strip()}
        if self.base_env:
            kwargs["api_base"] = os.getenv(self.base_env, "").strip()

        for _ in range(MAX_TOOL_ROUNDS):
            response, errors = None, []
            for model in self.models:  # busy/retired model -> try the next one
                try:
                    response = await litellm.acompletion(
                        model=model,
                        messages=[{"role": "system", "content": build_system_prompt()}, *history],
                        tools=OPENAI_TOOL_SCHEMAS,
                        timeout=45,
                        num_retries=0,
                        **kwargs,
                    )
                    break
                except Exception as e:  # quota, bad key, overloaded, offline...
                    log.warning("%s failed: %s: %s", model, type(e).__name__, str(e)[:200])
                    errors.append(f"{model}: {type(e).__name__}")
            if response is None:
                raise ProviderUnavailable("; ".join(errors))

            message = response.choices[0].message
            # Keep the provider's own fields (e.g. Gemini thought signatures) for the next call.
            history.append(message.model_dump(exclude_none=True))
            tool_calls = message.tool_calls or []
            if not tool_calls:
                return (message.content or "").strip() or "✅ কাজ শেষ।"

            for call in tool_calls:
                try:
                    args = json.loads(call.function.arguments or "{}")
                    output, is_error = await run_tool(call.function.name, args, ctx)
                except json.JSONDecodeError:
                    output, is_error = "Invalid JSON arguments; call the tool again.", True
                history.append({"role": "tool", "tool_call_id": call.id,
                                "content": ("ERROR: " if is_error else "") + output[:20000]})

        self.reset(chat_id)
        return STEP_LIMIT_REPLY


def build_providers() -> dict[str, Provider]:
    providers: list[Provider] = [
        ClaudeProvider(),
        LiteLLMProvider("gemini", "Gemini", "GEMINI_API_KEY", "GEMINI_MODEL",
                        "gemini/gemini-flash-latest", model_prefix="gemini/"),
        LiteLLMProvider("custom", "Custom", "CUSTOM_API_KEY", "CUSTOM_MODEL",
                        base_env="CUSTOM_API_BASE", model_prefix="openai/"),
        LiteLLMProvider("groq", "Groq", "GROQ_API_KEY", "GROQ_MODEL",
                        "groq/openai/gpt-oss-120b", model_prefix="groq/"),
        LiteLLMProvider("openrouter", "OpenRouter", "OPENROUTER_API_KEY", "OPENROUTER_MODEL",
                        model_prefix="openrouter/"),
        LiteLLMProvider("mistral", "Mistral", "MISTRAL_API_KEY", "MISTRAL_MODEL",
                        "mistral/mistral-small-latest", model_prefix="mistral/"),
    ]
    return {p.name: p for p in providers}
