"""Tests du scoring déterministe Python pour les automatismes maths.

On couvre les 4 types numériques (entier, decimal, fraction, pourcentage)
et le type texte_court avec ≥ 20 cas par type, en se concentrant sur les
pièges identifiés au DNB :

- virgule décimale française vs point anglais
- fraction non simplifiée vs irréductible
- pourcentage avec ou sans signe `%`
- entier avec écriture décimale équivalente (« 12,0 » == « 12 »)
- réponse vide / espaces / unités parasites
- formes_acceptees prioritaire sur le parsing numérique
"""

from __future__ import annotations

import pytest

from app.mathematiques.automatismes.scoring import (
    check,
    normalize_fraction,
    normalize_number,
    normalize_percentage,
)


# ============================================================================
# normalize_number — fonction bas niveau
# ============================================================================


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("12", "12"),
        ("12,0", "12.0"),
        ("12.0", "12.0"),
        ("-3,14", "-3.14"),
        ("+0,5", "0.5"),
        ("1 200", "1200"),
        ("1\u202f200", "1200"),
        ("1\xa0200,5", "1200.5"),
        ("0", "0"),
        ("0,001", "0.001"),
        ("", None),
        (None, None),
        ("abc", None),
        ("12 km", None),
        ("12.0.0", None),
    ],
)
def test_normalize_number(raw, expected):
    result = normalize_number(raw)
    if expected is None:
        assert result is None
    else:
        from decimal import Decimal

        assert result == Decimal(expected)


# ============================================================================
# normalize_fraction — bas niveau
# ============================================================================


@pytest.mark.parametrize(
    "raw,num,den",
    [
        ("1/2", 1, 2),
        ("2/4", 1, 2),
        ("3/4", 3, 4),
        ("-1/2", -1, 2),
        ("0/5", 0, 1),
        ("10/15", 2, 3),
        ("0,5", 1, 2),
        ("0.5", 1, 2),
        ("0,25", 1, 4),
        ("1,5", 3, 2),
        ("2,5", 5, 2),
    ],
)
def test_normalize_fraction_valid(raw, num, den):
    from fractions import Fraction

    assert normalize_fraction(raw) == Fraction(num, den)


@pytest.mark.parametrize(
    "raw",
    ["", None, "abc", "1/0", "1//2", "x/2"],
)
def test_normalize_fraction_invalid(raw):
    assert normalize_fraction(raw) is None


# ============================================================================
# normalize_percentage — bas niveau
# ============================================================================


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("15", "15"),
        ("15%", "15"),
        ("15 %", "15"),
        ("15,5", "15.5"),
        ("15,5 %", "15.5"),
        ("100", "100"),
        ("0", "0"),
        ("", None),
    ],
)
def test_normalize_percentage(raw, expected):
    result = normalize_percentage(raw)
    if expected is None:
        assert result is None
    else:
        from decimal import Decimal

        assert result == Decimal(expected)


# ============================================================================
# check() — type entier
# ============================================================================


def _scoring_entier(rep: str, **kw):
    return {
        "mode": "python",
        "type_reponse": "entier",
        "reponse_canonique": rep,
        **kw,
    }


@pytest.mark.parametrize(
    "rep,answer,expected",
    [
        ("12", "12", True),
        ("12", "12,0", True),
        ("12", "12.0", True),
        ("12", " 12 ", True),
        ("12", "11", False),
        ("12", "13", False),
        ("12", "douze", False),
        ("12", "", False),
        ("12", None, False),
        ("0", "0", True),
        ("0", "0,0", True),
        ("-5", "-5", True),
        ("-5", "−5", False),
        ("-5", "5", False),
        ("100", "100", True),
        ("100", "1 00", True),
        ("100", "100 cm", False),
        ("144", "144", True),
        ("144", "144,0", True),
        ("3", "3.0000", True),
        ("3", "3,01", False),
        ("0", "", False),
    ],
)
def test_check_entier(rep, answer, expected):
    assert check(_scoring_entier(rep), answer) is expected


# ============================================================================
# check() — type decimal
# ============================================================================


def _scoring_decimal(rep: str, **kw):
    return {
        "mode": "python",
        "type_reponse": "decimal",
        "reponse_canonique": rep,
        **kw,
    }


@pytest.mark.parametrize(
    "rep,answer,expected",
    [
        ("8.9", "8.9", True),
        ("8.9", "8,9", True),
        ("8.9", "8.90", True),
        ("8.9", "8.91", True),  # tolérance défaut 0.01
        ("8.9", "8.92", False),
        ("8.9", "8,8", False),
        ("0.5", "0,5", True),
        ("0.5", "1/2", False),  # decimal n'accepte pas la fraction
        ("2.5", "2,5", True),
        ("2.5", "2.5 m", False),
        ("-3.14", "-3,14", True),
        ("-3.14", "−3,14", False),
        ("100", "100", True),
        ("100", "100,0", True),
        ("100", "100.001", True),
        ("0", "0", True),
        ("0", "0,001", True),
        ("0", "0,02", False),
        ("5.75", "5,75", True),
        ("5.75", "5.75", True),
        ("5.75", "23/4", False),
        ("5.75", "5,8", False),
    ],
)
def test_check_decimal(rep, answer, expected):
    assert check(_scoring_decimal(rep), answer) is expected


