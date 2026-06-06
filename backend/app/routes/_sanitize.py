"""Shared input sanitiser for API request bodies.

Defence in depth. Pydantic only catches type/length issues; raw user
input can still smuggle control characters (NUL, ANSI escapes,
BiDi overrides) that flow through to:

* SQLite chat history JSON — corrupts the conversation log.
* LLM provider HTTP bodies — wastes tokens, can flip the system prompt
  via U+202E (right-to-left override).
* Toast event payloads / WS frames — breaks the browser renderer.

``sanitize_text`` is wired via Pydantic ``field_validator`` on every
free-form text field a client can post (chat message, otp, session
rename title, candidate id, session id). Vietnamese characters are
preserved — only the control / formatting blocks are stripped.

Failure mode: an input that's empty after sanitisation raises
``ValueError`` from the validator, which the project's
``RequestValidationError`` handler in ``app/main.py`` converts to a
400 with a Vietnamese ``detail``. No 422 leaks.
"""

from __future__ import annotations

import re
import unicodedata

# C0 controls except newline / tab / carriage-return. CR is dropped
# (we normalise to LF below); newline and tab survive so multi-line
# pastes keep their shape.
_C0_DROP = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# C1 controls — U+0080 to U+009F. No legitimate Vietnamese text uses
# these.
_C1_DROP = re.compile(r"[-]")

# Zero-width + BiDi formatting characters. Strip aggressively:
#   U+200B-U+200F — ZWSP, ZWNJ, ZWJ, LRM, RLM
#   U+202A-U+202E — LRE, RLE, PDF, LRO, RLO  (BiDi attack surface)
#   U+2066-U+2069 — LRI, RLI, FSI, PDI       (BiDi attack surface)
#   U+FEFF        — BOM / ZWNBSP
_ZW_BIDI_DROP = re.compile(
    r"[​-‏‪-‮⁦-⁩﻿]"
)

# 3 or more consecutive newlines collapses to 2 — preserves a blank
# line for legibility but blocks newline-flood DoS.
_NEWLINE_RUN = re.compile(r"\n{3,}")


def sanitize_text(value: str, *, max_len: int) -> str:
    """Clean and length-cap a free-form text field.

    Order matters:
      1. Drop CR (so ``\\r\\n`` → ``\\n``).
      2. Drop C0 / C1 controls + ZW / BiDi formatting characters.
      3. NFC-normalise so visually identical glyphs hash the same.
      4. Trim whitespace, collapse newline runs.
      5. Truncate to ``max_len``.

    Raises ``ValueError`` if the input is empty after sanitisation.
    """
    if not isinstance(value, str):
        raise ValueError("Trường này phải là chuỗi văn bản")
    # 1 + 2: control / formatting strip. ``\r`` goes first so a lone
    # CR doesn't survive as content.
    cleaned = value.replace("\r", "")
    cleaned = _C0_DROP.sub("", cleaned)
    cleaned = _C1_DROP.sub("", cleaned)
    cleaned = _ZW_BIDI_DROP.sub("", cleaned)
    # 3: NFC normalisation so combining-mark variants don't sneak past
    # the categoriser / alias resolver.
    cleaned = unicodedata.normalize("NFC", cleaned)
    # 4: trim + collapse.
    cleaned = _NEWLINE_RUN.sub("\n\n", cleaned).strip()
    # 5: length cap.
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len].rstrip()
    if not cleaned:
        raise ValueError("Nội dung không hợp lệ")
    return cleaned
