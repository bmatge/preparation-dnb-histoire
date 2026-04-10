"""Tests des convertisseurs JSON → markdown utilisés par l'ingestion Albert.

Albert ingère mal les fichiers ``.json`` via ``/v1/documents`` (HTTP 422 sur
le parser interne, gotcha §5.4 HANDOFF). Avant chaque upload, on convertit
donc :

- ``_subject_json_to_markdown`` : sujets de DC histoire-géo
  (``content/histoire-geo-emc/subjects/*.json``).
- ``_redaction_subject_json_to_markdown`` : sujets de rédaction française
  (``content/francais/redaction/subjects/*.json``).

Le résultat doit être :
- du markdown valide en UTF-8,
- contenant tous les champs métier importants (consigne, contraintes,
  bornes pour le DC, options imagination/réflexion pour la rédaction),
- avec un nom de fichier virtuel ``<stem>.md`` cohérent avec le ``.json``
  source.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.ingest import (
    _redaction_subject_json_to_markdown,
    _subject_json_to_markdown,
)


# ============================================================================
# Convertisseur DC histoire-géo
# ============================================================================


class TestSubjectJsonToMarkdown:
    def _write_dc(self, tmp_path: Path, **payload) -> Path:
        path = tmp_path / "18genhgemcan1.json"
        path.write_text(json.dumps(payload, ensure_ascii=False))
        return path

    def test_renders_one_dc_with_all_fields(self, tmp_path):
        path = self._write_dc(
            tmp_path,
            year=2018,
            serie="générale",
            session="juin",
            session_label="juin 2018",
            source_file="18genhgemcan1.pdf",
            developpements_construits=[
                {
                    "discipline": "histoire",
                    "theme": "Berlin et la guerre froide",
                    "consigne": "Décris la situation de Berlin pendant la guerre froide.",
                    "verbe_cle": "décris",
                    "bornes_chrono": "1945-1989",
                    "bornes_spatiales": "Berlin et l'Europe",
                    "notions_attendues": ["mur", "blocus", "détente"],
                    "bareme_points": 10,
                }
            ],
        )

        md_bytes, name = _subject_json_to_markdown(path)
        assert name == "18genhgemcan1.md"
        md = md_bytes.decode("utf-8")

        assert "# Sujets DC — DNB 2018 juin 2018" in md
        assert "Source : 18genhgemcan1.pdf" in md
        assert "## Développement construit 1" in md
        assert "**Discipline** : histoire" in md
        assert "**Thème** : Berlin et la guerre froide" in md
        assert "Décris la situation de Berlin" in md
        assert "**Verbe-clé** : décris" in md
        assert "**Bornes chronologiques** : 1945-1989" in md
        assert "**Bornes spatiales** : Berlin et l'Europe" in md
        assert "- mur" in md
        assert "- blocus" in md
        assert "- détente" in md
        assert "**Barème** : 10 points" in md

    def test_renders_multiple_dcs(self, tmp_path):
        path = self._write_dc(
            tmp_path,
            year=2020,
            session_label="juin 2020",
            developpements_construits=[
                {
                    "discipline": "histoire",
                    "theme": "Première Guerre mondiale",
                    "consigne": "Explique les conditions de vie des soldats.",
                },
                {
                    "discipline": "geographie",
                    "theme": "Aménagement du territoire",
                    "consigne": "Montre comment la France aménage ses territoires.",
                },
            ],
        )
        md = _subject_json_to_markdown(path)[0].decode("utf-8")
        assert "## Développement construit 1" in md
        assert "## Développement construit 2" in md
        assert "Première Guerre mondiale" in md
        assert "Aménagement du territoire" in md

    def test_skips_optional_fields_when_missing(self, tmp_path):
        # verbe_cle, bornes, notions, bareme sont optionnels — ne doivent
        # pas apparaître si absents.
        path = self._write_dc(
            tmp_path,
            year=2019,
            developpements_construits=[
                {
                    "discipline": "histoire",
                    "theme": "Test",
                    "consigne": "Consigne test.",
                }
            ],
        )
        md = _subject_json_to_markdown(path)[0].decode("utf-8")
        assert "Verbe-clé" not in md
        assert "Bornes chronologiques" not in md
        assert "Notions attendues" not in md
        assert "Barème" not in md

    def test_output_is_utf8_encoded_bytes(self, tmp_path):
        path = self._write_dc(
            tmp_path,
            year=2018,
            developpements_construits=[
                {
                    "discipline": "geographie",
                    "theme": "Île-de-France et hétérogénéité",
                    "consigne": "Décris les inégalités territoriales.",
                }
            ],
        )
        md_bytes, _ = _subject_json_to_markdown(path)
        assert isinstance(md_bytes, bytes)
        # Doit pouvoir se décoder en UTF-8 sans erreur (avec les accents).
        text = md_bytes.decode("utf-8")
        assert "Île-de-France" in text


# ============================================================================
# Convertisseur Rédaction française
# ============================================================================


def _make_redaction_payload(
    *,
    annee: int = 2023,
    centre: str = "Métropole",
    code_sujet: str | None = "23GENFRRME1",
    texte_support_ref: str | None = None,
    imagination_consigne: str = "Raconte un événement marquant.",
    imagination_contraintes: list[str] | None = None,
    imagination_longueur: int | None = None,
    reflexion_consigne: str = "Que penses-tu de cette idée ?",
) -> dict:
    return {
        "id": f"{annee}_test",
        "source": {
            "annee": annee,
            "session": "inconnu",
            "centre": centre,
            "code_sujet": code_sujet,
        },
        "epreuve": {
            "intitule": "Rédaction",
            "duree_minutes": 90,
            "points_total": 40,
        },
        "texte_support_ref": texte_support_ref,
        "sujet_imagination": {
            "type": "imagination",
            "numero": "Sujet d'imagination",
            "amorce": "Amorce du sujet d'imagination.",
            "consigne": imagination_consigne,
            "contraintes": imagination_contraintes or [],
            "longueur_min_lignes": imagination_longueur,
            "reference_texte_support": None,
        },
        "sujet_reflexion": {
            "type": "reflexion",
            "numero": "Sujet de réflexion",
            "amorce": None,
            "consigne": reflexion_consigne,
            "contraintes": [],
            "longueur_min_lignes": None,
            "reference_texte_support": None,
        },
        "source_file": "2023_Metropole_francais_redaction.pdf",
    }


class TestRedactionSubjectJsonToMarkdown:
    def _write(self, tmp_path: Path, payload: dict, stem: str = "2023_Metropole_francais_redaction") -> Path:
        path = tmp_path / f"{stem}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False))
        return path

    def test_renders_full_subject(self, tmp_path):
        payload = _make_redaction_payload(
            annee=2023,
            centre="Métropole",
            texte_support_ref="texte de Colette, Sido",
            imagination_consigne="Raconte ton plus grand souvenir d'enfance.",
            imagination_contraintes=["récit à la première personne", "présent de narration"],
            imagination_longueur=50,
            reflexion_consigne="Pourquoi raconte-t-on sa vie ?",
        )
        path = self._write(tmp_path, payload)

        md_bytes, name = _redaction_subject_json_to_markdown(path)
        assert name == "2023_Metropole_francais_redaction.md"
        md = md_bytes.decode("utf-8")

        # En-tête + métadonnées
        assert "# Sujet de rédaction — DNB 2023 Métropole" in md
        assert "Code sujet : 23GENFRRME1" in md
        assert "Texte support : texte de Colette, Sido" in md
        assert "**Épreuve** : Rédaction (40 points, 1 h 30)" in md

        # Section imagination
        assert "## Sujet d'imagination" in md
        assert "**Étiquette** : Sujet d'imagination" in md
        assert "**Amorce** : Amorce du sujet d'imagination." in md
        assert "Raconte ton plus grand souvenir d'enfance." in md
        assert "**Contraintes** :" in md
        assert "- récit à la première personne" in md
        assert "- présent de narration" in md
        assert "~50 lignes minimum" in md

        # Section réflexion
        assert "## Sujet de réflexion" in md
        assert "Pourquoi raconte-t-on sa vie ?" in md

    def test_omits_optional_fields_when_null(self, tmp_path):
        payload = _make_redaction_payload(
            code_sujet=None,
            texte_support_ref=None,
            imagination_contraintes=[],
            imagination_longueur=None,
        )
        path = self._write(tmp_path, payload)
        md = _redaction_subject_json_to_markdown(path)[0].decode("utf-8")

        assert "Code sujet" not in md
        assert "Texte support" not in md
        assert "**Contraintes**" not in md
        assert "lignes minimum" not in md

    def test_filename_stem_preserved(self, tmp_path):
        payload = _make_redaction_payload()
        path = self._write(
            tmp_path, payload, stem="2024_Antilles-Guyane_francais_redaction_2"
        )
        _, name = _redaction_subject_json_to_markdown(path)
        assert name == "2024_Antilles-Guyane_francais_redaction_2.md"

    def test_output_is_valid_utf8(self, tmp_path):
        payload = _make_redaction_payload(
            centre="Polynésie",
            imagination_consigne="« Raconte ta vie sur l'île. »",
        )
        path = self._write(tmp_path, payload)
        md_bytes, _ = _redaction_subject_json_to_markdown(path)
        assert isinstance(md_bytes, bytes)
        text = md_bytes.decode("utf-8")
        assert "Polynésie" in text
        assert "« Raconte ta vie sur l'île. »" in text

    def test_real_corpus_sample_round_trip(self, tmp_path):
        # Smoke test sur un sujet réel du corpus committé : doit se
        # convertir sans planter et produire du contenu non vide.
        repo_root = Path(__file__).resolve().parent.parent.parent
        real_path = (
            repo_root
            / "content"
            / "francais"
            / "redaction"
            / "subjects"
            / "2023_Metropole_francais_redaction.json"
        )
        if not real_path.exists():
            pytest.skip(f"Corpus manquant : {real_path}")

        md_bytes, name = _redaction_subject_json_to_markdown(real_path)
        text = md_bytes.decode("utf-8")
        assert name == "2023_Metropole_francais_redaction.md"
        assert "DNB 2023 Métropole" in text
        assert "## Sujet d'imagination" in text
        assert "## Sujet de réflexion" in text
