# LLM Adapter Contract

This is Phase B forward documentation. Phase A does not call an LLM.

## Goals

LLM use must be pluggable, local-first, timeout-aware, and measurable. Parsing, synthesis, judging, and analysis should share a narrow adapter contract while allowing different local backends.

## Required Capabilities

An adapter should expose:

- `generate(prompt, *, timeout_s, max_tokens, temperature) -> LLMResponse`
- `generate_json(prompt, *, timeout_s, schema) -> dict`
- backend name and model identifier
- token accounting when the backend provides it
- latency measurement
- structured error reporting

## Response Shape

`LLMResponse` should include:

- `text`
- `model`
- `backend`
- `tokens_in`
- `tokens_out`
- `latency_ms`
- `error`

## Backend Selection

Default to an already running local backend. Do not hard-code a single provider. Later phases can add adapters for Ollama, llama-cpp-python, Codex, Claude, Pi, or Copilot as long as the local-only policy for sensitive data is respected.

## Failure Behavior

- Parser failures should return a conservative fallback query shape.
- Synthesis failures should not fabricate answers.
- Timeouts should be recorded as structured failures.
- All calls that feed a signal loop must record backend, model, latency, and token metadata when available.

