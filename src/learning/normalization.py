"""Versioned surface normalization only: no stemming, synonyms or semantic merging."""
from __future__ import annotations

import hashlib
import re
import unicodedata

NORMALIZATION_VERSION = '1'
_TRANSLATION = str.maketrans({'’': "'", '‘': "'", '“': '"', '”': '"'})


def normalize_expression(value: str) -> str:
    text = unicodedata.normalize('NFKC', value).translate(_TRANSLATION).casefold()
    text = re.sub(r'\s+', ' ', text).strip()
    # Keep internal punctuation (in/out vs in out, don't vs dont) and hyphens.
    # Leading/trailing quotation marks and terminal sentence punctuation are surface variants.
    wrappers = {'"': '"', "'": "'", '`': '`', '「': '」', '『': '』'}
    while text:
        previous = text
        text = text.rstrip(' .,!?:;。！？')
        if len(text) >= 2 and wrappers.get(text[0]) == text[-1]:
            text = text[1:-1].strip()
        if text == previous:
            break
    return text


def expression_id(value: str) -> str:
    normalized = normalize_expression(value)
    if not normalized:
        raise ValueError('Expression must not be empty after normalization')
    return f'exp:v{NORMALIZATION_VERSION}:' + hashlib.sha256(normalized.encode('utf-8')).hexdigest()
