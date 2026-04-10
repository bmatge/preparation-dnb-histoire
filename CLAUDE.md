# CLAUDE.md — revise-ton-dnb

Ce fichier est chargé automatiquement par Claude Code à chaque session. Il
ne documente pas l'état du projet (`HANDOFF.md` s'en charge) mais les
**conventions stables** et les **garde-fous** à respecter.

## À lire en début de session

1. **Ce fichier** (CLAUDE.md) — conventions et garde-fous.
2. **`HANDOFF.md`** — état du projet, décisions récentes, TODO list.
3. Le code concerné par la tâche en cours.

---

## Stack

FastAPI + Jinja2 + HTMX + SQLite (SQLModel) + Tailwind CDN vendored. Un seul
container Docker derrière Traefik. Pas de build JS. Cible utilisateur :
élèves de 3e préparant le DNB.

## Architecture haut niveau

Plateforme **multi-matières**. Chaque matière vit dans `app/<matiere>/`
(ex. `app/histoire_geo_emc/`, `app/francais/`) avec ses propres routes,
prompts, pedagogy, models, templates. Une matière peut contenir plusieurs
**épreuves** (ex. HG-EMC a « développement construit » et « repères »), dans
ce cas chaque épreuve vit dans son propre sous-dossier et un router racine
matière les inclut.

La plateforme partagée vit dans `app/core/` : `main.py` (FastAPI root),
`db.py` (Session + Turn partagés), `albert_client.py` (client Albert avec
TASK_PROFILES par tâche), `rag.py` (collections Albert indexées par
matière), `formatting.py`, `templates/base.html`.

Les contenus sources vivent dans `content/<matiere>/` (PDF annales, JSON
extraits, etc.).

---

## Commandes de dev

```bash
# Démarrer l'app en local
source .env && .venv/bin/python -m uvicorn app.core.main:app --reload

# Démarrage via docker (comme en prod)
docker compose up --build

# Build seul
docker compose build

# Reset de la DB (mode drop & recharge — pas d'Alembic)
rm -f data/app.db

# Scripts offline (Claude Opus, jamais en prod)
.venv/bin/python -m scripts.extract_subjects content/histoire-geo-emc/annales/
.venv/bin/python -m scripts.extract_reperes
.venv/bin/python -m scripts.ingest                     # push corpus → Albert

# Health check Albert
.venv/bin/python -c "from app.core.albert_client import AlbertClient; print(AlbertClient().health_check())"
```

