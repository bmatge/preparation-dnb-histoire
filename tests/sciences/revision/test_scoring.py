"""Tests du scoring déterministe Python pour la révision sciences.

Logique pure, pas de DB ni d'Albert. Couvre chacun des 6 types :
entier, decimal, pourcentage, texte_court, qcm, vrai_faux.
"""

from __future__ import annotations

from app.sciences.revision import scoring as science_scoring


# ============================================================================
# Entier
# ============================================================================


class TestEntier:
    def _sc(self, rep: str, **extra):
        return {"mode": "python", "type_reponse": "entier", "reponse_canonique": rep, **extra}

    def test_match_strict(self):
        assert science_scoring.check(self._sc("46"), "46") is True

    def test_virgule_fr_acceptee_si_partie_entiere(self):
        assert science_scoring.check(self._sc("46"), "46,0") is True

    def test_espace_milliers(self):
        assert science_scoring.check(self._sc("300000"), "300 000") is True

    def test_mauvaise_valeur(self):
        assert science_scoring.check(self._sc("46"), "47") is False

    def test_vide(self):
        assert science_scoring.check(self._sc("46"), "") is False

    def test_non_numerique(self):
        assert science_scoring.check(self._sc("46"), "quarante-six") is False


# ============================================================================
# Décimal
# ============================================================================


class TestDecimal:
    def _sc(self, rep: str, **extra):
        return {"mode": "python", "type_reponse": "decimal", "reponse_canonique": rep, **extra}

    def test_match_strict(self):
        assert science_scoring.check(self._sc("2.7"), "2.7") is True

    def test_virgule_fr(self):
        assert science_scoring.check(self._sc("2.7"), "2,7") is True

    def test_tolerance_par_defaut(self):
        # Tolérance par défaut = 0.01
        assert science_scoring.check(self._sc("2.7"), "2.71") is True

    def test_hors_tolerance(self):
        assert science_scoring.check(self._sc("2.7"), "3.0") is False

    def test_tolerance_personnalisee(self):
        sc = self._sc("13.8", tolerances={"abs": 0.3})
        assert science_scoring.check(sc, "14.0") is True
        assert science_scoring.check(sc, "14.5") is False

    def test_notation_scientifique(self):
        assert science_scoring.check(self._sc("300000"), "3e5") is True


# ============================================================================
# Pourcentage
# ============================================================================


class TestPourcentage:
    def _sc(self, rep: str, **extra):
        return {"mode": "python", "type_reponse": "pourcentage", "reponse_canonique": rep, **extra}

    def test_avec_signe(self):
        assert science_scoring.check(self._sc("78"), "78 %") is True

    def test_sans_signe(self):
        assert science_scoring.check(self._sc("78"), "78") is True

    def test_tolerance(self):
        # Tolérance par défaut 0.5
        assert science_scoring.check(self._sc("78"), "78.3") is True
        assert science_scoring.check(self._sc("78"), "79") is False

    def test_tolerance_personnalisee(self):
        sc = self._sc("78", tolerances={"abs": 1})
        assert science_scoring.check(sc, "79") is True


# ============================================================================
# Texte court
# ============================================================================


class TestTexteCourt:
    def _sc(self, rep: str, **extra):
        return {"mode": "python", "type_reponse": "texte_court", "reponse_canonique": rep, **extra}

    def test_strict(self):
        assert science_scoring.check(self._sc("noyau"), "noyau") is True

    def test_casse_ignoree(self):
        assert science_scoring.check(self._sc("Noyau"), "noyau") is True

    def test_accents_ignores(self):
        assert science_scoring.check(self._sc("hérédité"), "heredite") is True

    def test_article_ignore(self):
        # "le noyau" contient "noyau" → accepté
        assert science_scoring.check(self._sc("noyau"), "le noyau") is True

    def test_formes_acceptees(self):
        sc = self._sc("dioxygène", formes_acceptees=["O2", "le dioxygene"])
        assert science_scoring.check(sc, "O2") is True
        assert science_scoring.check(sc, "le dioxygène") is True

    def test_mauvaise_reponse(self):
        assert science_scoring.check(self._sc("noyau"), "membrane") is False


# ============================================================================
# QCM
# ============================================================================


class TestQCM:
    def _sc(self, rep: str, **extra):
        return {"mode": "python", "type_reponse": "qcm", "reponse_canonique": rep, **extra}

    def test_lettre_majuscule(self):
        assert science_scoring.check(self._sc("C"), "C") is True

    def test_lettre_minuscule_acceptee(self):
        assert science_scoring.check(self._sc("C"), "c") is True

    def test_numero_proposition(self):
        assert science_scoring.check(self._sc("P2"), "p2") is True

    def test_mauvaise_proposition(self):
        assert science_scoring.check(self._sc("P2"), "P3") is False

    def test_espaces(self):
        assert science_scoring.check(self._sc("P2"), " P2 ") is True


# ============================================================================
# Vrai / faux
# ============================================================================


class TestVraiFaux:
    def _sc(self, rep: str):
        return {"mode": "python", "type_reponse": "vrai_faux", "reponse_canonique": rep}

    def test_vrai_strict(self):
        assert science_scoring.check(self._sc("vrai"), "vrai") is True

    def test_vrai_synonymes(self):
        for syn in ("oui", "V", "true", "1", "Juste"):
            assert science_scoring.check(self._sc("vrai"), syn) is True, syn

    def test_faux_strict(self):
        assert science_scoring.check(self._sc("faux"), "faux") is True

    def test_faux_synonymes(self):
        for syn in ("non", "F", "false", "0", "incorrect"):
            assert science_scoring.check(self._sc("faux"), syn) is True, syn

    def test_mismatch(self):
        assert science_scoring.check(self._sc("vrai"), "faux") is False

    def test_reponse_non_reconnue(self):
        assert science_scoring.check(self._sc("vrai"), "peut-être") is False


# ============================================================================
# Mode non supporté → False
# ============================================================================


def test_mode_albert_renvoie_false():
    sc = {"mode": "albert", "reponse_modele": "x"}
    assert science_scoring.check(sc, "x") is False


def test_type_inconnu_renvoie_false():
    sc = {"mode": "python", "type_reponse": "truc_bizarre", "reponse_canonique": "x"}
    assert science_scoring.check(sc, "x") is False


def test_scoring_none():
    assert science_scoring.check(None, "x") is False  # type: ignore[arg-type]


def test_answer_vide():
    sc = {"mode": "python", "type_reponse": "texte_court", "reponse_canonique": "x"}
    assert science_scoring.check(sc, "") is False
