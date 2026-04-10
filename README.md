# revise-ton-dnb

Application web pour aider des collégien·ne·s de 3e à s'entraîner au
**développement construit** du DNB d'histoire-géographie-EMC.

Le tuteur s'appuie sur **Albert**, le LLM souverain de l'État français
(`albert.api.etalab.gouv.fr`). Aucun appel OpenAI / Anthropic en runtime.

## Parcours élève

Sept étapes linéaires, en mode semi-assisté pour le MVP :

1. Tirage d'un sujet d'annale réelle (DNB 2018-2022).
2. L'élève écrit un plan + ses idées principales.
3. Albert pose 3 questions socratiques pour creuser (sans donner le plan).
4. L'élève retravaille sa proposition.
5. Albert souligne les progrès et identifie ce qui reste flou.
6. L'élève rédige son développement construit complet.
7. Albert produit une correction détaillée fond + forme avec sources.

## Stack

- **FastAPI** + Jinja2 + HTMX + Tailwind CDN (pas de build JS)
- **SQLite** locale (`data/app.db`)
- **Albert** : modèles `openai/gpt-oss-120b` (éval) et
  `mistralai/Mistral-Small-3.2-24B-Instruct-2506` (UI courte)
- **RAG** : 4 collections natives Albert (`dnb_programmes`, `dnb_corriges`,
  `dnb_methodo`, `dnb_sujets`) — pas de vector DB locale

## Démarrage local

```bash
# 1. Dépendances
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2. Configuration
cp .env.example .env
# Éditer .env et renseigner ALBERT_API_KEY (et ANTHROPIC_API_KEY pour les
# scripts offline d'extraction de sujets — pas requis pour faire tourner l'app).

# 3. Première ingestion du corpus dans Albert (idempotent)
set -a && . ./.env && set +a
.venv/bin/python -m scripts.ingest

# 4. Lancement serveur
.venv/bin/uvicorn app.main:app --reload --port 8000
# → ouvrir http://127.0.0.1:8000
```

Au premier démarrage, la base SQLite est créée et les 23 sujets d'annales
sont chargés depuis `content/histoire-geo-emc/subjects/*.json` (générés par `extract_subjects.py`).

## Démarrage Docker / prod

L'app est packagée pour un déploiement derrière Traefik (cf
`docker-compose.yml`).

```bash
docker compose build
docker compose up -d
docker compose logs -f
```

L'URL de prod est définie dans les labels Traefik
(`revise-ton-brevet.matge.com`).

⚠️ **Penser à fixer `SESSION_SECRET`** dans `.env` avant la mise en ligne :
sans valeur stable, chaque restart du conteneur invalide les sessions des
élèves en cours.

## Scripts CLI

| Script | Quand l'utiliser |
|---|---|
| `python -m scripts.extract_subjects content/histoire-geo-emc/annales/` | Re-extraire les sujets DC depuis les PDF d'annales (Opus offline, coûte des tokens Anthropic) |
| `python -m scripts.ingest` | Pousser le corpus dans Albert (idempotent, sha256) |
| `python -m scripts.ingest --only methodo` | Re-pousser une seule collection |
| `python -m scripts.ingest --force` | Re-pousser tout, même les fichiers inchangés |

## Structure

```
app/
├── main.py            # FastAPI + routes + templates Jinja
├── prompts.py         # Templates pédagogiques (cœur du tuteur)
├── albert_client.py   # Wrapper OpenAI-compat + routage tâches + post-filtres
├── rag.py             # Recherche dans les collections Albert + nettoyage
├── pedagogy.py        # Orchestration des étapes (RAG + prompts + persist.)
├── db.py              # SQLModel : Subject, Session, Turn
└── templates/         # Jinja + HTMX
scripts/
├── extract_subjects.py  # Opus offline → JSON structuré
└── ingest.py            # Push du corpus vers les collections Albert
content/
└── histoire-geo-emc/
    ├── programme/     # Programmes officiels cycle 4 (PDF)
    ├── methodologie/  # Fiches méthodo DC
    ├── annales/       # PDF DNB 2018-2022
    ├── corriges/      # Corrigés modèles
    └── subjects/      # JSON par annale (sortie d'extract_subjects)
        └── variations/ # Variations générées offline par Opus
data/
├── ingest_state.db    # SHA256 des fichiers ingérés (idempotence)
└── app.db             # SQLite runtime (sessions élèves)
```

## Garde-fous pédagogiques

- **Anti-rédaction-à-la-place** : prompt système strict + post-filtre
  heuristique côté `albert_client.py` (`GhostwritingDetected`).
- **Anti-hallucination** : règle « ne cite que ce qui est dans les balises
  `<context>` » + obligation de citer la source entre crochets pour chaque
  fait historique. Retry automatique si les citations manquent.
- **Pas de plan dicté** : la phrase finale de la correction est contrainte
  à une seule orientation générale, pas de plan numéroté.
- **Séparation contexte / copie élève** : balises XML dédiées
  (`<copie_eleve>`, `<proposition_eleve>`) pour éviter que l'IA confonde
  ce que l'élève a écrit avec ce qui est dans le RAG.

## Prochaines étapes (post-MVP)

- Mode `TRES_ASSISTE` (mindmap + décryptage explicite)
- Mode `NON_ASSISTE` (éval brève)
- Streaming SSE pour afficher les réponses Albert en temps réel
- Script `audit_hallucinations.py` (cf HANDOFF.md §6, tâche 5) pour
  valider 0 hallucination avant ouverture publique
