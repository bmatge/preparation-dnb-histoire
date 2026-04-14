# Annales DNB mathématiques — fenêtre 2018-2025

Sujets officiels versionnés pour servir de matériau source à l'épreuve
« raisonnement et résolution de problèmes » (`app/mathematiques/problemes/`).

## Provenance

Corpus extrait de l'archive personnelle
`~/Documents/Projets/RevisionDNB/Mathematiques/annales/`, elle-même
constituée le 2026-04-10 à partir de sources officielles :

- **Éduscol / Ministère de l'Éducation nationale**
  (`eduscol.education.gouv.fr`) — sujets officiels de la série générale.
- **APMEP** (`apmep.fr`) — recueil complémentaire, retypeset LaTeX.

Seuls les PDFs « short-name » (format `YYYY_Centre_maths.pdf`) ont été
retenus ici. Ils correspondent aux sessions principales (session de juin
en métropole, sessions de juin à l'étranger). Pour les sessions
secondaires (Polynésie sept, Nouvelle-Calédonie déc, rattrapages), les
PDFs APMEP long-name existent dans l'archive mais ne sont pas importés
en V1 : ils pourront être ajoutés dans un lot de suivi.

## Couverture (29 sujets)

| Année | Centres couverts |
|---|---|
| 2018 | Amérique Nord, Inde (Pondichéry), Métropole |
| 2019 | Amérique Nord, Métropole |
| 2020 | Amérique Nord, Métropole, Nouvelle-Calédonie |
| 2021 | Amérique Nord, Métropole |
| 2022 | Amérique Nord, Amérique Sud, Asie, Polynésie |
| 2023 | Amérique Nord, Asie, Métropole, Nouvelle-Calédonie, Polynésie |
| 2024 | Amérique Nord, Amérique Sud, Asie, Métropole, Polynésie |
| 2025 | Amérique Nord, Asie, Groupe 1, Métropole, Nouvelle-Calédonie, Polynésie |

Soit ~145 exercices de problèmes à terme (5 exercices en moyenne par
sujet).

## Note importante — réforme 2026

Ces sujets datent d'**avant la réforme du DNB 2026**. Le format
pré-2026 était :

- 5 exercices indépendants de 20 points chacun (100 pts total),
- pas de partie « automatismes » distincte (celle-ci apparaît en 2026),
- calculatrice autorisée sur tout le sujet.

Les exercices **restent parfaitement valides** pour s'entraîner sur le
programme cycle 4, **à l'exception** des rares énoncés qui dépendent
d'un format épreuve (ex. « recopier le numéro »). Lors de l'extraction
interactive à venir, ces points d'adaptation seront traités au cas par
cas.

## Extraction

L'extraction structurée (énoncé → JSON Pydantic `ProblemExerciseSchema`
avec `contexte`, `sous_questions`, `scoring`, éventuels `image`) se fait
**hors de ce commit**, dans une session Claude Code interactive
ultérieure (conformément au garde-fou « Opus offline uniquement » du
CLAUDE.md).

Les fichiers produits par cette extraction vivront dans
`content/mathematiques/problemes/exercices/` (même format que l'existant
`sujets_zero_2026.json`).
