"""A3 hybrid LLM router.

Hermes talks to this process as its ``openai-codex`` endpoint (``HERMES_CODEX_BASE_URL``).
Each Responses API request is classified and either forwarded unchanged to the ChatGPT
Codex backend (Hermes' own OAuth headers pass through) or rewritten for local Gemma 4
on Ollama, reached through the Tailscale SOCKS5 proxy in the cloud deployment.
"""

__version__ = "0.1.0"
