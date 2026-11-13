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

SPANISH_GENDER_NOUNS = {
    "amigo": "amiga", "amigos": "amigas",
    "chico": "chica", "chicos": "chicas",
    "niño": "niña", "niños": "niñas",
    "muchacho": "muchacha", "muchachos": "muchachas",
    "hermano": "hermana", "hermanos": "hermanas",
    "hijo": "hija", "hijos": "hijas",
    "abuelo": "abuela", "abuelos": "abuelas",
    "tío": "tía", "tíos": "tías",
    "primo": "prima", "primos": "primas",
    "compañero": "compañera", "compañeros": "compañeras",
    "camarero": "camarera", "camareros": "camareras",
    "cocinero": "cocinera", "cocineros": "cocineras",
    "enfermero": "enfermera", "enfermeros": "enfermeras",
    "vecino": "vecina", "vecinos": "vecinas",
    "maestro": "maestra", "maestros": "maestras",
    "alumno": "alumna", "alumnos": "alumnas",
    "médico": "médica", "médicos": "médicas",
    "abogado": "abogada", "abogados": "abogadas",
    "secretario": "secretaria", "secretarios": "secretarias",
    "profesor": "profesora", "profesores": "profesoras",
    "doctor": "doctora", "doctores": "doctoras",
    "director": "directora", "directores": "directoras",
    "señor": "señora", "señores": "señoras",
    "escritor": "escritora", "escritores": "escritoras",
    "jugador": "jugadora", "jugadores": "jugadoras",
    "padre": "madre", "padres": "madres",
    "hombre": "mujer", "hombres": "mujeres",
    "rey": "reina", "reyes": "reinas",
}

SPANISH_GENDER_ADJECTIVES = {
    "alto": "alta", "altos": "altas",
    "bajo": "baja", "bajos": "bajas",
    "pequeño": "pequeña", "pequeños": "pequeñas",
    "bonito": "bonita", "bonitos": "bonitas",
    "guapo": "guapa", "guapos": "guapas",
    "simpático": "simpática", "simpáticos": "simpáticas",
    "tranquilo": "tranquila", "tranquilos": "tranquilas",
    "rápido": "rápida", "rápidos": "rápidas",
    "lento": "lenta", "lentos": "lentas",
    "nuevo": "nueva", "nuevos": "nuevas",
    "viejo": "vieja", "viejos": "viejas",
    "bueno": "buena", "buenos": "buenas",
    "malo": "mala", "malos": "malas",
    "cansado": "cansada", "cansados": "cansadas",
    "contento": "contenta", "contentos": "contentas",
    "enfermo": "enferma", "enfermos": "enfermas",
    "aburrido": "aburrida", "aburridos": "aburridas",
    "divertido": "divertida", "divertidos": "divertidas",
    "listo": "lista", "listos": "listas",
    "rojo": "roja", "rojos": "rojas",
    "blanco": "blanca", "blancos": "blancas",
    "negro": "negra", "negros": "negras",
    "amarillo": "amarilla", "amarillos": "amarillas",
    "rubio": "rubia", "rubios": "rubias",
    "frío": "fría", "fríos": "frías",
    "seco": "seca", "secos": "secas",
    "limpio": "limpia", "limpios": "limpias",
    "sucio": "sucia", "sucios": "sucias",
    "rico": "rica", "ricos": "ricas",
    "largo": "larga", "largos": "largas",
    "corto": "corta", "cortos": "cortas",
    "caro": "cara", "caros": "caras",
    "barato": "barata", "baratos": "baratas",
}

ALL_M2F = {**SPANISH_GENDER_NOUNS, **SPANISH_GENDER_ADJECTIVES}
ALL_F2M = {v: k for k, v in ALL_M2F.items()}
ARTICLE_M2F = {"el": "la", "los": "las", "un": "una", "unos": "unas"}
ARTICLE_F2M = {v: k for k, v in ARTICLE_M2F.items()}

SWEDISH_GENDER_PAIRS = {
    "pojke": "flicka", "pojken": "flickan",
    "kille": "tjej", "killen": "tjejen",
    "bror": "syster", "brodern": "systern",
    "son": "dotter", "sonen": "dottern",
    "morfar": "mormor", "farfar": "farmor",
    "farbror": "faster", "morbror": "moster",
    "man": "kvinna", "mannen": "kvinnan",
    "pappa": "mamma", "pappan": "mamman",
    "kung": "drottning", "kungen": "drottningen",
    "herre": "dam", "herrn": "damen",
}
SWE_REV = {v: k for k, v in SWEDISH_GENDER_PAIRS.items()}


@dataclass
class WordPair:
    term: str
    translation: str
    term_alts: list[str] = field(default_factory=list)
    translation_alts: list[str] = field(default_factory=list)


