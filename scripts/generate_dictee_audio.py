"""Synthese vocale offline des dictees DNB francais via Mistral Voxtral TTS.

Script offline, execute par le mainteneur sur sa machine de developpement.
Pour chaque dictee extraite par `scripts.extract_dictees`, on genere un MP3
par phrase et par voix vers :

    content/francais/dictee/audio/<voix_slug>/<dictee_slug>/phrase_NN.mp3

Les fichiers MP3 sont committes dans le repo et servis comme statiques par
le runtime — le VPS n'execute jamais de TTS. La regle souverainete
(`CLAUDE.md` §1) interdit les appels externes en runtime, pas en extraction
offline ; ce script est donc analogue a `extract_dictees.py` cote
positionnement.

## Voix

Le clonage vocal utilise un echantillon de reference (ref_audio) envoye a
chaque requete. L'echantillon doit etre un MP3 francais de ~10 secondes
place dans `content/francais/dictee/voices/<slug>.mp3`.

## Dependances dev (PAS dans requirements.txt prod)

- `httpx` (deja present via FastAPI)
- `ffmpeg` (binaire systeme, pour le re-encodage en 32 kbps mono)
- Variable d'environnement `MISTRAL_API_KEY`

## Licence

Voxtral TTS utilise la licence CC BY-NC 4.0, non-commerciale.
revise-ton-dnb est educatif gratuit, donc compatible.

## Usage

    .venv/bin/python -m scripts.generate_dictee_audio
    .venv/bin/python -m scripts.generate_dictee_audio --limit 1
    .venv/bin/python -m scripts.generate_dictee_audio --voice koro
    .venv/bin/python -m scripts.generate_dictee_audio --force
    .venv/bin/python -m scripts.generate_dictee_audio --dry-run
"""

from __future__ import annotations

import argparse
import base64
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
VOICES_DIR = REPO_ROOT / "content" / "francais" / "dictee" / "voices"

API_URL = "https://api.mistral.ai/v1/audio/speech"
MODEL = "voxtral-mini-tts-2603"

# Re-encodage final : 32 kbps mono, suffisant pour de la voix dictee.
MP3_BITRATE = "32k"

# Pause entre deux appels API pour respecter le rate limit.
API_PAUSE = 1.0


@dataclass(frozen=True)
class VoiceSpec:
    slug: str
    sample_file: str
    label: str


VOICES: dict[str, VoiceSpec] = {
    "koro": VoiceSpec(
        slug="koro",
        sample_file="koro.mp3",
        label="Professeur Koro (voix masculine)",
    ),
    "maomao": VoiceSpec(
        slug="maomao",
        sample_file="maomao.mp3",
        label="Mao Mao (voix féminine)",
    ),
}


def _ensure_ffmpeg() -> None:
    if shutil.which("ffmpeg") is None:
        sys.exit(
            "ffmpeg introuvable. Installe-le (`brew install ffmpeg` sur macOS) "
            "puis relance."
        )


def _load_ref_audio(voice: VoiceSpec) -> str:
    """Charge et encode en base64 l'echantillon de reference d'une voix."""
    sample_path = VOICES_DIR / voice.sample_file
    if not sample_path.exists():
        sys.exit(
            f"Echantillon introuvable : {sample_path}\n"
            f"Place un MP3 francais de ~10s dans {VOICES_DIR}/"
        )
    return base64.b64encode(sample_path.read_bytes()).decode()


def _reencode_mp3(src_mp3: Path, dst_mp3: Path) -> None:
    """Re-encode un MP3 en mono 32 kbps via ffmpeg."""
    dst_mp3.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", str(src_mp3),
        "-ac", "1",
        "-b:a", MP3_BITRATE,
        str(dst_mp3),
    ]
    subprocess.run(cmd, check=True)


