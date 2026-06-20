"""PII masking for stored/logged content (S4B.2).

Applies conservative, regex-only masking to user-supplied text before it
is persisted to the database or written to logs.  The masks are applied in
a fixed order so that longer/more-specific patterns (IBAN, CARD) are matched
before shorter ones (PHONE).

Rules:
- Masking is only applied to *stored* content — API responses return the
  original text to the requesting user (they know what they typed).
- No external NLP libraries; regex only.
- Only high-confidence patterns are masked to avoid false positives.
"""

from __future__ import annotations

import re


class PIIMasker:
    """Stateless PII masker.  Instantiate once and reuse across requests."""

    # ------------------------------------------------------------------
    # Compiled patterns (order matters — longer patterns first)
    # ------------------------------------------------------------------

    # Email addresses
    _EMAIL = re.compile(
        r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b",
        re.IGNORECASE,
    )

    # IBAN: 2-letter country code + 2 digits + 10-30 alphanumeric chars
    # (must come before PHONE/CARD to avoid partial matches)
    _IBAN = re.compile(
        r"\b[A-Z]{2}\d{2}[A-Z0-9]{10,30}\b",
        re.IGNORECASE,
    )

    # Credit/debit card: 13–19 contiguous digits possibly separated by spaces
    # or dashes.  Uses a negative-lookbehind so plain year-ranges like
    # "2020-2024" are not matched.
    _CARD = re.compile(
        r"(?<!\d)"                          # not preceded by digit
        r"(\d{4}[\s\-]?){3}\d{1,7}"        # 4-digit groups, last group 1-7 digits
        r"(?!\d)",                          # not followed by digit
    )

    # Phone numbers: 7+ digits with optional leading + and separators
    # Matches international and local formats such as:
    #   +1 (800) 555-0123, 07700 900123, +44-20-7946-0958, 5550123
    _PHONE = re.compile(
        r"(?<!\d)"
        r"\+?[\d][\d\s\-\.\(\)]{5,18}\d"
        r"(?!\d)",
    )

    # IPv4 addresses
    _IPV4 = re.compile(
        r"\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}"
        r"(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\b",
    )

    # Ordered list of (pattern, replacement) tuples.
    # Process IBAN before CARD before PHONE so longer sequences win.
    _RULES: list[tuple[re.Pattern, str]] = [
        (_EMAIL, "[EMAIL]"),
        (_IBAN,  "[IBAN]"),
        (_CARD,  "[CARD]"),
        (_IPV4,  "[IP]"),
        (_PHONE, "[PHONE]"),
    ]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def mask(self, text: str) -> str:
        """Return *text* with all detected PII replaced by placeholder tokens.

        Args:
            text: The raw user-supplied string.

        Returns:
            The masked string, or the original string if no PII is found.
        """
        for pattern, replacement in self._RULES:
            text = pattern.sub(replacement, text)
        return text


# Module-level singleton — import this everywhere PII masking is needed.
pii_masker = PIIMasker()
