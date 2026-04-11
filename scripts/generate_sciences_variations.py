"""Génère des variations numériques de questions de révision sciences.

Vague 2 : on prend les questions « à formule » de la Physique-Chimie
(vitesse, poids, énergie cinétique/potentielle, U=RI, P=UI, masse
volumique, etc.) et on produit plusieurs variantes avec des valeurs
numériques différentes mais cohérentes. Pas d'appel API, 100 % stdlib
+ random. Le seed est fixé par défaut pour que deux runs successifs
produisent exactement les mêmes variations (reproductibilité).

Sortie : un fichier par thème dans
`content/sciences/revision/questions/*_variations.json` à côté des
seeds. Le loader `init_sciences_revision` les charge automatiquement
car le nom ne commence pas par `_`.

Usage :
    .venv/bin/python -m scripts.generate_sciences_variations
    .venv/bin/python -m scripts.generate_sciences_variations --dry-run
    .venv/bin/python -m scripts.generate_sciences_variations --seed 42

Pour ajouter une nouvelle question seed à varier : créer un nouveau
générateur dans ce fichier et l'ajouter à la liste `GENERATORS` en bas.
Chaque générateur retourne une liste de dicts qui matchent le schéma
Pydantic `SciencesQuestion` (cf. app/sciences/revision/models.py).
"""

from __future__ import annotations

import argparse
import json
import logging
import random
from collections import defaultdict
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
QUESTIONS_DIR = REPO_ROOT / "content" / "sciences" / "revision" / "questions"


# ============================================================================
# Helpers
# ============================================================================


def _fmt(v: float) -> str:
    """Formate un nombre : entier sans décimal, décimal avec ≤ 4 décimales,
    zéros de traîne supprimés."""
    if v == int(v):
        return str(int(v))
    s = f"{v:.4f}".rstrip("0").rstrip(".")
    return s


def _question(
    *,
    qid: str,
    theme: str,
    competence: str,
    enonce: str,
    reponse_canonique: str,
    type_reponse: str = "decimal",
    unite: str | None = None,
    reveal: str | None = None,
    tolerances: dict | None = None,
    formes_acceptees: list[str] | None = None,
    document: str = "fiche_sciences_02_mouvements_energie",
) -> dict:
    """Produit un dict de question au format `SciencesQuestion`."""
    scoring = {
        "mode": "python",
        "type_reponse": type_reponse,
        "reponse_canonique": reponse_canonique,
    }
    if unite is not None:
        scoring["unite"] = unite
    if tolerances:
        scoring["tolerances"] = tolerances
    if formes_acceptees:
        scoring["formes_acceptees"] = formes_acceptees
    q: dict = {
        "id": qid,
        "discipline": "physique_chimie",
        "theme": theme,
        "competence": competence,
        "source": {"type": "variation_generee", "document": document},
        "enonce": enonce,
        "scoring": scoring,
    }
    if reveal:
        q["reveal_explication"] = reveal
    return q


# ============================================================================
# Générateurs — un par formule / notion
# ============================================================================


def gen_vitesse(rng: random.Random) -> list[dict]:
    """v = d / t"""
    combos = [
        (60, 2, "cycliste", "km", "h"),
        (120, 3, "TGV", "km", "h"),
        (90, 3, "automobiliste", "km", "h"),
        (50, 5, "randonneur", "km", "h"),
        (100, 2, "cycliste lors d'une étape", "km", "h"),
        (200, 4, "conducteur de train", "km", "h"),
        (45, 3, "coureur à pied", "km", "h"),
    ]
    out = []
    for i, (d, t, sujet, u_d, u_t) in enumerate(combos, start=1):
        v = d / t
        out.append(
            _question(
                qid=f"pc_me_01_v{i}",
                theme="mouvements_energie",
                competence="Calcul de vitesse (v = d/t)",
                enonce=(
                    f"Un {sujet} parcourt {_fmt(d)} {u_d} en {_fmt(t)} {u_t}. "
                    f"Quelle est sa vitesse moyenne en {u_d}/{u_t} ?"
                ),
                reponse_canonique=_fmt(v),
                unite=f"{u_d}/{u_t}",
                reveal=f"v = d / t = {_fmt(d)} / {_fmt(t)} = {_fmt(v)} {u_d}/{u_t}.",
            )
        )
    return out


def gen_conversion_kmh_ms(rng: random.Random) -> list[dict]:
    """Conversion km/h → m/s (diviser par 3.6)."""
    values = [36, 54, 90, 108, 18, 126]
    out = []
    for i, v_kmh in enumerate(values, start=1):
        v_ms = v_kmh / 3.6
        out.append(
            _question(
                qid=f"pc_me_02_v{i}",
                theme="mouvements_energie",
                competence="Conversion km/h → m/s",
                enonce=f"Convertis {_fmt(v_kmh)} km/h en m/s.",
                reponse_canonique=_fmt(round(v_ms, 2)),
                unite="m/s",
                tolerances={"abs": 0.1},
                reveal=(
                    f"Pour passer de km/h à m/s, on divise par 3,6 : "
                    f"{_fmt(v_kmh)} / 3,6 ≈ {_fmt(round(v_ms, 2))} m/s."
                ),
            )
        )
    return out