# ============================================================================
# check() — type fraction
# ============================================================================


def _scoring_fraction(rep: str, **kw):
    return {
        "mode": "python",
        "type_reponse": "fraction",
        "reponse_canonique": rep,
        **kw,
    }


@pytest.mark.parametrize(
    "rep,answer,expected",
    [
        ("3/4", "3/4", True),
        ("3/4", "6/8", True),
        ("3/4", "9/12", True),
        ("3/4", "0,75", True),
        ("3/4", "0.75", True),
        ("3/4", "0,8", False),
        ("3/4", "3 / 4", True),
        ("3/4", "", False),
        ("1/2", "1/2", True),
        ("1/2", "2/4", True),
        ("1/2", "0,5", True),
        ("1/2", "1/3", False),
        ("1/2", "0,4", False),
        ("7/4", "7/4", True),
        ("7/4", "1,75", True),
        ("7/4", "14/8", True),
        ("7/4", "5/4", False),
        ("5/2", "5/2", True),
        ("5/2", "2,5", True),
        ("5/2", "10/4", True),
        ("3/10", "3/10", True),
        ("3/10", "0,3", True),
        ("1/4", "0,25", True),
        ("1/4", "25/100", True),
    ],
)
def test_check_fraction(rep, answer, expected):
    assert check(_scoring_fraction(rep), answer) is expected


# ============================================================================
# check() — type pourcentage
# ============================================================================


def _scoring_pourcentage(rep: str, **kw):
    return {
        "mode": "python",
        "type_reponse": "pourcentage",
        "reponse_canonique": rep,
        **kw,
    }


@pytest.mark.parametrize(
    "rep,answer,expected",
    [
        ("25", "25", True),
        ("25", "25%", True),
        ("25", "25 %", True),
        ("25", "25,0", True),
        ("25", "25,4", True),  # tolérance 0.5
        ("25", "25,6", False),
        ("25", "26", False),
        ("60", "60", True),
        ("60", "60%", True),
        ("60", "60 %", True),
        ("60", "59,5", True),
        ("60", "61", False),
        ("100", "100%", True),
        ("100", "100", True),
        ("100", "99", False),
        ("0", "0", True),
        ("0", "0%", True),
        ("0", "1", False),
        ("50", "50", True),
        ("50", "50,3", True),
        ("50", "60", False),
    ],
)
def test_check_pourcentage(rep, answer, expected):
    assert check(_scoring_pourcentage(rep), answer) is expected


# ============================================================================
# check() — type texte_court
# ============================================================================


def _scoring_texte(rep: str, **kw):
    return {
        "mode": "python",
        "type_reponse": "texte_court",
        "reponse_canonique": rep,
        **kw,
    }


@pytest.mark.parametrize(
    "rep,answer,expected",
    [
        ("oui", "oui", True),
        ("oui", "Oui", True),
        ("oui", "OUI", True),
        ("oui", "oui!", True),
        ("oui", "non", False),
        ("non", "non", True),
        ("non", "Non", True),
        ("non", "oui", False),
        ("8x", "8x", True),
        ("8x", "8 x", True),
        ("8x", "8 X", True),
        ("8x", "9x", False),
        ("Paris", "paris", True),
        ("Paris", "la ville de paris", True),  # inclusion
        ("Paris", "Lyon", False),
        ("2n", "2n", True),
        ("2n", "2 n", True),
        ("n+1", "n + 1", True),
        ("n+1", "1+n", False),  # ordre compte (pas d'inclusion partielle valide)
    ],
)
def test_check_texte_court(rep, answer, expected):
    assert check(_scoring_texte(rep), answer) is expected


# ============================================================================
# formes_acceptees prioritaire
# ============================================================================


def test_formes_acceptees_prioritaire():
    """Une forme listée dans `formes_acceptees` doit être acceptée même si
    le parsing numérique aurait échoué."""
    scoring = {
        "mode": "python",
        "type_reponse": "entier",
        "reponse_canonique": "5",
        "formes_acceptees": ["x = 5", "x=5"],
    }
    assert check(scoring, "x = 5") is True
    assert check(scoring, "x=5") is True
    assert check(scoring, "5") is True
    assert check(scoring, "6") is False


def test_formes_acceptees_avec_unite():
    scoring = {
        "mode": "python",
        "type_reponse": "entier",
        "reponse_canonique": "4",
        "formes_acceptees": ["4 h", "4h", "4 heures"],
    }
    assert check(scoring, "4 h") is True
    assert check(scoring, "4h") is True
    assert check(scoring, "4 heures") is True
    assert check(scoring, "4") is True


# ============================================================================
# Modes inconnus / mode albert
# ============================================================================


def test_mode_albert_renvoie_false():
    """check() est strictement Python — mode albert dispatch ailleurs."""
    scoring = {
        "mode": "albert",
        "reponse_modele": "n'importe quoi",
        "criteres_validation": [],
    }
    assert check(scoring, "n'importe quoi") is False


def test_mode_inconnu_renvoie_false():
    assert check({"mode": "wat", "type_reponse": "entier", "reponse_canonique": "1"}, "1") is False


def test_scoring_non_dict_renvoie_false():
    assert check("not a dict", "12") is False  # type: ignore[arg-type]
