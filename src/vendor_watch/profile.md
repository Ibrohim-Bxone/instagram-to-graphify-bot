# User Technical Profile & Ecosystem Context

## Active AI Coding Models & Subscriptions
- **Claude Code**: Anthropic Opus 5.5 across 2 Pro accounts. Primary role: system architect, specifications, and conflict resolution.
- **Codex CLI**: OpenAI GPT-6 Astra (ChatGPT Plus). Primary role: independent QA, security audits, and adversarial testing.
- **Antigravity (`agy`)**: Google Gemini Pro ecosystem (gemini-3.8-flash-*) with 10 rotating accounts. Primary driver and coder.
- **Gemini App**: Gemini 4 Argon for interactive multimodal reasoning.
- **Vertex AI**: Google Cloud Vertex AI enterprise environment (credit valid through 2026-11-12).
- **Ollama Local**: Zero-token local inference running qwen3.6, qwen2.5-coder:7b, and bge-m3 embeddings.

## Custom Internal Tools & Infrastructure
- **agy-run.py konveyer**: Multi-model coder/QA/review pipeline with automatic account failover.
- **argon-ish.py**: Desktop clipboard bridge connecting Gemini Argon with local environments.
- **Graphify**: Local semantic memory MCP server (ChromaDB + SQLite FTS5).
- **Uzbek TTS pipeline**: Custom Uzbek text-to-speech engine running on Vertex AI.
- **Antigravity Switcher**: Automatic account and quota rotation daemon for Antigravity IDE.
- **Telegram Knowledge Bot**: Knowledge extraction and synthesis bot for engineering workflows.

## Core Production Projects
- **Uzbekonacisco.uz**: Enterprise CCNP network engineering educational platform.
- **AI academiya**: Educational academy courses and automated media factory.

## Triage & Analysis Rules
1. Highlights must evaluate concrete relevance to the specific models, tools, and projects above.
2. Any overlap with existing custom tools must be tagged as "ehtimoliy" (potential overlap) and never state that it "replaces" existing tools.
3. SDK patch releases should be routed to background digests without interrupting active work unless containing security or breaking changes.