def _synthesize_one(
    api_key: str,
    text: str,
    ref_audio_b64: str,
    out_mp3: Path,
) -> None:
    """Appelle Voxtral TTS et sauvegarde le MP3 re-encode."""
    import httpx

    resp = httpx.post(
        API_URL,
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": MODEL,
            "input": text,
            "ref_audio": ref_audio_b64,
            "response_format": "mp3",
        },
        timeout=120.0,
    )
    resp.raise_for_status()
    audio_bytes = base64.b64decode(resp.json()["audio_data"])

    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
        tmp.write(audio_bytes)
        tmp_path = Path(tmp.name)

    try:
        _reencode_mp3(tmp_path, out_mp3)
    finally:
        tmp_path.unlink(missing_ok=True)


def _process_dictee(
    api_key: str,
    json_path: Path,
    voices: list[VoiceSpec],
    ref_audios: dict[str, str],
    *,
    force: bool,
    dry_run: bool,
) -> tuple[int, int]:
    data = json.loads(json_path.read_text(encoding="utf-8"))
    slug = data["id"]
    phrases = data["phrases"]
    n_done = 0
    n_skip = 0

    for voice in voices:
        out_dir = AUDIO_DIR / voice.slug / slug
        ref_b64 = ref_audios[voice.slug]

        for phrase in phrases:
            order = phrase["ordre"]
            text = phrase["texte"]
            out_mp3 = out_dir / f"phrase_{order:02d}.mp3"

            if out_mp3.exists() and not force:
                n_skip += 1
                continue

            if dry_run:
                logger.info(
                    "  [dry-run] %s/%s phrase_%02d (%d chars)",
                    voice.slug, slug, order, len(text),
                )
                n_done += 1
                continue

            out_dir.mkdir(parents=True, exist_ok=True)
            t0 = time.time()
            try:
                _synthesize_one(api_key, text, ref_b64, out_mp3)
            except Exception as e:
                logger.error(
                    "  echec %s/%s phrase %d : %s", voice.slug, slug, order, e,
                )
                continue
            dt = time.time() - t0
            logger.info(
                "  %s/%s phrase_%02d.mp3 (%.1fs, %d chars)",
                voice.slug, slug, order, dt, len(text),
            )
            n_done += 1
            time.sleep(API_PAUSE)

    return n_done, n_skip


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--force", action="store_true",
        help="re-genere meme si le MP3 existe",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="ne traite que les N premieres dictees",
    )
    parser.add_argument(
        "--voice",
        choices=list(VOICES.keys()) + ["all"],
        default="all",
        help="ne genere qu'une seule voix",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="affiche ce qui serait genere sans appeler l'API",
    )
    args = parser.parse_args()

    api_key = os.environ.get("MISTRAL_API_KEY")
    if not api_key and not args.dry_run:
        sys.exit(
            "MISTRAL_API_KEY manquant. Ajoute-le dans .env ou exporte-le."
        )

    _ensure_ffmpeg()

    if not EXERCISES_DIR.exists():
        sys.exit(f"Repertoire dictees introuvable : {EXERCISES_DIR}")

    json_files = sorted(
        p for p in EXERCISES_DIR.glob("*.json") if p.name != "_all.json"
    )
    if args.limit is not None:
        json_files = json_files[: args.limit]
    if not json_files:
        sys.exit("Aucune dictee a traiter")

    voices = (
        list(VOICES.values()) if args.voice == "all"
        else [VOICES[args.voice]]
    )

    # Charger les echantillons de reference une seule fois.
    ref_audios: dict[str, str] = {}
    if not args.dry_run:
        for voice in voices:
            logger.info("Chargement echantillon %s...", voice.sample_file)
            ref_audios[voice.slug] = _load_ref_audio(voice)
    else:
        ref_audios = {v.slug: "" for v in voices}

    logger.info(
        "%d dictee(s), %d voix, modele %s",
        len(json_files), len(voices), MODEL,
    )

    AUDIO_DIR.mkdir(parents=True, exist_ok=True)

    total_done = 0
    total_skip = 0
    for json_path in json_files:
        logger.info("\n[%s]", json_path.stem)
        n_done, n_skip = _process_dictee(
            api_key or "",
            json_path,
            voices,
            ref_audios,
            force=args.force,
            dry_run=args.dry_run,
        )
        total_done += n_done
        total_skip += n_skip

    logger.info("\n" + "=" * 60)
    logger.info(
        "Termine : %d generes, %d ignores (deja presents)",
        total_done, total_skip,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
