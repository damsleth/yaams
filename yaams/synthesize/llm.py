"""Pluggable LLM adapter for synthesis (and future query parsing).

The architecture spec says any of ollama / llama-cpp / claude / codex /
copilot must be wireable as a backend. v1 ships:

- OllamaAdapter: HTTP to a local ollama server (default localhost:11434)
- SubprocessAdapter: pipes prompt to stdin of any CLI (codex, claude, ...)
  and reads stdout - lets the user point at whatever they already run
- DummyAdapter: deterministic, used for tests and "no backend configured"

`llm_adapter_from_config(cfg)` reads `synth.backend` from config.yaml.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Protocol


@dataclass
class LLMResponse:
  text: str
  backend: str
  model: str | None = None


class LLMAdapter(Protocol):
  backend_name: str
  model_name: str | None

  def complete(
    self,
    prompt: str,
    *,
    max_tokens: int = 1024,
    temperature: float = 0.0,
  ) -> LLMResponse: ...


class DummyAdapter:
  backend_name = "dummy"

  def __init__(self, model_name: str | None = None):
    self.model_name = model_name

  def complete(
    self,
    prompt: str,
    *,
    max_tokens: int = 1024,
    temperature: float = 0.0,
  ) -> LLMResponse:
    head = prompt.splitlines()[0][:80] if prompt else ""
    return LLMResponse(
      text=f"[dummy adapter] no synthesis configured. prompt[0]: {head}",
      backend=self.backend_name,
      model=self.model_name,
    )


class OllamaAdapter:
  backend_name = "ollama"

  def __init__(
    self,
    model_name: str | None = "llama3.1",
    host: str = "http://localhost:11434",
    timeout: float = 60.0,
  ):
    self.model_name = model_name
    self.host = host.rstrip("/")
    self.timeout = timeout

  def complete(
    self,
    prompt: str,
    *,
    max_tokens: int = 1024,
    temperature: float = 0.0,
  ) -> LLMResponse:
    import httpx

    body = {
      "model": self.model_name,
      "prompt": prompt,
      "stream": False,
      "options": {
        "temperature": temperature,
        "num_predict": max_tokens,
      },
    }
    response = httpx.post(
      f"{self.host}/api/generate",
      json=body,
      timeout=self.timeout,
    )
    response.raise_for_status()
    data = response.json()
    return LLMResponse(
      text=str(data.get("response", "")).strip(),
      backend=self.backend_name,
      model=self.model_name,
    )


class SubprocessAdapter:
  backend_name = "subprocess"

  def __init__(
    self,
    command: list[str],
    model_name: str | None = None,
    timeout: float = 120.0,
    encoding: str = "utf-8",
  ):
    if not command:
      raise ValueError("SubprocessAdapter requires a non-empty command")
    self.command = list(command)
    self.model_name = model_name
    self.timeout = timeout
    self.encoding = encoding

  def complete(
    self,
    prompt: str,
    *,
    max_tokens: int = 1024,
    temperature: float = 0.0,
  ) -> LLMResponse:
    result = subprocess.run(
      self.command,
      input=prompt,
      capture_output=True,
      text=True,
      timeout=self.timeout,
      encoding=self.encoding,
    )
    if result.returncode != 0:
      raise RuntimeError(
        f"LLM subprocess {self.command[0]!r} exited {result.returncode}: "
        f"{result.stderr.strip()}"
      )
    return LLMResponse(
      text=result.stdout.strip(),
      backend=self.backend_name,
      model=self.model_name,
    )


class ClaudeCliAdapter:
  """Drives `claude -p --input-format text` via stdin."""

  backend_name = "claude"

  def __init__(
    self,
    model_name: str | None = None,
    timeout: float = 120.0,
    safe_mode: bool = False,
  ):
    self.model_name = model_name
    self.timeout = timeout
    # safe_mode adds --safe-mode, which disables the user's CLAUDE.md, skills,
    # hooks, output styles, etc. for this invocation (auth is untouched, unlike
    # --bare). Keeps the user's global config from leaking into programmatic
    # completions like the post-ingest summary.
    self.safe_mode = safe_mode

  def complete(
    self,
    prompt: str,
    *,
    max_tokens: int = 1024,
    temperature: float = 0.0,
  ) -> LLMResponse:
    cmd = ["claude", "-p", "--input-format", "text"]
    if self.safe_mode:
      cmd.append("--safe-mode")
    if self.model_name:
      cmd += ["--model", self.model_name]
    result = subprocess.run(
      cmd,
      input=prompt,
      capture_output=True,
      text=True,
      timeout=self.timeout,
    )
    if result.returncode != 0:
      raise RuntimeError(
        f"claude CLI exited {result.returncode}: {result.stderr.strip()}"
      )
    return LLMResponse(
      text=result.stdout.strip(),
      backend=self.backend_name,
      model=self.model_name,
    )


class CodexCliAdapter:
  """Drives `codex exec -` via stdin."""

  backend_name = "codex"

  def __init__(
    self,
    model_name: str | None = None,
    timeout: float = 120.0,
  ):
    self.model_name = model_name
    self.timeout = timeout

  def complete(
    self,
    prompt: str,
    *,
    max_tokens: int = 1024,
    temperature: float = 0.0,
  ) -> LLMResponse:
    cmd = ["codex", "exec", "-"]
    if self.model_name:
      cmd = ["codex", "--model", self.model_name, "exec", "-"]
    result = subprocess.run(
      cmd,
      input=prompt,
      capture_output=True,
      text=True,
      timeout=self.timeout,
    )
    if result.returncode != 0:
      raise RuntimeError(
        f"codex CLI exited {result.returncode}: {result.stderr.strip()}"
      )
    return LLMResponse(
      text=result.stdout.strip(),
      backend=self.backend_name,
      model=self.model_name,
    )


def llm_adapter_from_config(cfg: dict) -> LLMAdapter:
  synth = cfg.get("synth", {}) or {}
  backend = (synth.get("backend") or "dummy").strip().lower()
  model = synth.get("model")
  timeout = float(synth.get("timeout") or 120.0)

  if backend == "claude":
    return ClaudeCliAdapter(
      model_name=str(model) if model else None,
      timeout=timeout,
      safe_mode=bool(synth.get("safe_mode", False)),
    )
  if backend == "codex":
    return CodexCliAdapter(
      model_name=str(model) if model else None,
      timeout=timeout,
    )
  if backend == "ollama":
    return OllamaAdapter(
      model_name=str(model or "llama3.1"),
      host=str(synth.get("host") or "http://localhost:11434"),
      timeout=float(synth.get("timeout") or 60.0),
    )
  if backend == "subprocess":
    command = synth.get("command")
    if not command:
      raise ValueError("synth.backend=subprocess requires synth.command")
    return SubprocessAdapter(
      command=list(command),
      model_name=str(model) if model else None,
      timeout=timeout,
    )
  if backend == "dummy":
    return DummyAdapter(model_name=str(model) if model else None)
  raise ValueError(f"Unknown synth.backend: {backend}")
