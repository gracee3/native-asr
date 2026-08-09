#!/usr/bin/env python3
"""Versioned transcript normalization and word error accounting."""

from __future__ import annotations

import re
import unicodedata

NORMALIZATION_VERSION = "english-upper-apostrophe-v1"


def normalize(text: str) -> str:
    text = text.translate(str.maketrans({"’": "'", "‘": "'", "ʼ": "'", "`": "'"})).upper()
    cleaned = []
    for character in text:
        category = unicodedata.category(character)
        if character == "'" or character.isspace() or category[0] in ("L", "N", "M"):
            cleaned.append(character)
        elif category.startswith("P") or category.startswith("S"):
            cleaned.append(" ")
        else:
            cleaned.append(character)
    return re.sub(r"\s+", " ", "".join(cleaned)).strip()


def errors(reference: str, hypothesis: str) -> dict[str, int]:
    ref = normalize(reference).split()
    hyp = normalize(hypothesis).split()
    # Each cell is (total edits, substitutions, deletions, insertions).
    previous = [(index, 0, 0, index) for index in range(len(hyp) + 1)]
    for ref_index, ref_word in enumerate(ref, 1):
        current = [(ref_index, 0, ref_index, 0)]
        for hyp_index, hyp_word in enumerate(hyp, 1):
            if ref_word == hyp_word:
                current.append(previous[hyp_index - 1])
                continue
            substitution = previous[hyp_index - 1]
            deletion = previous[hyp_index]
            insertion = current[hyp_index - 1]
            candidates = (
                (substitution[0] + 1, substitution[1] + 1, substitution[2], substitution[3]),
                (deletion[0] + 1, deletion[1], deletion[2] + 1, deletion[3]),
                (insertion[0] + 1, insertion[1], insertion[2], insertion[3] + 1),
            )
            current.append(min(candidates))
        previous = current
    total, substitutions, deletions, insertions = previous[-1]
    return {
        "errors": total, "substitutions": substitutions, "deletions": deletions,
        "insertions": insertions, "reference_words": len(ref),
    }