def gen_poids(rng: random.Random) -> list[dict]:
    """P = m × g, avec g = 10 N/kg."""
    masses = [2, 10, 25, 0.5, 75, 100]
    out = []
    for i, m in enumerate(masses, start=1):
        p = m * 10
        out.append(
            _question(
                qid=f"pc_me_06_v{i}",
                theme="mouvements_energie",
                competence="Calcul du poids P = m × g",
                enonce=(
                    f"Quel est le poids (en newtons) d'un objet de masse "
                    f"{_fmt(m)} kg sur Terre, avec g = 10 N/kg ?"
                ),
                reponse_canonique=_fmt(p),
                unite="N",
                reveal=f"P = m × g = {_fmt(m)} × 10 = {_fmt(p)} N.",
            )
        )
    return out


def gen_energie_cinetique(rng: random.Random) -> list[dict]:
    """Ec = ½ × m × v²"""
    combos = [
        (4, 2),
        (1, 10),
        (10, 5),
        (0.5, 4),
        (2, 6),
        (5, 2),
    ]
    out = []
    for i, (m, v) in enumerate(combos, start=1):
        ec = 0.5 * m * v * v
        out.append(
            _question(
                qid=f"pc_me_09_v{i}",
                theme="mouvements_energie",
                competence="Énergie cinétique Ec = ½mv²",
                enonce=(
                    f"Un objet de masse {_fmt(m)} kg se déplace à {_fmt(v)} m/s. "
                    f"Quelle est son énergie cinétique en joules ?"
                ),
                reponse_canonique=_fmt(ec),
                unite="J",
                reveal=(
                    f"Ec = ½ × m × v² = 0,5 × {_fmt(m)} × {_fmt(v)}² = {_fmt(ec)} J."
                ),
            )
        )
    return out


def gen_energie_potentielle(rng: random.Random) -> list[dict]:
    """Ep = m × g × h, g = 10 N/kg."""
    combos = [
        (1, 2),
        (0.5, 10),
        (2, 5),
        (0.1, 20),
        (3, 4),
        (5, 3),
    ]
    out = []
    for i, (m, h) in enumerate(combos, start=1):
        ep = m * 10 * h
        out.append(
            _question(
                qid=f"pc_me_10_v{i}",
                theme="mouvements_energie",
                competence="Énergie potentielle de position",
                enonce=(
                    f"Un objet de masse {_fmt(m)} kg est suspendu à {_fmt(h)} m "
                    f"du sol (g = 10 N/kg). Quelle est son énergie potentielle en joules ?"
                ),
                reponse_canonique=_fmt(ep),
                unite="J",
                reveal=(
                    f"Ep = m × g × h = {_fmt(m)} × 10 × {_fmt(h)} = {_fmt(ep)} J."
                ),
            )
        )
    return out


def gen_puissance_electrique(rng: random.Random) -> list[dict]:
    """P = U × I"""
    combos = [
        (12, 2),
        (6, 0.5),
        (230, 1),
        (24, 0.25),
        (110, 2),
        (9, 0.1),
    ]
    out = []
    for i, (u, ic) in enumerate(combos, start=1):
        p = u * ic
        out.append(
            _question(
                qid=f"pc_me_13_v{i}",
                theme="mouvements_energie",
                competence="Puissance électrique P = U × I",
                enonce=(
                    f"Un appareil fonctionne sous {_fmt(u)} V et est traversé "
                    f"par un courant de {_fmt(ic)} A. Quelle est sa puissance en watts ?"
                ),
                reponse_canonique=_fmt(p),
                unite="W",
                reveal=f"P = U × I = {_fmt(u)} × {_fmt(ic)} = {_fmt(p)} W.",
            )
        )
    return out


def gen_energie_electrique(rng: random.Random) -> list[dict]:
    """E = P × t (en kWh)"""
    combos = [
        (0.5, 3),
        (1, 4),
        (2, 1),
        (0.1, 10),
        (0.2, 5),
        (0.06, 10),
    ]
    out = []
    for i, (p_kw, t_h) in enumerate(combos, start=1):
        e = p_kw * t_h
        out.append(
            _question(
                qid=f"pc_me_14_v{i}",
                theme="mouvements_energie",
                competence="Énergie électrique E = P × t",
                enonce=(
                    f"Un appareil de {_fmt(p_kw)} kW fonctionne pendant "
                    f"{_fmt(t_h)} h. Quelle énergie consomme-t-il en kWh ?"
                ),
                reponse_canonique=_fmt(e),
                unite="kWh",
                reveal=f"E = P × t = {_fmt(p_kw)} × {_fmt(t_h)} = {_fmt(e)} kWh.",
            )
        )
    return out


