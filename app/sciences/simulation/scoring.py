"""Scoring deterministe Python pour l'epreuve Simulation sciences.

Copie adaptee de ``app/sciences/revision/scoring.py`` — pas d'import
cross-epreuve (cf. CLAUDE.md : code partage via ``app/core/`` uniquement).

Logique pure : entree = chaine saisie par l'eleve + dict de scoring,
sortie = ``bool``. Pas d'appel Albert ici.
"""

from __future__ import annotations

import re
import unicodedata
from decimal import Decimal, InvalidOperation
from typing import Any


# ============================================================================
# Normalisation textuelle
# ============================================================================


_PONCTUATION = re.compile(r"[^\w\s/.,-]")


def _strip_accents(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def _lex_norm(text: str) -> str:
    if text is None:
        return ""
    return _strip_accents(str(text)).strip().lower()


def _text_norm(text: str) -> str:
    if text is None:
        return ""
    out = _strip_accents(str(text)).lower().strip()
    out = re.sub(r"^(le|la|les|l[' ]|un|une|des|d[' ])\s*", "", out)
    out = _PONCTUATION.sub(" ", out)
    out = re.sub(r"\s+", " ", out).strip()
    return out


# ============================================================================
# Parsers numeriques
# ============================================================================


def normalize_number(s: str) -> Decimal | None:
    if s is None:
        return None
    txt = str(s).strip()
    if not txt:
        return None
    txt = txt.replace("\u202f", "").replace("\xa0", "").replace(" ", "")
    txt = txt.replace(",", ".")
    if not re.match(r"^[+-]?\d+(\.\d+)?([eE][+-]?\d+)?$", txt):
        return None
    try:
        return Decimal(txt)
    except InvalidOperation:
        return None


def normalize_percentage(s: str) -> Decimal | None:
    if s is None:
        return None
    txt = str(s).strip().rstrip("%").strip()
    return normalize_number(txt)


# ============================================================================
# Comparaison avec tolerance
# ============================================================================


def _decimals_match(
    student: Decimal, expected: Decimal, abs_tol: float, rel_tol: float
) -> bool:
    diff = abs(student - expected)
    if diff <= Decimal(str(abs_tol)):
        return True
    if rel_tol > 0 and expected != 0:
        rel = diff / abs(expected)
        if rel <= Decimal(str(rel_tol)):
            return True
    return False


# ============================================================================
# Vrai / faux : synonymes acceptes
# ============================================================================


_TRUE_SYNONYMS = {"vrai", "v", "oui", "o", "true", "1", "juste", "correct"}
_FALSE_SYNONYMS = {"faux", "f", "non", "n", "false", "0", "incorrect"}


def _normalize_vrai_faux(s: str) -> str | None:
    norm = _lex_norm(s).rstrip(".")
    if norm in _TRUE_SYNONYMS:
        return "vrai"
    if norm in _FALSE_SYNONYMS:
        return "faux"
    return None


# ============================================================================
# Point d'entree — check
# ============================================================================


_DEFAULT_TOLERANCES_ABS: dict[str, float] = {
    "entier": 0.0,
    "decimal": 0.01,
    "pourcentage": 0.5,
    "texte_court": 0.0,
    "qcm": 0.0,
    "vrai_faux": 0.0,
}


def _resolve_abs_tol(scoring: dict, type_reponse: str) -> float:
    tol = scoring.get("tolerances") or {}
    if isinstance(tol, dict) and "abs" in tol and tol.get("abs") is not None:
        try:
            return float(tol["abs"])
        except (TypeError, ValueError):
            pass
    return _DEFAULT_TOLERANCES_ABS.get(type_reponse, 0.0)


def _resolve_rel_tol(scoring: dict) -> float:
    tol = scoring.get("tolerances") or {}
    if isinstance(tol, dict) and "rel" in tol and tol.get("rel") is not None:
        try:
            return float(tol["rel"])
        except (TypeError, ValueError):
            pass
    return 0.0


def _try_formes_acceptees(scoring: dict, student_answer: str) -> bool | None:
    formes = scoring.get("formes_acceptees") or []
    if not formes:
        return None
    student = _lex_norm(student_answer)
    for f in formes:
        if _lex_norm(f) == student:
            return True
    return None


def check(scoring: dict[str, Any], student_answer: str) -> bool:
    """Evalue si la reponse eleve matche le scoring fourni."""
    if not isinstance(scoring, dict):
        return False
    if scoring.get("mode") != "python":
        return False
    if student_answer is None or not str(student_answer).strip():
        return False

    type_reponse = scoring.get("type_reponse") or "texte_court"
    expected_raw = scoring.get("reponse_canonique") or ""

    if _try_formes_acceptees(scoring, student_answer):
        return True

    abs_tol = _resolve_abs_tol(scoring, type_reponse)
    rel_tol = _resolve_rel_tol(scoring)

    if type_reponse in ("entier", "decimal"):
        student = normalize_number(student_answer)
        expected = normalize_number(expected_raw)
        if student is None or expected is None:
            return False
        if type_reponse == "entier" and abs_tol == 0.0:
            return student == expected
        return _decimals_match(student, expected, abs_tol, rel_tol)

    if type_reponse == "pourcentage":
        student = normalize_percentage(student_answer)
        expected = normalize_percentage(expected_raw)
        if student is None or expected is None:
            return False
        return _decimals_match(student, expected, abs_tol, rel_tol)

    if type_reponse == "qcm":
        return _lex_norm(student_answer) == _lex_norm(expected_raw)

    if type_reponse == "vrai_faux":
        student_norm = _normalize_vrai_faux(student_answer)
        expected_norm = _normalize_vrai_faux(expected_raw)
        if student_norm is None or expected_norm is None:
            return False
        return student_norm == expected_norm

    if type_reponse == "texte_court":
        student_norm = _text_norm(student_answer)
        expected_norm = _text_norm(expected_raw)
        if not student_norm or not expected_norm:
            return False
        if student_norm == expected_norm:
            return True
        if expected_norm in student_norm:
            return True
        return False

    return False


__all__ = [
    "normalize_number",
    "normalize_percentage",
    "check",
]
