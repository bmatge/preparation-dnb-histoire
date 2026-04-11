# Figures — mathématiques

Ce dossier contient les figures utilisées par les questions d'automatismes
(et bientôt les exercices de problèmes) qui dépendent d'un support visuel.

## Origine

Les figures sont **extraites directement depuis les PDFs des sujets zéro
officiels** (`content/mathematiques/automatismes/sujets_zero/`) à l'aide
de `pdfimages` (fournit par le paquet `poppler` sur macOS :
`brew install poppler`). Pas de re-génération côté code : on récupère
l'image exacte que l'élève verra sur le vrai sujet.

C'est la stratégie par défaut de l'ADR-002. La génération matplotlib
reste en fallback pour le jour où on aura des variantes paramétrées, mais
ce n'est pas utilisé en V1.

## Manifeste

| Fichier | Source PDF | Page | Question |
|---|---|---|---|
| `auto_sjz_a_q4_droite_graduee.png` | `Sujet_zero_DNB_2026_serie_generale_A.pdf` | 1 | Automatisme Q4 — droite graduée, abscisse du point E |
| `auto_sjz_a_q7_thales.png` | `Sujet_zero_DNB_2026_serie_generale_A.pdf` | 2 | Automatisme Q7 — Thalès, triangle ADE avec (DE)//(CB) |
| `auto_sjz_a_q9_scratch_carre.png` | `Sujet_zero_DNB_2026_serie_generale_A.pdf` | 2 | Automatisme Q9 — blocs Scratch, programme « définir carré » |
| `auto_sjz_b_q4_temperature.png` | `Sujet_zero_DNB_2026_serie_generale_B.pdf` | 2 | Automatisme Q4 — graphique température en fonction de l'horaire |
| `auto_sjz_b_q8_thales.png` | `Sujet_zero_DNB_2026_serie_generale_B.pdf` | 3 | Automatisme Q8 — Thalès, triangle avec (DE)//(AC) |
| `auto_sjz_b_q9_algorithme.png` | `Sujet_zero_DNB_2026_serie_generale_B.pdf` | 3 | Automatisme Q9 — blocs Scratch, algorithme multiplier/ajouter/diviser |
| `prob_sjz_a_ex3_droites.png` | `Sujet_zero_DNB_2026_serie_generale_A.pdf` | 5 | Problème Ex3 — graphe des droites (d1) et (d2) pour f(x)=4x+3 et g(x)=6x |
| `prob_sjz_a_ex4_octogone.png` | `Sujet_zero_DNB_2026_serie_generale_A.pdf` | 6 | Problème Ex4 — octogone IJKLMNOP inscrit dans le carré ABCD (sans cercle) |
| `prob_sjz_a_ex4_octogone_et_cercle.png` | `Sujet_zero_DNB_2026_serie_generale_A.pdf` | 6 | Problème Ex4 — octogone + cercle inscrit de diamètre 9, figure principale de l'exercice |
| `prob_sjz_b_ex1_triangle.png` | `Sujet_zero_DNB_2026_serie_generale_B.pdf` | 4 | Problème Ex1 — triangle ABC avec (BA)//(EC), ABC=36°, BAC=108°, x/y/z à déterminer |

## Reproduire l'extraction

```bash
# Lister les images embarquées (diagnostic)
pdfimages -list content/mathematiques/automatismes/sujets_zero/Sujet_zero_DNB_2026_serie_generale_A.pdf

# Extraire toutes les images des pages 1 à 2 en PNG
pdfimages -png -p -f 1 -l 2 \
  content/mathematiques/automatismes/sujets_zero/Sujet_zero_DNB_2026_serie_generale_A.pdf \
  /tmp/serieA

# Les images sont nommées <prefix>-<page>-<index>.png. Les fichiers en mode L
# (grayscale) sont des masks alpha (smask) qui peuvent être ignorés quand le
# fond des figures est déjà blanc.
```

Pour une nouvelle question officielle, la démarche est :

1. Identifier le PDF source et la page de la question
2. `pdfimages -list` pour voir les images embarquées sur cette page
3. `pdfimages -png -p -f N -l N` pour extraire
4. Vérifier visuellement quelle image correspond (les tailles aident : figures
   géométriques = portrait, graphiques = carré, Scratch = plus large)
5. Renommer en `<id_question>.png` et déposer ici
6. Mettre à jour le tableau ci-dessus

## Pour les figures qui ne viennent pas d'un PDF source

Si on introduit plus tard des **variantes paramétrées** (ex. un Thalès avec
des valeurs différentes tirées au hasard), il faudra retomber sur la
stratégie de fallback de l'ADR-002 : génération matplotlib offline. À
ce moment-là, on introduira un sous-dossier `generated/` avec la logique
`src/out`. Tant qu'on reste sur les sujets officiels, on n'en a pas besoin.