def gen_loi_ohm_u(rng: random.Random) -> list[dict]:
    """U = R × I (calcul de U)"""
    combos = [
        (50, 0.4),
        (100, 0.1),
        (220, 0.05),
        (10, 2),
        (5, 1),
        (75, 0.2),
    ]
    out = []
    for i, (r, ic) in enumerate(combos, start=1):
        u = r * ic
        out.append(
            _question(
                qid=f"pc_es_01_v{i}",
                theme="electricite_signaux",
                competence="Loi d'Ohm — calcul de U",
                enonce=(
                    f"Une résistance de {_fmt(r)} Ω est traversée par un "
                    f"courant de {_fmt(ic)} A. Quelle est la tension à ses "
                    f"bornes en volts ?"
                ),
                reponse_canonique=_fmt(u),
                unite="V",
                reveal=f"U = R × I = {_fmt(r)} × {_fmt(ic)} = {_fmt(u)} V.",
                document="fiche_sciences_03_electricite_signaux",
            )
        )
    return out


def gen_loi_ohm_i(rng: random.Random) -> list[dict]:
    """I = U / R (calcul de I)"""
    combos = [
        (12, 6),
        (24, 8),
        (10, 100),
        (230, 460),
        (4.5, 9),
        (5, 50),
    ]
    out = []
    for i, (u, r) in enumerate(combos, start=1):
        ic = u / r
        out.append(
            _question(
                qid=f"pc_es_02_v{i}",
                theme="electricite_signaux",
                competence="Loi d'Ohm — calcul de I",
                enonce=(
                    f"Un dipôle de résistance {_fmt(r)} Ω est branché sous "
                    f"{_fmt(u)} V. Quelle est l'intensité du courant en "
                    f"ampères ?"
                ),
                reponse_canonique=_fmt(round(ic, 4)),
                unite="A",
                tolerances={"abs": 0.005},
                reveal=(
                    f"I = U / R = {_fmt(u)} / {_fmt(r)} ≈ "
                    f"{_fmt(round(ic, 4))} A."
                ),
                document="fiche_sciences_03_electricite_signaux",
            )
        )
    return out


def gen_masse_volumique(rng: random.Random) -> list[dict]:
    """ρ = m / V"""
    materials = [
        ("aluminium", 27, 10, "2.7"),
        ("fer", 79, 10, "7.9"),
        ("cuivre", 89, 10, "8.9"),
        ("or", 193, 10, "19.3"),
        ("eau", 50, 50, "1"),
        ("plomb", 113, 10, "11.3"),
    ]
    out = []
    for i, (nom, m, v, rho) in enumerate(materials, start=1):
        out.append(
            _question(
                qid=f"pc_om_13_v{i}",
                theme="organisation_matiere",
                competence="Calcul de masse volumique ρ = m/V",
                enonce=(
                    f"Un échantillon a une masse de {_fmt(m)} g et un volume "
                    f"de {_fmt(v)} cm³. Quelle est sa masse volumique en g/cm³ ?"
                ),
                reponse_canonique=rho,
                unite="g/cm³",
                reveal=(
                    f"ρ = m / V = {_fmt(m)} / {_fmt(v)} = {rho} g/cm³ "
                    f"— c'est la masse volumique du {nom}."
                ),
                document="fiche_sciences_01_organisation_matiere",
            )
        )
    return out


# ============================================================================
# Table des générateurs
# ============================================================================


GENERATORS: list[Callable[[random.Random], list[dict]]] = [
    gen_vitesse,
    gen_conversion_kmh_ms,
    gen_poids,
    gen_energie_cinetique,
    gen_energie_potentielle,
    gen_puissance_electrique,
    gen_energie_electrique,
    gen_loi_ohm_u,
    gen_loi_ohm_i,
    gen_masse_volumique,
]


# ============================================================================
# Main
# ============================================================================


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--seed", type=int, default=2026,
        help="Seed pour `random` (défaut : 2026, reproductible).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="N'écrit aucun fichier, affiche juste les comptages.",
    )
    args = parser.parse_args()

    rng = random.Random(args.seed)

    # Génère toutes les variations et regroupe par thème.
    by_theme: dict[str, list[dict]] = defaultdict(list)
    total = 0
    for gen in GENERATORS:
        questions = gen(rng)
        for q in questions:
            by_theme[q["theme"]].append(q)
            total += 1

    logger.info("Seed : %d", args.seed)
    logger.info("Générateurs : %d", len(GENERATORS))
    logger.info("Questions générées : %d", total)
    for theme, qs in sorted(by_theme.items()):
        logger.info("  %s : %d variations", theme, len(qs))

    if args.dry_run:
        logger.info("(dry-run : aucun fichier écrit)")
        return 0

    QUESTIONS_DIR.mkdir(parents=True, exist_ok=True)
    for theme, qs in sorted(by_theme.items()):
        out_path = QUESTIONS_DIR / f"physique_chimie_{theme}_variations.json"
        out_path.write_text(
            json.dumps({"questions": qs}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        logger.info("Écrit : %s (%d questions)", out_path.relative_to(REPO_ROOT), len(qs))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
