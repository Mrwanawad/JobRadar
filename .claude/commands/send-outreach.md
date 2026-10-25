---
description: Send JobRadar's approved application drafts via Gmail / WhatsApp, then record results
allowed-tools: Bash(uv run jobradar sendable), Bash(uv run jobradar mark:*), mcp__claude_ai_Gmail__send_message, mcp__whatsapp__send_message, mcp__whatsapp__send_file
---

You are JobRadar's sender. Python already decided what may go out (dry run, daily cap,
one message per recipient). Send exactly that and nothing else.

1. Run `uv run jobradar sendable`. It prints JSON `{"blocked": str, "items": [...]}`.
   If `items` is empty, print `blocked` (or "nothing to send") and stop.
2. For each item, in order:
   - `channel == "email"`: call `mcp__claude_ai_Gmail__send_message` with
     `to=[recipient]`, `subject`, `body` exactly as given (plain text, do not edit or add Markdown).
   - `channel == "whatsapp"`: call `mcp__whatsapp__send_message` with `recipient` and
     `message=body`; then, if `attach` is non-empty, `mcp__whatsapp__send_file` with
     `recipient` and `media_path=attach`.
   - On success: `uv run jobradar mark <id> sent --ref "<message id or ok>"`.
   - On failure: `uv run jobradar mark <id> failed --ref "<short error>"` and continue.
3. Never send to any address or number not in `items`, never rewrite the text, never retry
   a failed item in the same run.
4. End with one line per item: `#id channel recipient -> sent|failed`.
