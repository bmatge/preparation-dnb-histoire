"""Convertit les pages d'un PDF de sujet sciences en PNG pour la simulation.

Usage :
    .venv/bin/python -m scripts.capture_sciences_sujets \\
        content/sciences/annales/scb_sujet-2025-sciences-metropole.pdf \\
        --slug 2025_metropole \\
        --disc1-pages 1-3 \\
        --disc2-pages 4-6

Le script cree les PNG dans ``content/sciences/simulation/captures/{slug}/``
avec le nommage ``d{N}_page_{P}.png`` (N = 1 ou 2, P = numero de page dans
la discipline). Les pages sont en 200 DPI par defaut.

Prerequis : Poppler installe (pdftoppm) + pdf2image.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from pdf2image import convert_from_path

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
CAPTURES_DIR = REPO_ROOT / "content" / "sciences" / "simulation" / "captures"

DEFAULT_DPI = 200


def parse_page_range(spec: str) -> list[int]:
    """Parse une spec de pages : '1-3' -> [1, 2, 3], '2,4' -> [2, 4]."""
    pages: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            pages.extend(range(int(a), int(b) + 1))
        else:
            pages.append(int(part))
    return pages


def capture_discipline(
    pdf_path: Path,
    slug: str,
    disc_number: int,
    page_numbers: list[int],
    dpi: int,
) -> list[str]:
    """Convertit les pages d'une discipline en PNG. Retourne les chemins relatifs."""
    out_dir = CAPTURES_DIR / slug
    out_dir.mkdir(parents=True, exist_ok=True)

    filenames: list[str] = []
    for local_idx, page_num in enumerate(page_numbers, start=1):
        images = convert_from_path(
            str(pdf_path),
            first_page=page_num,
            last_page=page_num,
            dpi=dpi,
        )
        if not images:
            logger.warning("Page %d non trouvee dans %s", page_num, pdf_path.name)
            continue

        fname = f"d{disc_number}_page_{local_idx}.png"
        out_path = out_dir / fname
        images[0].save(str(out_path), "PNG")
        logger.info("  %s (page PDF %d)", fname, page_num)
        filenames.append(f"{slug}/{fname}")

    return filenames


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convertit un PDF sujet sciences en captures PNG."
    )
    parser.add_argument("pdf", type=Path, help="Chemin du PDF annale")
    parser.add_argument("--slug", required=True, help="Identifiant du sujet (ex: 2025_metropole)")
    parser.add_argument("--disc1-pages", required=True, help="Pages de la discipline 1 (ex: 1-3)")
    parser.add_argument("--disc2-pages", required=True, help="Pages de la discipline 2 (ex: 4-6)")
    parser.add_argument("--dpi", type=int, default=DEFAULT_DPI, help=f"Resolution (defaut: {DEFAULT_DPI})")
    args = parser.parse_args()

    if not args.pdf.exists():
        logger.error("PDF introuvable : %s", args.pdf)
        sys.exit(1)

    logger.info("Conversion de %s (slug=%s, dpi=%d)", args.pdf.name, args.slug, args.dpi)

    d1_pages = parse_page_range(args.disc1_pages)
    d2_pages = parse_page_range(args.disc2_pages)

    logger.info("Discipline 1 : pages %s", d1_pages)
    d1_files = capture_discipline(args.pdf, args.slug, 1, d1_pages, args.dpi)

    logger.info("Discipline 2 : pages %s", d2_pages)
    d2_files = capture_discipline(args.pdf, args.slug, 2, d2_pages, args.dpi)

    logger.info("Fait. %d + %d captures dans %s/%s/",
                len(d1_files), len(d2_files), CAPTURES_DIR, args.slug)
    logger.info("Fichiers discipline 1 : %s", d1_files)
    logger.info("Fichiers discipline 2 : %s", d2_files)


if __name__ == "__main__":
    main()
