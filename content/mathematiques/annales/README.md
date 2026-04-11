# Annales DNB mathématiques (hors-git)

Ce dossier accueille les **annales officielles du DNB mathématiques**
(2008–2025) et leurs **corrigés**, utilisés comme corpus RAG par la
collection Albert `dnb_math_annales`.

## Pourquoi ce contenu n'est pas committé

Les 241 sujets + 190 corrigés représentent ~250 Mo de PDFs. Ils
proviennent d'une bibliothèque externe (`~/Documents/Projets/RevisionDNB/
Mathematiques`) qui est la source de vérité ; les recopier dans le repo
gonflerait inutilement l'historique git. Le dossier est donc
**gitignoré**, à l'exception de ce README et des `.gitkeep` qui en
préservent la structure.

## Synchroniser les fichiers en local

Depuis la racine du repo :

```bash
./scripts/sync_math_annales.sh
```

Le script utilise `rsync` pour copier uniquement les `.pdf` depuis
`~/Documents/Projets/RevisionDNB/Mathematiques/{annales,corrections}/`
vers ce dossier. Idempotent : un re-run ne re-télécharge que les
nouveaux fichiers.

Après synchronisation :

- `content/mathematiques/annales/*.pdf` — sujets bruts (métropole,
  Amérique du Nord/Sud, Asie, Polynésie, Nouvelle-Calédonie, Pondichéry,
  Liban, centres étrangers, séries DV/techno incluses)
- `content/mathematiques/annales/corrections/*.pdf` — corrigés associés

## Ingestion dans Albert

Une fois les PDFs en place :

```bash
# Vérifier ce qui sera ingéré
.venv/bin/python -m scripts.ingest --matiere mathematiques --collections math_annales --dry-run

# Ingestion réelle
.venv/bin/python -m scripts.ingest --matiere mathematiques --collections math_annales
```

L'ID de collection retourné par Albert doit être reporté dans
`FALLBACK_COLLECTION_IDS["mathematiques"]["dnb_math_annales"]`
(`app/core/rag.py`) pour servir de filet de sécurité si la route
`/v1/collections` est indisponible.

## Tâches qui interrogent cette collection

Définies dans `app/core/rag.py:TASK_COLLECTIONS["mathematiques"]` :

- `MATH_AUTO_EVAL_OPEN` — évaluation Albert d'une réponse ouverte d'automatisme
- `MATH_PROB_EVAL_OPEN` — évaluation Albert d'une réponse ouverte de problème

Pas d'usage côté indices ou révélation : on évite que le tuteur recopie
une rédaction d'annale pendant un parcours élève.

## Source

Bibliothèque locale : `~/Documents/Projets/RevisionDNB/Mathematiques/`

Origine des fichiers : Eduscol, APMEP, sites des académies, sites
spécialisés (Mathenpoche, M@ths Mathadoc, Sésamath…).
