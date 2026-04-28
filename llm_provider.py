# llm_provider.py
"""
LLM provider abstraction so matcher.py doesn't need to know whether
it's talking to Ollama (local), OpenAI (API), or Gemini (API).

Switch providers by setting LLM_PROVIDER in your .env file:
    LLM_PROVIDER=ollama   -> uses local Ollama (llama3.1)
    LLM_PROVIDER=openai   -> uses OpenAI API (requires OPENAI_API_KEY)
    LLM_PROVIDER=gemini   -> uses Google Gemini API (requires GEMINI_API_KEY)
"""

import os
import requests
from abc import ABC, abstractmethod
from typing import Optional


# Fallback when LLM_PROVIDER is not set in the environment.
DEFAULT_PROVIDER = "ollama"


class LLMProvider(ABC):
    @abstractmethod
    def generate(self, prompt: str, json_mode: bool = False, temperature: float = 0.2) -> str:
        ...


class OllamaProvider(LLMProvider):
    def __init__(self, model: str, url: str):
        self.model = model
        self.url = url

    def generate(self, prompt: str, json_mode: bool = False, temperature: float = 0.2) -> str:
        payload = {
            "model": self.model, 
            "prompt": prompt, 
            "stream": False,
            "options": {"temperature": temperature}
        }
        if json_mode:
            payload["format"] = "json"
        r = requests.post(self.url, json=payload, timeout=90)
        data = r.json()
        if "error" in data:
            raise RuntimeError(f"Ollama error: {data['error']}")
        return data.get("response", "").strip()


class OpenAIProvider(LLMProvider):
    def __init__(self, api_key: str, model: str):
        # Lazy import so users without the openai package installed
        # are unaffected when they're only using Ollama.
        from openai import OpenAI
        self.client = OpenAI(api_key=api_key)
        self.model = model

    def generate(self, prompt: str, json_mode: bool = False, temperature: float = 0.2) -> str:
        kwargs = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
        }
        if json_mode:
            # OpenAI requires the word "json" to appear somewhere in the prompt
            # when response_format is json_object.
            kwargs["response_format"] = {"type": "json_object"}
        r = self.client.chat.completions.create(**kwargs)
        return r.choices[0].message.content.strip()


class GeminiProvider(LLMProvider):
    def __init__(self, api_key: str, model: str):
        # Lazy import: uses Google's NEW unified SDK (google-genai), which
        # supports the current Gemini 2.x and 3.x model families.
        # Install with:  pip install google-genai
        from google import genai
        from google.genai import types
        self._client = genai.Client(api_key=api_key)
        self._types = types
        self.model = model

    def generate(self, prompt: str, json_mode: bool = False, temperature: float = 0.2) -> str:
        kwargs = {"temperature": temperature}
        if json_mode:
            kwargs["response_mime_type"] = "application/json"
            
        config = self._types.GenerateContentConfig(**kwargs)

        response = self._client.models.generate_content(
            model=self.model,
            contents=prompt,
            config=config,
        )
        return (response.text or "").strip()


_provider: Optional[LLMProvider] = None


def get_llm_provider() -> LLMProvider:
    """Returns a cached provider instance based on environment variables."""
    global _provider
    if _provider is not None:
        return _provider

    name = os.environ.get("LLM_PROVIDER", DEFAULT_PROVIDER).lower()

    if name == "ollama":
        _provider = OllamaProvider(
            model=os.environ.get("LLM_MODEL", "llama3.1"),
            url=os.environ.get("OLLAMA_URL", "http://localhost:11434/api/generate"),
        )
    elif name == "openai":
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError(
                "OPENAI_API_KEY must be set in your .env file when LLM_PROVIDER=openai"
            )
        _provider = OpenAIProvider(
            api_key=api_key,
            model=os.environ.get("LLM_MODEL", "gpt-4.1-mini"),
        )
    elif name == "gemini":
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError(
                "GEMINI_API_KEY must be set in your .env file when LLM_PROVIDER=gemini"
            )
        _provider = GeminiProvider(
            api_key=api_key,
            model=os.environ.get("LLM_MODEL", "gemini-2.5-flash"),
        )
    else:
        raise ValueError(
            f"Unknown LLM_PROVIDER: {name!r} (expected 'ollama', 'openai', or 'gemini')"
        )

    return _provider
