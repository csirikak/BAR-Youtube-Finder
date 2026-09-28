"""Conservative cleanup of player names read from the BAR player list."""

import re
import sqlite3
import unicodedata
from collections import defaultdict
from contextlib import closing
from functools import lru_cache
from pathlib import Path

from rapidfuzz import fuzz, process


UI_LABEL = re.compile(
    r"^(?:enemies|spectators|units|total|fps|tors)(?:\s*[:/]\s*|\s+)?[\d.,/ ]*$",
    re.IGNORECASE,
)
STATISTIC = re.compile(r"^[\d., ]+[kKmM%↑↓tIil|]*$")


def clean_text(text):
    text = unicodedata.normalize("NFKC", text).strip()
    # A star marks a friend in the UI; it is not part of the account name.
    text = text.lstrip("*°").strip()
    return text


def is_name(text):
    return (
        3 <= len(text) <= 32
        and sum(char.isalnum() or char in "_[]()-" for char in text) >= 3
        and any(char.isalpha() for char in text)
        and not UI_LABEL.fullmatch(text)
        and not STATISTIC.fullmatch(text)
        and not text.casefold().endswith("(ai)")
    )


def text_variants(text):
    """Keep the original first, so genuine leading/trailing digits survive."""
    variants = [clean_text(text)]
    # OCR occasionally reads rank icons as these glyphs. Only remove them
    # when followed by a numeric rank, never from an arbitrary Unicode name.
    without_badge = re.sub(r"^[参谷会鑫米灸多系众 ]+(?=\d)", "", variants[0])
    if without_badge != variants[0]:
        variants.append(without_badge)
    for value in list(variants):
        without_rank = re.sub(r"^\d{1,3}\s*\*?\s*", "", value)
        if without_rank != value:
            variants.append(without_rank)
    for value in list(variants):
        # Adjacent resource statistics sometimes merge with the name.
        without_stats = re.sub(r"\s+\d{1,3}\s*[↑↓tIil|1,. ]*$", "", value)
        if without_stats != value:
            variants.append(without_stats)
        # Attached digits are removed only if a resulting name is in the
        # local player catalog; unknown names keep their original digits.
        without_stats = re.sub(r"\d{1,3}[.,]\d{1,3}\s*[↑↓tIil|,. ]*$", "", value)
        if without_stats != value:
            variants.append(without_stats)
        without_stats = re.sub(r"\d{1,3}\s*[↑↓tIil|,. ]*$", "", value)
        if without_stats != value:
            variants.append(without_stats)
    return list(dict.fromkeys(clean_text(value) for value in variants if is_name(clean_text(value))))


class PlayerNames:
    def __init__(self, names=()):
        self.names = frozenset(names)
        self.folded = defaultdict(list)
        self.buckets = defaultdict(list)
        for name in sorted(self.names):
            self.folded[name.casefold()].append(name)
            self.buckets[(name[0].casefold(), len(name))].append(name)

    @classmethod
    def from_database(cls, filename):
        path = Path(filename)
        if not path.is_file():
            return cls()
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as database:
            names = [row[0] for row in database.execute("SELECT player_name FROM players") if row[0]]
        return cls(names)

    @lru_cache(maxsize=8192)
    def resolve(self, raw):
        variants = text_variants(raw)
        if not variants:
            return None
        # Prefer the unmodified spelling, then exact catalog matches after
        # removing surrounding UI text. Case-sensitive names remain intact.
        for value in variants:
            if value in self.names:
                return value
        for value in variants:
            matches = self.folded.get(value.casefold(), ())
            if len(matches) == 1:
                return matches[0]

        # Correct small OCR typos only when one catalog entry wins clearly.
        # Restrict the search by initial and length to keep it inexpensive.
        matches = {}
        for value in variants:
            if len(value) < 6:
                continue
            candidates = [
                name for length in range(len(value) - 2, len(value) + 3)
                for name in self.buckets.get((value[0].casefold(), length), ())
            ]
            for name, score, _ in process.extract(
                    value, candidates, scorer=fuzz.ratio, limit=2, score_cutoff=88):
                matches[name] = max(matches.get(name, 0), score)
        ranked = sorted(matches.items(), key=lambda pair: pair[1], reverse=True)
        if ranked and ranked[0][1] >= 90 and (
                len(ranked) == 1 or ranked[0][1] - ranked[1][1] >= 5):
            return ranked[0][0]

        # Without an unambiguous catalog match, retain the OCR spelling.
        # Only clearly separated ranks/statistics can be removed safely.
        fallback = re.sub(r"^\d{1,3}\s+\*?", "", clean_text(raw))
        fallback = re.sub(r"\s+\d{1,3}\s*[↑↓tIil|1,. ]*$", "", fallback)
        fallback = clean_text(fallback)
        return fallback if is_name(fallback) else None
