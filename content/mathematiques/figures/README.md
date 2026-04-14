# Figures mathématiques — manifeste

Extraction offline via `pdfimages -png` (poppler). Conforme à ADR-002
(tier 1 — extraction directe depuis le PDF source officiel, fidélité
parfaite, zéro dépendance Python).

## Source

Les PDFs sources vivent dans
`content/mathematiques/automatismes/sujets_zero/`.

- `Sujet_zero_DNB_2026_serie_generale_A.pdf` (Série A)
- `Sujet_zero_DNB_2026_serie_generale_B.pdf` (Série B)
- `Sujet_zero_DNB_2026_maths.pdf` — **doublon exact de la série A**,
  on ne l'extrait pas.

## Procédure d'extraction (reproductible)

```bash
pdfimages -png -p content/mathematiques/automatismes/sujets_zero/Sujet_zero_DNB_2026_serie_generale_A.pdf \
    content/mathematiques/figures/sujets_zero/serie_A/A
pdfimages -png -p content/mathematiques/automatismes/sujets_zero/Sujet_zero_DNB_2026_serie_generale_B.pdf \
    content/mathematiques/figures/sujets_zero/serie_B/B
```

Les `smask` (masques alpha auxiliaires) renvoyés par `pdfimages` sont
ensuite supprimés à la main (inutiles pour l'affichage web).

## Inventaire

Les noms de fichiers sont ceux produits par `pdfimages` :
`{prefix}-{page:03d}-{objet:03d}.png`. Pas de renommage pour conserver
une trace directe de l'origine. Les IDs de questions suivent la
convention de `content/mathematiques/automatismes/questions/sujets_zero.json`
et `content/mathematiques/problemes/exercices/sujets_zero_2026.json`.

### Série A — 9 figures

| Fichier | Page | Partie | Question/Exercice | Contenu |
|---|---|---|---|---|
| `serie_A/A-001-000.png` | 1 | Automatismes | Q4 (`auto_sjz_A_q4`) | Droite graduée avec repères 0, 1 et point E à placer |
| `serie_A/A-001-002.png` | 1 | Automatismes | Q5 (`auto_sjz_A_q5`) | Triangle ABC rectangle en B, angle de 35° en A, angle inconnu en C |
| `serie_A/A-002-004.png` | 2 | Automatismes | Q9 (`auto_sjz_A_q9a` / `q9b`) | Blocs Scratch « définir carré » avec slots à compléter |
| `serie_A/A-002-005.png` | 2 | Automatismes | Q6 (`auto_sjz_A_q6`) | Triangle ABC rectangle en A (cosinus de l'angle ABC) |
| `serie_A/A-002-007.png` | 2 | Automatismes | Q7 (`auto_sjz_A_q7`) | Configuration de Thalès dans triangle ADE (AC = 4 cm, CB = 2 cm, DE = 7 cm) |
| `serie_A/A-004-009.png` | 4 | Problèmes | Exercice 2 (`prob_2026A_ex2`) | Flowchart « choisir, ×2, carré, −9, afficher » |
| `serie_A/A-005-010.png` | 5 | Problèmes | Exercice 3 (`prob_2026A_ex3`) | Graphique de deux droites (d₁) et (d₂) |
| `serie_A/A-006-011.png` | 6 | Problèmes | Exercice 4 (non extrait — voir note) | Carré ABCD avec octogone IJKLMNOP et cercle inscrit (centre S, diamètre 9 cm) |
| `serie_A/A-006-012.png` | 6 | Problèmes | Exercice 4 (non extrait — voir note) | Carré ABCD avec octogone IJKLMNOP seul (vue aire grisée) |

### Série B — 4 figures

| Fichier | Page | Partie | Question/Exercice | Contenu |
|---|---|---|---|---|
| `serie_B/B-002-000.png` | 2 | Automatismes | Q6 (`auto_sjz_B_q6`) | Losange ABCD de côté 3 cm |
| `serie_B/B-003-002.png` | 3 | Automatismes | Q8 (`auto_sjz_B_q8`) | Configuration de Thalès (DE // AC) pour calculer AB |
| `serie_B/B-003-004.png` | 3 | Automatismes | Q9 (`auto_sjz_B_q9`) | Algorithme Scratch (×8, +10, ÷2) |
| `serie_B/B-004-006.png` | 4 | Problèmes | Exercice 1 (non extrait — voir note) | Deux triangles avec angles 108°, 36°, inconnus x, y, z, droites (BA) et (EC) parallèles |

## Notes

- **Exercice A-4 (octogone/cercle)** et **Exercice B-1 (angles x, y, z)**
  ne sont **pas** présents dans
  `content/mathematiques/problemes/exercices/sujets_zero_2026.json`. Ils
  ont été écartés en V1 faute de rendu de figures (cf. ADR-002 et
  `_comment` des JSON). Les figures sont maintenant disponibles : ils
  peuvent donc être ré-intégrés dans les JSON et branchés dans les
  templates `app/mathematiques/problemes/templates/`.
- **Exercice A-1 (diagramme vélo)** : le diagramme en barres est
  **dessiné vectoriel directement dans le PDF**, pas embarqué comme
  image raster. `pdfimages` ne le capture donc pas. Solution future :
  `pdftoppm -r 200 -f 3 -l 3` pour rendre la page 3 en PNG, puis crop
  PIL — pas nécessaire en V1 puisque les sous-questions graphiques de
  cet exo sont déjà écartées (cf. `_comment` du JSON).
- **Questions Q4/Q5/Q6/Q7/Q9 des automatismes série A** et
  **Q6/Q8/Q9 série B** : elles sont **déjà présentes** dans
  `sujets_zero.json` mais ont été reformulées textuellement
  (cf. `_comment` des fichiers JSON). Elles peuvent maintenant être
  complétées avec un champ `image: "figures/sujets_zero/serie_X/..."`
  pour afficher la figure originale en plus de l'énoncé.

## Pistes d'intégration (non effectuées)

1. Ajouter un champ optionnel `image: str | None` dans le schéma
   Pydantic `Question` (`app/mathematiques/automatismes/models.py`) et
   `Exercice` (`app/mathematiques/problemes/models.py`).
2. Dans les templates de quiz, afficher l'image si présente (balise
   `<img>` avec `alt` tiré de l'énoncé). Servir `content/mathematiques/figures/`
   via un montage `StaticFiles` dans `app/core/main.py`.
3. Ré-intégrer les questions/exercices écartés en V1 (A-ex4, B-ex1,
   A-1 si on fait le crop pdftoppm).