def expand_slashes(text: str) -> list[str]:
    """Expands Spanish gender slash notations like 'el/la amigo/a' or 'cansado/a'."""
    s = text.strip()
    m = re.match(r"^(el|la|un|una)/(el|la|un|una)\s+([a-zA-ZáéíóúüñÁÉÍÓÚÜÑ]+)o/-?a$", s, re.IGNORECASE)
    if m:
        art1, art2, stem = m.groups()
        return [f"{art1} {stem}o", f"{art2} {stem}a"]
    m = re.match(r"^(el|la|un|una)/(el|la|un|una)\s+([a-zA-ZáéíóúüñÁÉÍÓÚÜÑ]+(?:or|ón|án|ín))/-?a$", s, re.IGNORECASE)
    if m:
        art1, art2, stem = m.groups()
        return [f"{art1} {stem}", f"{art2} {stem}a"]
    m = re.match(r"^(el|la|un|una)/(el|la|un|una)\s+([a-zA-ZáéíóúüñÁÉÍÓÚÜÑ]+)$", s, re.IGNORECASE)
    if m:
        art1, art2, noun = m.groups()
        return [f"{art1} {noun}", f"{art2} {noun}"]
    m = re.match(r"^([a-zA-ZáéíóúüñÁÉÍÓÚÜÑ]+)o/-?a$", s, re.IGNORECASE)
    if m:
        stem = m.group(1)
        return [f"{stem}o", f"{stem}a"]
    m = re.match(r"^([a-zA-ZáéíóúüñÁÉÍÓÚÜÑ]+(?:or|ón|án|ín))/-?a$", s, re.IGNORECASE)
    if m:
        stem = m.group(1)
        return [stem, f"{stem}a"]
    return [s]


def _split_alternatives(text: str) -> list[str]:
    expanded = expand_slashes(text)
    if len(expanded) > 1:
        return expanded
    parts = [p.strip() for p in re.split(r"\s*[/;,]\s*", text) if p.strip()]
    out = []
    for part in parts:
        out.extend(expand_slashes(part))
    return out or [text.strip()]


def _swedish_target(translation: str, target_gender: str) -> str:
    t = translation.strip()
    if not t:
        return t
    low = t.lower()
    if target_gender == "femininum" and low in SWEDISH_GENDER_PAIRS:
        return SWEDISH_GENDER_PAIRS[low]
    if target_gender == "maskulinum" and low in SWE_REV:
        return SWE_REV[low]
    return t


def opposite_gender(term: str, translation: str = "", term_alts: list[str] | None = None) -> tuple[str, str, str] | None:
    """Returns (opposite_spanish, target_gender, swedish_hint) or None."""
    term_clean = term.strip()
    words = term_clean.split()
    if not words:
        return None

    alts = term_alts or []
    for alt in alts:
        alt_words = alt.strip().split()
        if len(words) == 2 and len(alt_words) == 2:
            if words[0].lower() in ARTICLE_M2F and alt_words[0].lower() in ARTICLE_F2M:
                return alt, "femininum", _swedish_target(translation, "femininum")
            if words[0].lower() in ARTICLE_F2M and alt_words[0].lower() in ARTICLE_M2F:
                return alt, "maskulinum", _swedish_target(translation, "maskulinum")
        elif len(words) == 1 and len(alt_words) == 1:
            if words[0].lower().endswith("o") and alt_words[0].lower().endswith("a"):
                return alt, "femininum", _swedish_target(translation, "femininum")
            if words[0].lower().endswith("a") and alt_words[0].lower().endswith("o"):
                return alt, "maskulinum", _swedish_target(translation, "maskulinum")

    if len(words) == 2 and words[0].lower() in ARTICLE_M2F:
        w = words[1].lower()
        if w in ALL_M2F:
            return f"{ARTICLE_M2F[words[0].lower()]} {ALL_M2F[w]}", "femininum", _swedish_target(translation, "femininum")
    elif len(words) == 2 and words[0].lower() in ARTICLE_F2M:
        w = words[1].lower()
        if w in ALL_F2M:
            return f"{ARTICLE_F2M[words[0].lower()]} {ALL_F2M[w]}", "maskulinum", _swedish_target(translation, "maskulinum")
    elif len(words) == 1:
        w = words[0].lower()
        if w in ALL_M2F:
            return ALL_M2F[w], "femininum", _swedish_target(translation, "femininum")
        elif w in ALL_F2M:
            return ALL_F2M[w], "maskulinum", _swedish_target(translation, "maskulinum")

    return None


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
        # In Spanish, answering with the opposite gender is wrong, not a typo.
        if language == "es":
            opp = opposite_gender(form)
            if opp:
                opp_bare, _ = drop_article(strip_accents(opp[0]), language)
                if normalize(given_bare) == normalize(opp_bare):
                    best = CheckResult(Verdict.WRONG, "gender", form)
                    continue
        # Small typo: one letter for short words, two for longer ones.
        allowed = 1 if len(target_bare) <= 6 else 2
        if len(target_bare) >= 4 and edit_distance(given_bare, target_bare) <= allowed:
            best = best or CheckResult(Verdict.ALMOST, "typo", form)
    return best or CheckResult(Verdict.WRONG, expected=main)
