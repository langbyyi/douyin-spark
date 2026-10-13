"""Shared display-name identity used by configuration, chat and the send ledger."""

import unicodedata


def normalize_name(name: str) -> str:
    """Compare display names using Unicode width and whitespace normalization."""
    return " ".join(unicodedata.normalize("NFKC", name).split())
