"""Scoring déterministe Python pour les questions à réponse numérique.

Logique pure : entrée = chaîne saisie par l'élève + dict de scoring (au
format `ScoringPython` côté Pydantic), sortie = `bool` (correcte ou non).
Pas d'appel Albert ici. Stdlib uniquement (`decimal` + `fractions`).

Règles :

- **entier** : tolérance par défaut 0. Une virgule décimale FR est
  acceptée si la partie fractionnaire est nulle (« 12,0 » == « 12 »).

- **decimal** : tolérance par défaut absolue 0.01 (les sujets zéro
  donnent souvent 2 décimales). La virgule FR est acceptée à l'entrée.

- **fraction** : on parse `a/b`, on simplifie via `Fraction`. Une
  fraction non simplifiée est acceptée (`2/4` == `1/2`). Un décimal
  exact convertible (`0,5` pour `1/2`) est aussi accepté.

- **pourcentage** : tolérance par défaut absolue 0.5 (l'élève peut
  arrondir à l'entier près). Le signe `%` est optionnel à l'entrée.

- **texte_court** : comparaison normalisée (lower, strip, accents,
  ponctuation, articles). Inclusion généreuse, comme côté repères.

`formes_acceptees` (liste de chaînes) est tentée en premier en
comparaison littérale après normalisation lex (lower + strip), avant
de tomber dans la logique numérique. Elle sert à blanchir des formes
exactes attendues sans avoir à étendre le parser.

Aucune exception n'est levée hors du module : une réponse mal formée
renvoie simplement `False` (l'UI ne fait pas de différence entre « pas
parsable » et « faux numériquement »).
"""

from __future__ import annotations

import re
import unicodedata
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from typing import Any


# ============================================================================
# Normalisation textuelle
# ============================================================================


_PONCTUATION = re.compile(r"[^\w\s/.,-]")


def _strip_accents(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def _lex_norm(text: str) -> str:
    """Normalisation lex légère pour `formes_acceptees`."""
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
    """Parse un nombre décimal écrit à la française ou à l'anglaise.

    Accepte : « 12 », « 12,0 », « 12.0 », « -3,14 », « +0,5 », « 1 200 »,
    « 1 200,5 » (espace insécable, espace fin, espace classique comme
    séparateur de milliers). Renvoie `None` si la chaîne n'est pas un
    nombre.
    """
    if s is None:
        return None
    txt = str(s).strip()
    if not txt:
        return None
    txt = txt.replace("\u202f", "").replace("\xa0", "").replace(" ", "")
    txt = txt.replace(",", ".")
    if not re.match(r"^[+-]?\d+(\.\d+)?$", txt):
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


def normalize_fraction(s: str) -> Fraction | None:
    """Parse une fraction `a/b` (numérateur et dénominateur entiers).

    Accepte aussi un nombre décimal exact convertible (« 0,5 » → `1/2`).
    Renvoie `None` si rien ne marche.
    """
    if s is None:
        return None
    txt = str(s).strip().replace(" ", "").replace("\u202f", "")
    if "/" in txt:
        m = re.match(r"^([+-]?\d+)\s*/\s*([+-]?\d+)$", txt)
        if not m:
            return None
        num = int(m.group(1))
        den = int(m.group(2))
        if den == 0:
            return None
        return Fraction(num, den)
    # Fallback : décimal convertible exactement
    dec = normalize_number(txt)
    if dec is None:
        return None
    try:
        return Fraction(dec).limit_denominator(10_000)
    except (TypeError, ValueError):
        return None


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
# Point d'entrée — check
# ============================================================================


# Tolérances par défaut quand le JSON ne les précise pas. Calibrées pour
# matcher les conventions DNB observées sur les sujets zéro 2026.
_DEFAULT_TOLERANCES_ABS: dict[str, float] = {
    "entier": 0.0,
    "decimal": 0.01,
    "pourcentage": 0.5,
    "fraction": 0.0,
    "texte_court": 0.0,
}


def _resolve_abs_tol(scoring: dict, type_reponse: str) -> float:
    """Tolérance absolue effective : valeur du JSON si fournie, sinon défaut."""
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

    `scoring` est attendu sous forme de dict (format `ScoringPython.model_dump()`).
    Si le mode n'est pas `python`, on renvoie `False` — `pedagogy.evaluate_answer`
    aiguillera vers Albert pour le mode `albert`.
    """
    if not isinstance(scoring, dict):
        return False
    if scoring.get("mode") != "python":
        return False
    if student_answer is None or not str(student_answer).strip():
        return False

    type_reponse = scoring.get("type_reponse") or "decimal"
    expected_raw = scoring.get("reponse_canonique") or ""

    # 1. Tentative formes_acceptees (court-circuit). On le tente AVANT le
    # parsing numérique pour qu'une forme exacte attendue ait toujours la
    # priorité (utile par ex. pour des notations particulières comme
    # « 1/2 » alors qu'on autorise aussi « 0,5 »).
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
            # Match strict sur la partie entière (« 12,0 » == « 12 »).
            return student == expected
        return _decimals_match(student, expected, abs_tol, rel_tol)

    if type_reponse == "pourcentage":
        student = normalize_percentage(student_answer)
        expected = normalize_percentage(expected_raw)
        if student is None or expected is None:
            return False
        return _decimals_match(student, expected, abs_tol, rel_tol)

    if type_reponse == "fraction":
        student = normalize_fraction(student_answer)
        expected = normalize_fraction(expected_raw)
        if student is None or expected is None:
            return False
        return student == expected

    if type_reponse == "texte_court":
        student_norm = _text_norm(student_answer)
        expected_norm = _text_norm(expected_raw)
        if not student_norm or not expected_norm:
            return False
        if student_norm == expected_norm:
            return True
        if expected_norm in student_norm:
            return True
        # Expressions mathématiques type « 8x » / « 8 x » / « 2 n » : on
        # retente sans aucun espace. Utile pour les réponses littérales
        # courtes (« 2n », « n+1 », « 8x ») où l'espace entre coefficient
        # et variable ne devrait pas faire échouer la comparaison.
        return expected_norm.replace(" ", "") == student_norm.replace(" ", "")

    # Type inconnu : on échoue silencieusement.
    return False


__all__ = [
    "normalize_number",
    "normalize_percentage",
    "normalize_fraction",
    "check",
]
