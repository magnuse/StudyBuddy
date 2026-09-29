"""Word lists: parsing what parents send, and checking answers without a language model."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from enum import Enum

# Separators between the foreign word and the Swedish word, most specific first.
SEPARATORS = [r"\t+", r"\s+[–—-]+\s+", r"\s*=\s*", r"\s*:\s+", r"\s{3,}"]
ARTICLES = {
    "es": ["el", "la", "los", "las", "un", "una", "unos", "unas"],
    "en": ["the", "a", "an"],
    "de": ["der", "die", "das", "ein", "eine"],
    "fr": ["le", "la", "les", "un", "une", "l'"],
}
SWEDISH_PREFIXES = ["att", "en", "ett"]


@dataclass
class WordPair:
    term: str
    translation: str
    term_alts: list[str] = field(default_factory=list)
    translation_alts: list[str] = field(default_factory=list)


def _split_alternatives(text: str) -> list[str]:
    parts = [p.strip() for p in re.split(r"\s*[/;,]\s*", text) if p.strip()]
    return parts or [text.strip()]


def parse_word_list(text: str) -> list[WordPair]:
    """Parses lines like 'la casa - huset' or 'el coche / el carro = bilen'."""
    pairs = []
    for raw in text.splitlines():
        line = raw.strip().strip("•*·").strip()
        line = re.sub(r"^\d+[.)]\s*", "", line)
        if not line:
            continue
        for separator in SEPARATORS:
            parts = re.split(separator, line, maxsplit=1)
            if len(parts) == 2 and parts[0].strip() and parts[1].strip():
                terms = _split_alternatives(parts[0])
                translations = _split_alternatives(parts[1])
                pairs.append(WordPair(terms[0], translations[0], terms[1:], translations[1:]))
                break
    return pairs


def strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn")


def normalize(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[¿?¡!.,;:\"()]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def drop_article(text: str, language: str | None) -> tuple[str, str | None]:
    words = text.split(" ")
    articles = ARTICLES.get(language or "", []) if language != "sv" else SWEDISH_PREFIXES
    if len(words) > 1 and words[0] in articles:
        return " ".join(words[1:]), words[0]
    return text, None


def edit_distance(a: str, b: str) -> int:
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb)))
        previous = current
    return previous[-1]


class Verdict(str, Enum):
    CORRECT = "correct"
    ALMOST = "almost"  # counts as right, with a note (accent, article or small typo)
    WRONG = "wrong"  # clearly not on the list; may still be a synonym


@dataclass
class CheckResult:
    verdict: Verdict
    reason: str | None = None  # 'accent', 'article', 'typo'
    expected: str = ""

    @property
    def score(self) -> int:
        return {Verdict.CORRECT: 2, Verdict.ALMOST: 2, Verdict.WRONG: 0}[self.verdict]


def check_answer(answer: str, expected: list[str], language: str | None, strict_accents: bool = False) -> CheckResult:
    """Checks a typed answer against the accepted forms of one word.

    `language` is the language of the expected answer: 'sv' for Swedish, or e.g. 'es'.
    """
    given = normalize(answer)
    main = expected[0]
    for form in expected:
        target = normalize(form)
        if given == target:
            return CheckResult(Verdict.CORRECT, expected=form)

    best: CheckResult | None = None
    for form in expected:
        target = normalize(form)
        # Missing accent or ñ.
        if strip_accents(given) == strip_accents(target):
            result = CheckResult(Verdict.WRONG if strict_accents else Verdict.ALMOST, "accent", form)
            if not strict_accents:
                return result
            best = best or result
            continue
        # Wrong or missing article (el/la), or Swedish 'att'/'en'/'ett'.
        given_bare, given_article = drop_article(strip_accents(given), language)
        target_bare, target_article = drop_article(strip_accents(target), language)
        if given_bare == target_bare and given_article != target_article:
            if language == "sv":
                return CheckResult(Verdict.CORRECT, expected=form)
            return CheckResult(Verdict.ALMOST, "article", form)
        # Small typo: one letter for short words, two for longer ones.
        allowed = 1 if len(target_bare) <= 6 else 2
        if len(target_bare) >= 4 and edit_distance(given_bare, target_bare) <= allowed:
            best = best or CheckResult(Verdict.ALMOST, "typo", form)
    return best or CheckResult(Verdict.WRONG, expected=main)
