"""Tokenisation helpers shared by the keyword index and the hashing embedder.

Schema text is full of identifiers like ``BidHeaderDetail`` or
``PODetailItems``; splitting them into words is what lets a question like
"bid header line specs" find the right table.
"""

from __future__ import annotations

import re

_CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")
_WORD = re.compile(r"[A-Za-z0-9_]+")

STOPWORDS = frozenset(
    "a an and are as at be by for from get give how i in is it list me of on or "
    "show that the their this to was were what when where which who with all "
    "find pull query table tables data".split()
)


def split_identifier(token: str) -> list[str]:
    """``PODetailItems`` -> ``["po", "detail", "items"]``."""
    parts: list[str] = []
    for chunk in token.split("_"):
        parts.extend(m.group(0).lower() for m in _CAMEL.finditer(chunk))
    return [p for p in parts if p]


def _stem(word: str) -> str:
    # Deliberately tiny: just enough that "vendors" matches "vendor".
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def tokens(text: str, drop_stopwords: bool = True) -> list[str]:
    """Lower-cased, identifier-split, lightly stemmed word tokens."""
    out: list[str] = []
    for raw in _WORD.findall(text):
        pieces = split_identifier(raw)
        whole = raw.lower()
        if len(pieces) > 1:
            out.append(whole)  # keep the full identifier too, for exact hits
        out.extend(pieces)
    out = [_stem(t) for t in out]
    if drop_stopwords:
        out = [t for t in out if t not in STOPWORDS]
    return out


def expand_identifiers(text: str) -> str:
    """Append the split form of every identifier so FTS5 can match words."""
    extra = []
    for raw in _WORD.findall(text):
        pieces = split_identifier(raw)
        if len(pieces) > 1:
            extra.append(" ".join(pieces))
    return text + ("\n" + " ".join(extra) if extra else "")
