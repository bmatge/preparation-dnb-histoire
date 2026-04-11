"""Scoring déterministe Python pour l'épreuve Révision sciences.

Logique pure : entrée = chaîne saisie par l'élève + dict de scoring
(format `SciencesScoringPython.model_dump()`), sortie = `bool`. Pas
d'appel Albert ici. Stdlib uniquement (`decimal`).

Types supportés :

- **entier** : tolérance absolue 0 par défaut. Virgule FR acceptée si
  la partie fractionnaire est nulle (« 12,0 » == « 12 »).
- **decimal** : tolérance absolue par défaut 0.01.
- **pourcentage** : tolérance absolue par défaut 0.5. Signe `%` optionnel.
- **texte_court** : normalisation (lower, strip, accents, ponctuation,
  articles) avec inclusion généreuse.
- **qcm** : comparaison lex-normalisée stricte (identifiant court de la
  proposition, ex. « P2 », « 3 », « C »).
- **vrai_faux** : comparaison lex-normalisée avec synonymes courants
  (« oui » ≡ « vrai », « non » ≡ « faux »).

`formes_acceptees` (liste de chaînes) est testée en premier en
comparaison littérale après normalisation lex (lower + strip), avant
de tomber dans la logique spécifique au type.

La règle « ne jamais lever d'exception hors du module » est conservée :
toute chaîne mal formée renvoie `False`.

Ce module réutilise la logique de `app/mathematiques/automatismes/scoring.py`
en la spécialisant pour les besoins sciences. Pas d'import cross-matière —
le code est dupliqué volontairement, cf. CLAUDE.md §Architecture.
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
    """Normalisation lex légère pour `formes_acceptees` et QCM."""
    if text is None:
        return ""
    return _strip_accents(str(text)).strip().lower()


def _text_norm(text: str) -> str:
    """Normalisation agressive pour comparer du texte court."""
    if text is None:
        return ""
    out = _strip_accents(str(text)).lower().strip()
    out = re.sub(r"^(le|la|les|l[' ]|un|une|des|d[' ])\s*", "", out)
    out = _PONCTUATION.sub(" ", out)
    out = re.sub(r"\s+", " ", out).strip()
    return out


# ============================================================================
# Parsers numériques
# ============================================================================


def normalize_number(s: str) -> Decimal | None:
    """Parse un nombre décimal (français ou anglais).

    Accepte « 12 », « 12,0 », « 12.0 », « -3,14 », « 1 200 », « 1,5e-3 ».
    Pour la notation scientifique, on accepte un exposant entier derrière
    `e` ou `E`.
    """
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
    """Parse un pourcentage (« 15 », « 15% », « 15 % », « 15,5 % »)."""
    if s is None:
        return None
    txt = str(s).strip().rstrip("%").strip()
    return normalize_number(txt)


# ============================================================================
# Comparaison avec tolérance
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
# Vrai / faux : synonymes acceptés
# ============================================================================


_TRUE_SYNONYMS = {"vrai", "v", "oui", "o", "true", "1", "juste", "correct"}
_FALSE_SYNONYMS = {"faux", "f", "non", "n", "false", "0", "incorrect"}


def _normalize_vrai_faux(s: str) -> str | None:
    """Renvoie 'vrai', 'faux' ou None si non reconnu."""
    norm = _lex_norm(s).rstrip(".")
    if norm in _TRUE_SYNONYMS:
        return "vrai"
    if norm in _FALSE_SYNONYMS:
        return "faux"
    return None


# ============================================================================
# Point d'entrée — check
# ============================================================================


# Tolérances par défaut quand le JSON ne les précise pas. Calibrées sur
# les ordres de grandeur typiques d'un énoncé DNB sciences.
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
    """Évalue si la réponse élève matche le scoring fourni.

    `scoring` attendu sous forme de dict (format
    `SciencesScoringPython.model_dump()`). Si le mode n'est pas `python`,
    renvoie `False` (la couche `pedagogy.evaluate_answer` aiguillera vers
    Albert pour le mode `albert`).
    """
    if not isinstance(scoring, dict):
        return False
    if scoring.get("mode") != "python":
        return False
    if student_answer is None or not str(student_answer).strip():
        return False

    type_reponse = scoring.get("type_reponse") or "texte_court"
    expected_raw = scoring.get("reponse_canonique") or ""

    # 1. Court-circuit via `formes_acceptees`.
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
        # QCM : comparaison lex-normalisée stricte de l'identifiant
        # (« P2 », « 3 », « C »…). Ignore accents/casse, mais pas de
        # tolérance de forme — on veut que l'élève tape exactement la
        # lettre/le numéro attendu.
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

    # Type inconnu : échec silencieux.
    return False


__all__ = [
    "normalize_number",
    "normalize_percentage",
    "check",
]
