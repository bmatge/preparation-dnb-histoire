"""Pédagogie de la dictée : diff lettre-à-mot, scoring, feedback.

Le coeur de l'épreuve dictée côté runtime est **déterministe** : on compare
la copie de l'élève au texte de référence (extrait dans le JSON), on relève
les erreurs et on calcule un score. Aucun appel LLM n'est nécessaire pour
cette boucle — Albert pourra être branché en V2 pour expliquer en langage
naturel les fautes non triviales.

## Barème DNB officiel (2018-2025)

- Total de l'épreuve : 10 points.
- 1 point retiré par faute lexicale (orthographe d'usage, accents, mots rares).
- 2 points retirés par faute grammaticale (accord, conjugaison, homophones,
  ponctuation, majuscule).
- Plancher : 0 (pas de note négative).

## Choix MVP : pas de classification automatique lexicale/grammaticale

Distinguer une faute lexicale d'une faute grammaticale demande un jugement
contextuel qu'on ne peut pas faire en pur Python sans LLM. En MVP on
applique un barème simplifié : **chaque faute = 1 point retiré, plafonné à
10**. C'est plus indulgent que le barème officiel mais ça reste pédagogique
et la note finale est calibrée par le plafond. La classification précise
arrivera quand on branchera Albert sur ce parcours.

## Tokenisation

On découpe les deux textes (référence et copie) en tokens via un regex
qui sépare mots, ponctuation et espaces. Le diff opère sur la séquence
de tokens. Chaque token-mot qui diffère compte pour 1 faute. Les
divergences sur la ponctuation et les majuscules sont aussi comptées
mais pas plafonnées différemment.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Literal

# Un token est soit un mot (\w+), soit un caractère/séquence non-mot (incluant
# ponctuation et espaces). On garde tout dans la sortie pour pouvoir
# reconstituer un texte affichable avec coloration des erreurs.
_TOKEN_RE = re.compile(r"\w+|[^\w\s]+|\s+", re.UNICODE)


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text)


def _is_word(token: str) -> bool:
    """Vrai si le token est un mot (au moins un caractère alphanumérique)."""
    return bool(token) and any(c.isalnum() for c in token)


def _normalize_for_compare(token: str) -> str:
    """Normalisation pour la comparaison : casse + accents.

    On garde la version originale pour l'affichage, mais on compare en
    case-insensitive et accent-insensitive ? Non — pour une dictée on est
    strict sur les accents et la casse, c'est l'objet de l'épreuve. Cette
    fonction sert juste à normaliser les apostrophes et guillemets pour ne
    pas pénaliser un élève qui aurait tapé une apostrophe droite à la place
    d'une apostrophe typographique.
    """
    # Normalise les apostrophes (droite, courbe, demi-cadratin) → '
    t = token.replace("\u2019", "'").replace("\u02bc", "'")
    # Normalise les guillemets français/anglais
    t = t.replace("«", '"').replace("»", '"').replace("\u201c", '"').replace("\u201d", '"')
    # Normalise les tirets variés
    t = t.replace("\u2013", "-").replace("\u2014", "-")
    return t


# ============================================================================
# Résultat structuré
# ============================================================================


TokenStatus = Literal["ok", "wrong", "missing", "extra"]


@dataclass
class TokenDiff:
    """Une portion de texte alignée entre la copie élève et la référence.

    - `ok`      : identique (token affiché tel quel)
    - `wrong`   : l'élève a écrit quelque chose à la place du mot attendu
    - `missing` : l'élève a oublié un mot présent dans la référence
    - `extra`   : l'élève a ajouté un mot absent de la référence
    """

    status: TokenStatus
    expected: str  # texte attendu (vide si extra)
    got: str  # texte saisi par l'élève (vide si missing)


@dataclass
class DicteeResult:
    """Résultat complet d'une évaluation de dictée."""

    diff: list[TokenDiff]
    nb_fautes: int
    points_perdus: int
    note_sur_10: int

    @property
    def parfait(self) -> bool:
        return self.nb_fautes == 0


# ============================================================================
# Coeur du diff
# ============================================================================


def evaluate(reference: str, eleve: str) -> DicteeResult:
    """Évalue la copie d'un élève par rapport au texte de référence.

    Renvoie un `DicteeResult` qui contient le diff token par token et les
    métriques de scoring. La fonction est pure (pas d'effet de bord), donc
    facile à tester unitairement.
    """
    ref_tokens = _tokenize(reference)
    elv_tokens = _tokenize(eleve)

    # Pour le matching, on travaille sur des tokens normalisés ; pour
    # l'affichage on garde les originaux. Les deux listes ont la même
    # longueur que leurs équivalents non normalisés.
    ref_norm = [_normalize_for_compare(t) for t in ref_tokens]
    elv_norm = [_normalize_for_compare(t) for t in elv_tokens]

    matcher = SequenceMatcher(a=ref_norm, b=elv_norm, autojunk=False)
    diff: list[TokenDiff] = []
    nb_fautes = 0

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for k in range(i1, i2):
                diff.append(TokenDiff("ok", ref_tokens[k], ref_tokens[k]))
            continue

        if tag == "replace":
            # Aligne mot à mot dans le bloc remplacé. La longueur des deux
            # côtés peut différer ; on couple ce qui peut l'être et on
            # marque le surplus comme missing/extra.
            ref_block = list(range(i1, i2))
            elv_block = list(range(j1, j2))
            common = min(len(ref_block), len(elv_block))
            for k in range(common):
                ri = ref_block[k]
                ej = elv_block[k]
                diff.append(TokenDiff("wrong", ref_tokens[ri], elv_tokens[ej]))
                if _is_word(ref_tokens[ri]) or _is_word(elv_tokens[ej]):
                    nb_fautes += 1
            for k in range(common, len(ref_block)):
                ri = ref_block[k]
                diff.append(TokenDiff("missing", ref_tokens[ri], ""))
                if _is_word(ref_tokens[ri]):
                    nb_fautes += 1
            for k in range(common, len(elv_block)):
                ej = elv_block[k]
                diff.append(TokenDiff("extra", "", elv_tokens[ej]))
                if _is_word(elv_tokens[ej]):
                    nb_fautes += 1
            continue

        if tag == "delete":
            for k in range(i1, i2):
                diff.append(TokenDiff("missing", ref_tokens[k], ""))
                if _is_word(ref_tokens[k]):
                    nb_fautes += 1
            continue

        if tag == "insert":
            for k in range(j1, j2):
                diff.append(TokenDiff("extra", "", elv_tokens[k]))
                if _is_word(elv_tokens[k]):
                    nb_fautes += 1
            continue

    points_perdus = min(nb_fautes, 10)
    note_sur_10 = 10 - points_perdus
    return DicteeResult(
        diff=diff,
        nb_fautes=nb_fautes,
        points_perdus=points_perdus,
        note_sur_10=note_sur_10,
    )


__all__ = [
    "TokenDiff",
    "DicteeResult",
    "evaluate",
]