Pas de suite de tests à ce stade (`tests/` n'existe pas encore). Les
validations se font via smoke tests manuels + `scripts/audit_hallucinations.py`
(quand il sera écrit, cf. HANDOFF.md §6 Task 5).

Pas de linter configuré à ce stade (ni `ruff`, ni `black`, ni `mypy` dans
`requirements.txt`).

---

## Garde-fous (les plus coûteux à oublier)

### 1. Claude Opus : **OFFLINE uniquement**

Opus ne tourne que dans les scripts CLI (`scripts/extract_*.py`,
`scripts/audit_*.py`), **jamais en runtime prod**. Le runtime n'utilise que
**Albert** (`albert.api.etalab.gouv.fr`) pour des raisons de souveraineté.
Modèles Albert utilisés :
- `openai/gpt-oss-120b` pour les évaluations lourdes (reasoning, **`max_tokens ≥ 400`** obligatoire sinon le contenu est mangé par le reasoning)
- `mistralai/Mistral-Small-3.2-24B-Instruct-2506` pour les tâches UI courtes

### 2. Drop & recharge (pas d'Alembic)

Schéma DB qui évolue → `rm -f data/app.db` avant redémarrage. Les contenus
métier (sujets DC, repères, etc.) sont rechargés idempotemment au startup
depuis `content/**` par chaque sous-module via sa fonction `init_*()`
appelée dans `on_startup` de `app/core/main.py`.

### 3. Anti-hallucination (DC histoire-géo-EMC)

Les évaluations (FIRST_EVAL, SECOND_EVAL, FINAL_CORRECTION) passent par un
client Albert qui :
- **exige les citations entre crochets** `[programme]`, `[corrigé]`, `[méthodo]` (retry 1x si manquantes)
- **détecte la réécriture** (`_looks_like_ghostwritten_dc`) et refuse
- strict sur la séparation `<context>` (sources externes) vs
  `<proposition_eleve>` / `<copie_eleve>` (texte élève)

### 4. Pédagogie : l'IA ne rédige pas à la place de l'élève

Pour le DC : l'IA pose des questions, valorise, corrige, mais ne propose
**jamais** un plan tout fait ni ne rédige un paragraphe à la place. Règle
encodée dans `SYSTEM_PERSONA` + post-filtre `check_no_ghostwriting`.

Pour les repères : règle adaptée — l'IA donne **3 indices gradués** puis
révèle la réponse (la bonne réponse est l'objectif d'apprentissage,
contrairement au DC).

### 5. Ingestion Albert : pas de `.json`

Albert `/v1/documents` renvoie HTTP 422 sur les `.json`. Les fichiers JSON
(ex. sujets DC, repères) sont convertis en markdown à la volée dans
`scripts/ingest.py` avant upload — voir le pattern `CollectionSpec.convert`.

### 6. Multi-matière : ne pas contaminer entre matières

Chaque matière est isolée dans `app/<matiere>/` + `content/<matiere>/`. Ne
jamais importer du code d'une matière vers une autre — tout le code partagé
doit passer par `app/core/`. Les collections RAG sont indexées par matière
(`subject_kind` est la clé de `COLLECTION_LABELS`, `TASK_COLLECTIONS`,
`FALLBACK_COLLECTION_IDS` dans `app/core/rag.py`).

### 7. Clés API et .env

`.env` est gitignored. Les clés (ALBERT_API_KEY, ANTHROPIC_API_KEY) sont
chargées via `load_dotenv(..., override=True)` au tout début de
`app/core/main.py` — avant tout import qui lit l'environnement.

---

## Conventions de code

- **Français partout** : commentaires, docstrings, messages de log, UI, messages d'erreur élève, commits. Le seul anglais toléré : noms de symboles Python usuels (`class`, `def`, `return`, etc.) et ce qui vient des libs tierces.
- **Zéro emoji** dans le code source (ni dans les docstrings, ni dans les messages). L'UI HTML peut utiliser des emojis ponctuels pour la déco (cf. `home.html`, templates d'index).
- **Pas de refacto gratuite** : on ne touche qu'au périmètre demandé. Pas d'ajout de type hints, docstrings, wraps de gestion d'erreurs qui n'étaient pas là avant.
- **Pas d'abstraction prématurée** : trois lignes dupliquées valent mieux qu'un helper prématuré. Extraire un helper seulement quand un 3e appelant apparaît.
- **Messages élève** : tutoiement, ton bienveillant, phrases courtes, vocabulaire simple mais justes (terminologie historique/géographique correcte).
- **Gestion d'erreur côté élève** : jamais de stack trace dans l'UI. Un wrapper `_safe_chat` (DC) ou équivalent attrape tout et renvoie un message français lisible.
- **Pas de `print()`** : utiliser `logger = logging.getLogger(__name__)` + `logger.info/warning/error`.
- **Pas de migration Alembic** : cf. drop & recharge ci-dessus.
- **Sessions Starlette + DB** : l'état persistant va en DB (`Session` + `Turn`). L'état temporaire d'un parcours (ex. index courant dans un quiz) peut aller dans `request.session` (cookie).

---

## Workflow commits / PR

- **Commits atomiques** : un commit = un changement sémantique. Refacto pur (via `git mv`) séparé des changements de comportement.
- **`git mv`** systématique pour les déplacements de fichiers → préserve l'historique.
- **Format des messages de commit** : sujet en français, court, précédé d'un verbe à l'impératif ou d'un préfixe type (« Refacto … », « Ajoute … », « Fix … »). Suffixe `(#1)` pour rattacher à l'issue multi-matière racine.
- **Jamais `--no-verify`** sauf demande explicite.
- **Jamais d'amend** sur un commit déjà poussé.
- **Jamais push sur main** : toujours via PR. `main` est la branche de merge, pas de travail.
- **Ne jamais toucher au travail d'un autre agent** (p. ex. une branche parallèle en cours). Les fichiers d'une autre matière, d'un autre sous-package, ou les modifs non-committées d'un autre workstream sont intouchables.

---

## Pointeurs

- **État projet & décisions** : `HANDOFF.md` (racine du repo) — lire en début de chaque session pour connaître le contexte récent.
- **Plan initial validé** : `/home/miweb/.claude/plans/cuddly-prancing-llama.md` (chemin historique du dev solo).
- **Contenus sources** : `content/<matiere>/` (annales, programmes, méthodos, corrigés).
- **Scripts offline** : `scripts/` (extraction Opus, ingestion Albert).
