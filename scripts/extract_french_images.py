"""Extraction offline des illustrations des sujets français compréhension.

Script de développement exécuté une fois par le mainteneur. Parcourt les PDF
des annales DNB français et extrait l'illustration principale de chaque sujet
(photogramme, photographie, affiche, dessin, tableau, gravure) en PNG vers
`content/francais/comprehension/images/<slug>.png`.

Heuristique de sélection quand un PDF contient plusieurs images :
- dédoublonnage par bounding box (le layer PDF peut déposer deux copies du
  même bitmap au même endroit, cf. 2025 Amérique-Sud-Asie) ;
- exclusion du logo DNB récurrent en page 1 (~415 × 253 px, tronc commun
  « Éducation nationale / Session 20xx ») ;
- parmi les candidats restants, on garde celui de plus grande surface.

Le nom de fichier est le `slug` du JSON compagnon (`{annee}_{centre}`), pour
un mapping direct depuis le loader côté app.

Usage :
    .venv/bin/python -m scripts.extract_french_images
    .venv/bin/python -m scripts.extract_french_images --force
    .venv/bin/python -m scripts.extract_french_images --limit 4
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

import pdfplumber

logger = logging.getLogger("extract_french_images")

REPO_ROOT = Path(__file__).resolve().parent.parent
ANNALES_DIR = REPO_ROOT / "content" / "francais" / "comprehension" / "annales"
EXERCISES_DIR = REPO_ROOT / "content" / "francais" / "comprehension" / "exercises"
IMAGES_DIR = REPO_ROOT / "content" / "francais" / "comprehension" / "images"

# Résolution du rendu PIL : 200 DPI = ~2x la taille d'impression, suffisant
# pour une lecture confortable sans exploser le poids des PNG.
RENDER_DPI = 200

# Le logo DNB en page 1 des sujets 2018 Amérique-Nord et Inde fait environ
# 413-415 × 253 points PDF. On tolère ±5 % et on ne le filtre que s'il est
# sur la page 1 : on ne veut pas rejeter par erreur une illustration qui
# aurait des dimensions proches.
LOGO_WIDTH_RANGE = (390, 440)
LOGO_HEIGHT_RANGE = (235, 275)


def _is_dnb_logo(page_index: int, width: float, height: float) -> bool:
    if page_index != 0:
        return False
    return (
        LOGO_WIDTH_RANGE[0] <= width <= LOGO_WIDTH_RANGE[1]
        and LOGO_HEIGHT_RANGE[0] <= height <= LOGO_HEIGHT_RANGE[1]
    )


def _pick_main_image(pdf: pdfplumber.PDF, pdf_name: str) -> tuple[int, dict] | None:
    """Retourne (index_page, objet_image pdfplumber) du meilleur candidat."""
    candidates: list[tuple[int, dict]] = []
    seen_bboxes: set[tuple[int, int, int, int, int]] = set()

    for i, page in enumerate(pdf.pages):
        for img in page.images:
            bbox_key = (
                i,
                round(img["x0"]),
                round(img["top"]),
                round(img["x1"]),
                round(img["bottom"]),
            )
            if bbox_key in seen_bboxes:
                continue
            seen_bboxes.add(bbox_key)

            if _is_dnb_logo(i, img["width"], img["height"]):
                logger.debug("  skip logo DNB p%d (%s)", i + 1, pdf_name)
                continue

            candidates.append((i, img))

    if not candidates:
        return None

    def _area(entry: tuple[int, dict]) -> float:
        _, im = entry
        return float(im["width"]) * float(im["height"])

    candidates.sort(key=_area, reverse=True)
    return candidates[0]


def extract_one(pdf_path: Path, slug: str, out_dir: Path, *, force: bool) -> bool:
    out_path = out_dir / f"{slug}.png"
    if out_path.exists() and not force:
        logger.info("skip %s (déjà extrait)", out_path.name)
        return True

    with pdfplumber.open(pdf_path) as pdf:
        picked = _pick_main_image(pdf, pdf_path.name)
        if picked is None:
            logger.warning("aucune image détectée dans %s", pdf_path.name)
            return False

        page_idx, img = picked
        page = pdf.pages[page_idx]
        # On clamp la bbox aux limites de la page : certains PDF déclarent
        # des coordonnées qui dépassent légèrement (cf. 2022 Polynésie où
        # `top=43` et `bottom=423` sont valides mais proches des bords).
        bbox = (
            max(0, img["x0"]),
            max(0, img["top"]),
            min(page.width, img["x1"]),
            min(page.height, img["bottom"]),
        )
        cropped = page.crop(bbox)
        pil_image = cropped.to_image(resolution=RENDER_DPI).original
        pil_image.save(out_path)
        logger.info(
            "ok %s → %s (%dx%d)",
            pdf_path.name,
            out_path.name,
            pil_image.size[0],
            pil_image.size[1],
        )
        return True


def _iter_targets(limit: int | None) -> list[tuple[Path, str]]:
    """Liste des (pdf_path, slug) à traiter, basée sur les JSON exercices."""
    targets: list[tuple[Path, str]] = []
    for json_path in sorted(EXERCISES_DIR.glob("*.json")):
        if json_path.name == "_all.json":
            continue
        data = json.loads(json_path.read_text(encoding="utf-8"))
        slug = data["id"]
        source_file = data.get("source_file") or f"{json_path.stem}.pdf"
        pdf_path = ANNALES_DIR / source_file
        if not pdf_path.exists():
            logger.warning("PDF manquant pour %s : %s", slug, pdf_path.name)
            continue
        targets.append((pdf_path, slug))
        if limit is not None and len(targets) >= limit:
            break
    return targets


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="réextrait même si le PNG existe")
    parser.add_argument("--limit", type=int, default=None, help="ne traite que les N premiers sujets")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    IMAGES_DIR.mkdir(parents=True, exist_ok=True)

    targets = _iter_targets(args.limit)
    logger.info("%d sujet(s) à traiter", len(targets))

    ok = fail = 0
    for pdf_path, slug in targets:
        try:
            if extract_one(pdf_path, slug, IMAGES_DIR, force=args.force):
                ok += 1
            else:
                fail += 1
        except Exception as e:
            logger.error("erreur sur %s : %s", pdf_path.name, e)
            fail += 1

    logger.info("terminé : %d ok, %d échec(s)", ok, fail)
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
