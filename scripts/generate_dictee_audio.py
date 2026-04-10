"""Synthèse vocale offline des dictées DNB français via Coqui XTTS v2.

Script offline, exécuté par le mainteneur sur sa machine de développement.
Pour chaque dictée extraite par `scripts.extract_dictees`, on génère un MP3
par phrase et par voix vers :

    content/francais/dictee/audio/<voix_slug>/<dictee_slug>/phrase_NN.mp3

Les fichiers MP3 sont committés dans le repo et servis comme statiques par
le runtime — le VPS n'exécute jamais de TTS. La règle souveraineté
(`CLAUDE.md` §1) interdit les appels externes en runtime, pas en extraction
offline ; ce script est donc analogue à `extract_dictees.py` côté
positionnement.

## Voix retenues

Deux voix XTTS v2, validées en spike pour la qualité française :
- **Damien Black** (masculine) — voix posée, narration audiobook
- **Tammie Ema** (féminine) — voix mature, posée

## Vitesse

`speed=0.90`. Légèrement ralenti par rapport au défaut (1.0) pour laisser
le temps à un élève de 3e d'écrire au rythme normal d'une dictée scolaire.

## Dépendances dev (PAS dans requirements.txt prod)

- `coqui-tts[codec]` (~3 Go avec PyTorch, modèle XTTS v2 ~1,8 Go au 1er run)
- `ffmpeg` (binaire système, déjà présent sur la plupart des Mac via Homebrew)

## Licence

XTTS v2 utilise la Coqui Public Model License (CPML), non-commerciale.
revise-ton-dnb est éducatif gratuit, donc compatible.

## Usage

    .venv/bin/python -m scripts.generate_dictee_audio
    .venv/bin/python -m scripts.generate_dictee_audio --limit 1
    .venv/bin/python -m scripts.generate_dictee_audio --voice damien
    .venv/bin/python -m scripts.generate_dictee_audio --force
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
EXERCISES_DIR = REPO_ROOT / "content" / "francais" / "dictee" / "exercises"
AUDIO_DIR = REPO_ROOT / "content" / "francais" / "dictee" / "audio"

DEFAULT_SPEED = 0.90

# Encodage MP3 : 32 kbps mono est largement suffisant pour de la voix lente,
# le débit perçu est correct et le poids reste sous 50 Ko/phrase.
MP3_BITRATE = "32k"


@dataclass(frozen=True)
class VoiceSpec:
    slug: str
    xtts_speaker: str
    label: str


VOICES: dict[str, VoiceSpec] = {
    "damien": VoiceSpec(
        slug="damien",
        xtts_speaker="Damien Black",
        label="Damien (voix masculine)",
    ),
    "tammie": VoiceSpec(
        slug="tammie",
        xtts_speaker="Tammie Ema",
        label="Tammie (voix féminine)",
    ),
}


def _ensure_ffmpeg() -> None:
    if shutil.which("ffmpeg") is None:
        sys.exit(
            "ffmpeg introuvable. Installe-le (`brew install ffmpeg` sur macOS) "
            "puis relance."
        )


def _wav_to_mp3(wav_path: Path, mp3_path: Path) -> None:
    """Convertit un WAV stéréo XTTS en MP3 mono 32 kbps via ffmpeg."""
    mp3_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-y",
        "-loglevel",
        "error",
        "-i",
        str(wav_path),
        "-ac",
        "1",
        "-b:a",
        MP3_BITRATE,
        str(mp3_path),
    ]
    subprocess.run(cmd, check=True)


def _synthesize_one(
    tts,
    text: str,
    voice: VoiceSpec,
    speed: float,
    out_mp3: Path,
) -> None:
    with tempfile.TemporaryDirectory() as td:
        wav_path = Path(td) / "out.wav"
        tts.tts_to_file(
            text=text,
            speaker=voice.xtts_speaker,
            language="fr",
            file_path=str(wav_path),
            speed=speed,
            split_sentences=False,
        )
        _wav_to_mp3(wav_path, out_mp3)


def _process_dictee(
    tts,
    json_path: Path,
    voices: list[VoiceSpec],
    speed: float,
    *,
    force: bool,
) -> tuple[int, int]:
    data = json.loads(json_path.read_text(encoding="utf-8"))
    slug = data["id"]
    phrases = data["phrases"]
    n_done = 0
    n_skip = 0

    for voice in voices:
        out_dir = AUDIO_DIR / voice.slug / slug
        out_dir.mkdir(parents=True, exist_ok=True)
        for phrase in phrases:
            order = phrase["ordre"]
            text = phrase["texte"]
            out_mp3 = out_dir / f"phrase_{order:02d}.mp3"

            if out_mp3.exists() and not force:
                n_skip += 1
                continue

            t0 = time.time()
            try:
                _synthesize_one(tts, text, voice, speed, out_mp3)
            except Exception as e:
                logger.error("  echec %s/%s phrase %d : %s", voice.slug, slug, order, e)
                continue
            dt = time.time() - t0
            logger.info(
                "  %s/%s phrase_%02d.mp3 (%.1fs, %d chars)",
                voice.slug,
                slug,
                order,
                dt,
                len(text),
            )
            n_done += 1

    return n_done, n_skip


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="re-genere meme si le MP3 existe")
    parser.add_argument("--limit", type=int, default=None, help="ne traite que les N premieres dictees")
    parser.add_argument(
        "--voice",
        choices=list(VOICES.keys()) + ["all"],
        default="all",
        help="ne genere qu'une seule voix",
    )
    parser.add_argument("--speed", type=float, default=DEFAULT_SPEED)
    args = parser.parse_args()

    _ensure_ffmpeg()

    # Import paresseux : XTTS pèse plusieurs Go, on ne le charge qu'au moment
    # où on en a effectivement besoin.
    os.environ["COQUI_TOS_AGREED"] = "1"
    try:
        from TTS.api import TTS
    except ImportError as e:
        sys.exit(
            f"Coqui TTS introuvable ({e}). Installe-le avec :\n"
            "  .venv/bin/pip install 'coqui-tts[codec]' torch torchaudio "
            "'transformers<5'"
        )

    if not EXERCISES_DIR.exists():
        sys.exit(f"Repertoire dictees introuvable : {EXERCISES_DIR}")

    json_files = sorted(p for p in EXERCISES_DIR.glob("*.json") if p.name != "_all.json")
    if args.limit is not None:
        json_files = json_files[: args.limit]
    if not json_files:
        sys.exit("Aucune dictee a traiter")

    voices = list(VOICES.values()) if args.voice == "all" else [VOICES[args.voice]]
    logger.info("%d dictee(s), %d voix, vitesse %.2f", len(json_files), len(voices), args.speed)

    logger.info("chargement de XTTS v2 (~3 min au premier run)...")
    t_load = time.time()
    tts = TTS("tts_models/multilingual/multi-dataset/xtts_v2")
    logger.info("modele charge en %.1fs", time.time() - t_load)

    AUDIO_DIR.mkdir(parents=True, exist_ok=True)

    total_done = 0
    total_skip = 0
    for json_path in json_files:
        logger.info("\n[%s]", json_path.stem)
        n_done, n_skip = _process_dictee(
            tts, json_path, voices, args.speed, force=args.force
        )
        total_done += n_done
        total_skip += n_skip

    logger.info("\n" + "=" * 60)
    logger.info("Termine : %d generes, %d ignores (deja presents)", total_done, total_skip)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
