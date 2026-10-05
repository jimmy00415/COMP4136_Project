"""Adversarial identity checks for model-controlled recommendation reasons."""

from __future__ import annotations

import unicodedata
from collections.abc import Sequence

_LINE_CONTROLS = frozenset("\r\n\v\f\u0085\u2028\u2029")


def has_forbidden_identity_controls(text: str) -> bool:
    """Reject formatting, bidi, and line controls before identity comparison."""
    return any(
        character in _LINE_CONTROLS
        or unicodedata.category(character) == "Cf"
        for character in text
    )


def contains_adversarial_identity(text: str, identities: Sequence[str]) -> bool:
    """Compare NFKC/casefold identities after NFKD mark/punctuation-space removal."""
    canonical_text = _identity_key(text)
    return any(
        canonical_identity in canonical_text
        for identity in identities
        if (canonical_identity := _identity_key(identity))
    )


def _identity_key(text: str) -> str:
    normalized = unicodedata.normalize(
        "NFKD", unicodedata.normalize("NFKC", text).casefold()
    )
    return "".join(
        character
        for character in normalized
        if not character.isspace()
        and not unicodedata.category(character).startswith("P")
        and not unicodedata.category(character).startswith("M")
    )
