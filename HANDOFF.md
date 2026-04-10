# HANDOFF — revise-ton-dnb

> Document de passation entre sessions Claude Code.
> Session précédente : 2026-04-10. À lire intégralement avant de reprendre.

## 0. Contexte global

**Projet** : application web `revise-ton-dnb` pour aider des collégien·ne·s de 3e
à s'entraîner au **développement construit** (exercice clé du DNB histoire-géo-EMC).

**Utilisateur** : dev solo, francophone, veut une app souveraine basée sur
**Albert** (LLM du gouvernement français, `albert.api.etalab.gouv.fr`). Il ne
veut **pas** d'OpenAI/Anthropic en runtime.

**Repo** : `bmatge/revise-ton-dnb` (renommé le 2026-04-10, ex-`preparation-dnb-histoire`, cf. #8)

**Plan initial validé** : `/home/miweb/.claude/plans/cuddly-prancing-llama.md`
(à relire pour la vue complète).

---

## 1. Parcours pédagogique (ce que l'app doit faire)

7 étapes linéaires :

1. **Sujet** tiré des annales DNB
2. **Proposition libre** de l'élève (plan + idées)
3. **Première évaluation** par Albert
4. **Re-proposition** de l'élève
5. **Seconde évaluation** (compare v1/v2, souligne progrès)
6. **Rédaction** du développement construit complet
7. **Correction finale** (fond + forme, sources citées)

3 modes d'assistance :
- `TRES_ASSISTE` — mindmap + décryptage du sujet + éval détaillée
- `SEMI_ASSISTE` — questions socratiques, zéro diagnostic direct (**mode MVP**)
- `NON_ASSISTE` — éval brève, pas de méthodo

---

## 2. Décisions d'architecture (arrêtées, ne pas remettre en cause sans raison)

| Sujet | Décision | Rationale |
|---|---|---|
| **Stack** | FastAPI + Jinja2 + HTMX + SQLite + Tailwind CDN, un seul container Docker | Dev solo, pas de build JS, SSE natif |
| **Déploiement** | Auto-hébergé Docker, derrière Traefik, URL `revise-ton-brevet.matge.com` | Déjà configuré dans `docker-compose.yml` (labels Traefik) |
| **Modèles IA runtime** | `openai/gpt-oss-120b` (éval/correction) + `mistralai/Mistral-Small-3.2-24B-Instruct-2506` (UI/socratique) | Confirmés dispo sur Albert via `/v1/models` |
| **Claude Opus** | Uniquement OFFLINE par le dev (extraction sujets, audit hallucinations). **Jamais en prod**. | Coût quasi nul en runtime |
| **RAG** | Collections natives Albert (`/v1/collections` + `/v1/documents` + `/v1/search`). **Pas** de vector DB locale. | Souveraineté, simplicité, bge-m3 géré par Albert |
| **Chunking** | Délégué à Albert (`RecursiveCharacterTextSplitter` côté serveur, `chunk_size=2048`) | Pas besoin de pré-chunker |
| **Anti-hallucination** | Collection `dnb_programmes` (programme officiel 3e) = source d'autorité factuelle. Citations obligatoires entre `[crochets]` dans les évals. | Demande explicite user |
| **Mode MVP** | **Semi-assisté uniquement**. Très/Non viendront en V2. | Priorité : parcours complet avant modes |

---

## 3. ⚠️ Point de sécurité à traiter

L'utilisateur a **exposé ses clés API dans l'historique** (sélection IDE).
Il a dit « pour l'instant reste sur ces clés » au lieu de les révoquer.
**À rappeler à la fin du prochain run** : rotation des clés recommandée
(Anthropic console + console Albert/Etalab).

---

## 4. Ce qui est DÉJÀ FAIT (✅)

### 4.1 Structure projet
```
revise-ton-dnb/
├── .env              # ALBERT_API_KEY + ANTHROPIC_API_KEY (gitignored ✓)
├── .env.example      # template
├── .gitignore        # protège .env, .venv, data/
├── .venv/            # créé pour itération rapide (openai, httpx, pdfplumber, anthropic)
├── Dockerfile        # python:3.12-slim + uvicorn
├── docker-compose.yml# Traefik labels déjà intégrés (modifié par l'user)
├── requirements.txt  # FastAPI + jinja + sqlmodel + httpx + openai + anthropic + pdfplumber
├── content/histoire-geo-emc/
│   ├── annales/         # 23 PDF DNB 2018-2022
│   ├── corriges/        # 3 PDF corrigés modèles
│   ├── methodologie/    # 9 PDF + 1 .md (MrDarras est la référence)
│   ├── programme/       # 9 PDF officiels cycle 4 + compétences
│   └── subjects/        # 23 JSON + variations/ (cf extract_subjects.py)
├── app/
│   ├── __init__.py
│   ├── core/              Plateforme mutualisée (FastAPI root, DB partagée,
│   │                      Albert client, RAG, formatting, base.html)
│   └── histoire_geo_emc/  Matière DNB histoire-géo-EMC : routes, pedagogy,
│                          prompts, Subject, templates step_*.html
├── scripts/
│   ├── __init__.py
│   ├── extract_subjects.py ✅ Opus offline → JSON structuré
│   └── ingest.py           ✅ 4 collections Albert, idempotent
└── data/
    ├── app.db           # SQLite runtime (sessions élèves)
    └── ingest_state.db  # SQLite tracking sha256
```

### 4.2 `app/prompts.py`
- `SYSTEM_PERSONA` constant avec règle anti-hallucination méta (voir §5.1)
- 5 builders : `build_decrypt_subject`, `build_first_eval`, `build_second_eval`,
  `build_final_correction`, + constante `REFUSAL_REDACTION`
- Utilise **balises XML** `<context>`, `<proposition_eleve>`, `<copie_eleve>`
  pour séparer strictement RAG et texte de l'élève
- Le "conseil prioritaire" final est contraint à **une seule phrase
  d'orientation générale**, jamais un plan détaillé (cf §5.2)
- Modulation par mode implémentée pour `first_eval` / `second_eval` / `final_correction`

### 4.3 `app/albert_client.py`
- `AlbertClient(api_key, base_url)` — wrapper SDK `openai` en mode compat
- Routage par `Task` enum : `DECRYPT_SUBJECT`, `FIRST_EVAL`, `SECOND_EVAL`,
  `FINAL_CORRECTION`, `UI_TEXT`
- `TASK_PROFILES` → modèle + température + max_tokens + flags post-filtres
- **Post-filtres** :
  - `_looks_like_ghostwritten_dc` : désactivé pour `FINAL_CORRECTION`
    (faux positifs), actif pour eval1/eval2
  - `_has_citations` : regex `[programme]|[corrigé]|[méthodo]`, avec
    retry automatique une fois
- Exceptions : `GhostwritingDetected`, `MissingCitations`
- `chat()` non-streaming + `chat_stream()` pour SSE + `health_check()`
- **⚠️ gpt-oss-120b est un modèle "reasoning"** : consomme des tokens en
  `reasoning_content` AVANT le `content`. Prévoir `max_tokens ≥ 400`
  pour toute tâche non triviale (bug qui m'a piégé au début).

### 4.4 `scripts/extract_subjects.py`
- Opus (`claude-opus-4-6`, température 0) sur chaque PDF d'annale
- Extrait UNIQUEMENT les consignes explicitement nommées « développement
  construit » (pas les mini-rédactions EMC)
- Schéma JSON : `consigne, discipline, theme, verbe_cle, bornes_chrono,
  bornes_spatiales, notions_attendues[], bareme_points`
- Parse les noms de fichiers (`YYgenhgemcXXX.pdf`) pour année/série/session
- **23/23 annales traitées** : content/histoire-geo-emc/subjects/*.json + _all.json
- Distribution : 11 DC histoire, 12 DC géo
- Thèmes couverts : aménagement territoire, espaces faible densité,
  ultramarins, guerres mondiales, guerre froide, totalitarismes, Vichy

### 4.5 `scripts/ingest.py`
- 4 collections Albert toutes créées et peuplées :

| Collection Albert | ID | Docs | Source |
|---|---:|---:|---|
| `dnb_hgemc_methodo` | *tbd* | 7 | content/histoire-geo-emc/methodologie/ |
| `dnb_hgemc_corriges` | *tbd* | 3 | content/histoire-geo-emc/corriges/ |
| `dnb_hgemc_programmes` | *tbd* | 9 | content/histoire-geo-emc/programme/ |
| `dnb_hgemc_sujets` | *tbd* | 23 | content/histoire-geo-emc/subjects/*.json → md |

*tbd* : les IDs seront attribués au prochain run de `scripts/ingest.py` sous
le nouveau nommage. Pendant la fenêtre de bascule, `app/core/rag.py`
retombe sur les anciens noms via `LEGACY_COLLECTION_ALIASES` (anciens IDs :
methodo=184792, corriges=184795, programmes=184797, sujets=184809).

- Idempotence via SHA256 dans `data/ingest_state.db`
- **JSON → Markdown à la volée** pour `dnb_sujets` (Albert rejette le .json)
- RAG testé sur 4 scénarios avec scores cosine 0.69-0.84, résultats pertinents
- Usage : `python -m scripts.ingest [--only <key>] [--force] [--dry-run]`
  où `<key>` ∈ {sujets, corriges, methodo, programmes}

### 4.6 Tests live déjà passés
- `/v1/models` → OK, 7 modèles visibles
- Appels chat sur les 2 modèles cibles → OK, français propre
- Parcours complet en RAG (4 tâches sur `prompts.py`) → ~6500 tokens, 1.6g CO2eq
- Recherche sémantique multi-collections → OK

---

## 5. Gotchas / bugs déjà rencontrés et fixés (NE PAS REFAIRE)

### 5.1 Hallucination méta (CRITIQUE)
**Symptôme** : gpt-oss-120b attribuait à l'élève des faits présents dans le
`<context>` RAG mais absents de sa copie (« tu as bien identifié le blocus
de 1948 » alors que l'élève n'en parlait pas).

**Fix** :
- Balises XML dédiées `<proposition_eleve>`, `<proposition_eleve_v1/v2>`,
  `<copie_eleve>` au lieu de guillemets triples
- Règle explicite dans `SYSTEM_PERSONA` : « quand tu dis "tu as fait X",
  vérifie que X est dans CES balises, jamais dans `<context>` »
- Validé sur un run, mais à re-valider sur le jeu anti-hallucination (10 sujets)

### 5.2 Conseil final qui dicte un plan
**Symptôme** : dans la correction finale, Albert finissait par « commence par
X, puis parle de Y, termine par Z » — violation de la règle "l'IA ne fait pas
à la place".

**Fix** : règle durcie dans `build_final_correction` : le conseil final **doit
tenir en une seule phrase d'orientation générale**, interdiction explicite des
formats `I.1/I.2` ET de la prose « commence par… puis… ». Validé sur 3 runs.

### 5.3 Ghostwriting filter faux positif
**Symptôme** : `_looks_like_ghostwritten_dc` bloquait les vraies corrections
finales parce qu'elles contiennent beaucoup de prose.

**Fix** : `check_no_ghostwriting=False` pour `Task.FINAL_CORRECTION`, seuil
baissé à 300 caractères, détection basée sur l'absence de marqueurs
d'interaction (`tu`, `ton`, `?`, `•`) ET de marqueurs de structure
(`FOND`, `FORME`, `points forts`...).

### 5.4 JSON non parsé par Albert
**Symptôme** : `POST /v1/documents` avec `.json` → HTTP 422
"Parsing document failed".

**Fix** : `_subject_json_to_markdown` dans `ingest.py` convertit chaque JSON
de sujet en markdown lisible avant upload.

### 5.5 Regex session `g1`
**Symptôme** : 3 annales avec code `g11pdf` (session = `g1`, variante = `1`)
avaient `year=None` car la regex voulait `[a-z]{2,3}` pour la session.

**Fix** : regex élargie à `[a-z][a-z0-9]` pour accepter les digits dans le
code session. Corrigé sans re-dépenser de tokens Opus (reparse local).

### 5.6 max_tokens trop bas avec gpt-oss
**Symptôme** : `content: None` sur gpt-oss-120b avec `max_tokens=80`.

**Cause** : modèle reasoning qui consomme des tokens en `reasoning_content`
AVANT `content`. Avec seulement 80 tokens, tout est mangé par le raisonnement.

**Fix** : `max_tokens ≥ 400` minimum dans tous les `TASK_PROFILES`.

### 5.7 Artefacts dans les chunks
Les PDF extraits par Albert contiennent parfois `==> picture [...] intentionally
omitted <==`, `<br>`, etc. Ça ne gêne pas la recherche mais **à nettoyer dans
`rag.py`** avant d'injecter dans le prompt (sinon l'élève voit des trucs moches).

---

## 6. Ce qui RESTE À FAIRE

### Tâche 1 — `app/rag.py` (backend, court)
Wrapper `AlbertRagClient` + helper qui transforme un résultat de recherche en
liste de `RagPassage` (type déjà défini dans `prompts.py`). Fonctions attendues :
- `search(query: str, collections: list[str], limit: int = 5, score_threshold: float = 0.5) -> list[RagPassage]`
- `search_for_task(task: Task, query: str, subject_theme: str | None) -> list[RagPassage]`
  qui choisit automatiquement quelles collections interroger selon l'étape :
  - decrypt/eval : `dnb_programmes` + `dnb_corriges` + `dnb_methodo`
  - correction finale : les 3 + `dnb_sujets` pour recroiser
- Helper `_clean_chunk(text)` qui strip les `==> picture ... <==`, `<br>`,
  `**` excessifs, source lines en bas de page
- Cache en mémoire basique (dict keyed by (query, collections)) pour éviter
  de rappeler Albert 2x dans la même session d'un élève
- Tests : une query par collection pour valider les résultats

Les IDs de collections peuvent être hardcodés dans `app/rag.py` avec un
commentaire indiquant qu'ils sont créés par `scripts/ingest.py` :
```python
COLLECTION_IDS = {
    "dnb_methodo": 184792,
    "dnb_corriges": 184795,
    "dnb_programmes": 184797,
    "dnb_sujets": 184809,
}
```
Ou mieux : résoudre dynamiquement par nom au démarrage via `GET /v1/collections`.

### Tâche 2 — `app/db.py` (backend, court)
SQLModel tables :
- `Session(id, mode, subject_id, created_at)` — session d'un élève
- `Turn(id, session_id, step: int, role: "user"|"assistant", content: text, created_at)`
- `Subject(id, year, session, discipline, theme, consigne, verbe_cle, bornes_chrono, bornes_spatiales, notions_attendues: json, bareme_points, source_file)` — chargé depuis `content/histoire-geo-emc/subjects/*.json` au démarrage
- Helpers : `load_subjects_from_jsons()`, `random_subject(discipline?, theme?)`, `create_session()`, `add_turn()`, `get_session_history()`
- SQLite file : `data/app.db` (gitignored)

### Tâche 3 — `app/pedagogy.py` (backend, glue)
Orchestration qui reçoit la session + l'étape + l'input élève et appelle
prompts.py + albert_client.py + rag.py dans le bon ordre :
- `run_step_3(session, first_proposal) -> str` (appelle rag.search + build_first_eval + client.chat)
- `run_step_5(session, second_proposal) -> str`
- `run_step_7(session, student_text) -> str`
- `run_decrypt_subject(session) -> dict` (pour mode très assisté, V2)
- Sauvegarde automatique des turns dans la DB
- Gestion des erreurs `GhostwritingDetected` / `MissingCitations` → message
  d'erreur gracieux à l'élève + log

### Tâche 4 — `app/main.py` + templates HTMX (front)
- FastAPI routes :
  - `GET /` → page d'accueil, bouton "Commencer"
  - `POST /session/new` → crée session, redirect vers `/step/1`
  - `GET /step/{n}` → affiche l'étape n de la session courante (cookie)
  - `POST /step/{n}/submit` → traite l'input, appelle pedagogy.run_step_{n},
    renvoie un fragment HTML HTMX pour afficher la réponse
- Middleware session cookie (signed, via `itsdangerous` — déjà dans requirements)
- Templates Jinja2 dans `app/templates/` :
  - `base.html` — layout global avec Tailwind CDN
  - `step_1_subject.html` — affiche le sujet tiré + bouton "J'ai des idées"
  - `step_2_proposal.html` — textarea pour proposition
  - `step_3_eval.html` — affiche l'éval + bouton "Je retravaille"
  - `step_4_reproposal.html` — textarea
  - `step_5_eval.html` — seconde éval + bouton "Je rédige"
  - `step_6_writing.html` — grand textarea
  - `step_7_correction.html` — correction finale
  - `_partials/eval_response.html` — fragment HTMX
- Streaming optionnel SSE : garder pour V2, MVP en mode bloquant c'est OK
- CSS minimal via Tailwind CDN — pas de build

### Tâche 5 — Tests anti-hallucination
Script `scripts/audit_hallucinations.py` :
- Tire 10 sujets de la DB au hasard
- Pour chacun, simule un parcours élève avec une proposition/copie "moyenne"
  générée par Opus (pour avoir du contenu réaliste)
- Lance les étapes 3 + 7 via `pedagogy.py`
- Feed les sorties Albert dans Opus avec prompt : « Pour chaque affirmation
  factuelle dans cette réponse, indique si elle est présente dans le corpus
  fourni ci-dessous. Toute affirmation absente du corpus est une hallucination. »
- Logge les résultats dans `data/audit.json`
- **Objectif : 0 hallucination avant mise en ligne publique**

### Tâche 6 — README + démarrage
- `README.md` (court) : installation locale, installation Docker, commandes
  d'ingestion, URL prod
- Vérifier que `docker compose up --build` suffit à lancer l'app
- Tester un parcours élève complet en local via le navigateur

---

## 7. Commandes utiles (à avoir sous la main)

```bash
# Setup
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Env
set -a && . ./.env && set +a

# Ingestion
.venv/bin/python -m scripts.ingest --dry-run          # simule
.venv/bin/python -m scripts.ingest                    # tout
.venv/bin/python -m scripts.ingest --only methodo     # une seule
.venv/bin/python -m scripts.ingest --force            # re-pousse

# Extraction sujets (OFFLINE, coûte des tokens Opus)
.venv/bin/python -m scripts.extract_subjects content/histoire-geo-emc/annales/
.venv/bin/python -m scripts.extract_subjects content/histoire-geo-emc/annales/18genhgemcan1pdf-80388.pdf
.venv/bin/python -m scripts.extract_subjects content/histoire-geo-emc/annales/ --limit 2
.venv/bin/python -m scripts.extract_subjects content/histoire-geo-emc/annales/ --force

# Test de connectivité Albert
.venv/bin/python -c "
from app.core.albert_client import AlbertClient
print(AlbertClient().health_check())
"

# Recherche RAG manuelle
.venv/bin/python -c "
import os, httpx
BASE = 'https://albert.api.etalab.gouv.fr/v1'
KEY = os.environ['ALBERT_API_KEY']
r = httpx.post(f'{BASE}/search',
    headers={'Authorization': f'Bearer {KEY}'},
    json={'collection_ids':[184797], 'prompt':'guerre froide', 'method':'semantic', 'limit':3}
)
print(r.json())
"

# Docker (prod)
docker compose build
docker compose up -d
docker compose logs -f
```

---

## 8. Contraintes et conventions de code (respecter)

- **Français** pour tous les commentaires, docstrings, variables d'UI, logs
  destinés à l'user final. Anglais OK pour les noms de fonctions techniques.
- **Pas d'emojis** dans le code ou les fichiers sauf si explicitement demandé.
- **Pas de commentaires triviaux**, seulement là où la logique n'est pas
  évidente (pourquoi, pas quoi).
- **Pas de refacto gratuite** : l'user ne veut pas de "améliorations"
  au-delà de ce qui est demandé.
- **Tester en live** autant que possible : l'user apprécie voir les résultats
  réels, pas les "ça devrait marcher".
- **Jamais d'appel Opus en runtime prod**, uniquement via les scripts CLI
  offline.
- **Jamais de rédaction à la place de l'élève**, c'est LA règle pédagogique
  cardinale — tout post-filtre qui la relâche doit avoir une très bonne
  raison.

---

## 9. Point d'entrée pour la prochaine session

Commence par :

1. Lire CE fichier (HANDOFF.md) intégralement
2. Lire `/home/miweb/.claude/plans/cuddly-prancing-llama.md` pour le plan initial
3. Lire `app/prompts.py` et `app/albert_client.py` pour comprendre l'API interne
4. Vérifier que `.env` contient bien `ALBERT_API_KEY` et `ANTHROPIC_API_KEY`
5. `source .env && .venv/bin/python -c "from app.core.albert_client import AlbertClient; print(AlbertClient().health_check())"` pour confirmer que l'API Albert marche
6. Enchaîner les tâches 1 → 6 dans l'ordre de §6


**Mode opératoire attendu** : autonomie, décisions pragmatiques, tests
fréquents en live, pas de demandes de validation à chaque étape. L'user a
explicitement dit « enchaine tout en autonomie ».

Bonne continuation. 🚀

---

## ADDENDUM 2026-04-10 — Épreuve Repères HG-EMC (étape 3)

Branche : `feature/hg-reperes` (merge prévu **avant** la PR français pour
minimiser les conflits sur `app/core/`).

### 1. Refacto par épreuve

`app/histoire_geo_emc/` est maintenant scindé en sous-dossiers par
épreuve :

```
app/histoire_geo_emc/
  routes.py                        # router racine matière : index +
                                   # redirects de compat + include_router
  templates/index.html             # page d'index matière (2 cartes)
  developpement_construit/         # ex-contenu de histoire_geo_emc/
    routes.py, pedagogy.py, prompts.py, models.py, templates/
  reperes/                         # NOUVEAU
    routes.py, pedagogy.py, prompts.py, models.py, templates/
```

**URLs finales** :
- `/histoire-geo-emc/` → index matière (liste les 2 épreuves)
- `/histoire-geo-emc/developpement-construit/step/{n}` → DC (ex-`/step/{n}`)
- `/histoire-geo-emc/reperes/` → accueil repères
- `/histoire-geo-emc/reperes/quiz`, `/quiz/new`, `/quiz/answer`, `/quiz/synthese`

**Redirects de compat** (dans `app/histoire_geo_emc/routes.py`) :
- `/histoire-geo-emc/step/{n}` → 307 → `…/developpement-construit/step/{n}`
- `/histoire-geo-emc/session/new` → 307 → DC
- `/histoire-geo-emc/restart` → 303 → DC

Les redirects root-level dans `app/core/main.py` (`/step/N`, `/session/new`,
`/restart`) restent inchangés — ils redirigent vers les URLs matière, qui
redirigent à leur tour vers l'épreuve (double-hop acceptable).

### 2. Évolution du modèle `Session`

```python
class Session(SQLModel, table=True):
    subject_kind: str = Field(default="hgemc_dc", index=True)
    subject_id: int | None = Field(default=None, foreign_key="subject.id")
    ...
```

- `subject_kind` : identifiant libre d'épreuve. Valeurs utilisées :
  `"hgemc_dc"` et `"hgemc_reperes"`. L'agent français utilisera
  `"francais_..."` (au choix).
- `subject_id` : **nullable** — les sessions repères ne pointent pas
  vers une ligne `Subject`.
- `create_session(s, subject_kind=..., subject_id=..., mode=...)` :
  signature étendue, appelants DC passent `subject_kind="hgemc_dc"`.

### 3. ⚠️ À faire au redémarrage post-merge

L'app est en mode « drop & recharge » (pas d'Alembic). **Avant de
redémarrer après merge**, supprimer la DB pour recréer le schéma propre
avec la nouvelle colonne :

```bash
rm -f data/app.db
```

Les contenus (sujets DC + repères) sont rechargés idempotemment au
startup depuis les fichiers JSON dans `content/histoire-geo-emc/`.

### 4. Épreuve Repères — contenu

- **109 repères** chargés depuis
  `content/histoire-geo-emc/reperes/_all.json`, extraits depuis le BO
  n°42 du 14 novembre 2013 (annexe I — liste explicite des repères de
  fin de scolarité obligatoire) et le programme cycle 4 actuel
  (valeurs/notions EMC).
- Répartition : 62 histoire, 29 géographie, 18 EMC. 36 dates, 29
  événements, 29 lieux, 11 notions, 3 personnages, 1 définition.
- Règle cardinale respectée : aucun repère inventé — tous proviennent
  de listes textuelles explicites, avec traçabilité dans le champ
  `source` de chaque entrée.

### 5. Re-générer les repères si besoin

Script offline (Claude Opus, pattern copié de `extract_subjects.py`) :

```bash
source .env
.venv/bin/python -m scripts.extract_reperes
.venv/bin/python -m scripts.extract_reperes --force   # re-traite tout
.venv/bin/python -m scripts.extract_reperes --limit 2 # test rapide
```

Idempotent via SHA256 dans `data/ingest_state.db` (table
`reperes_sources`).

Note : la toute première extraction a été faite **directement dans une
session Claude Code** (lecture PDF via Read, écriture JSON via Write)
pour éviter un appel API Anthropic payant. Le script est là pour les
mises à jour futures.

### 6. Pédagogie repères — règle cardinale adaptée

Contrairement au DC où l'IA ne donne jamais la réponse, ici la bonne
réponse **est** l'objectif d'apprentissage. L'IA donne :

- **Niveau 1** : contexte très large (époque, siècle, discipline).
- **Niveau 2** : indice ciblé (verbe-clé, champ thématique précis).
- **Niveau 3** : quasi-réponse (première lettre, décennie, région).
- Après le 3ᵉ échec : **révélation** bienveillante avec
  contextualisation, et le repère est ajouté à la file de
  réexposition de fin de partie.

L'évaluation de la réponse est **déterministe Python** (pas d'Albert) :
normalisation accents/casse/ponctuation, tolérance ±1 an sur les
dates, gestion des dates « avant J.-C. », match partiel généreux sur
les libellés.

Albert (Mistral-Small) n'est appelé que pour **formuler** les
questions, les indices et la révélation — jamais pour **juger**.
Fallback déterministe pour chaque appel si Albert est indisponible.

### 7. Fichier `data/ingest_state.db`

Nouvelle table :
```
reperes_sources(path TEXT PK, sha256 TEXT, processed_at TEXT)
```
Cohabite avec la table existante pour `extract_subjects.py` — pas de
collision.

### 8. Fichiers 100 % à moi (PR HG repères)

Tout ce qui suit est dans le périmètre de cette PR et peut être
reviewé isolément :
- `app/histoire_geo_emc/routes.py` (nouveau rôle : router racine)
- `app/histoire_geo_emc/templates/index.html`
- `app/histoire_geo_emc/developpement_construit/**` (refacto pur
  + URLs internes des templates mises à jour)
- `app/histoire_geo_emc/reperes/**` (tout nouveau)
- `content/histoire-geo-emc/reperes/_all.json`
- `scripts/extract_reperes.py`
- `app/core/main.py` (2 imports + 1 appel init_reperes)
- `app/core/db.py` (Session.subject_kind + nullable subject_id)
- `app/core/templates/home.html` (texte carte HG-EMC)

**Non touché** (territoire français) : `app/francais/**`,
`content/francais/**`, `scripts/extract_french_exercises.py`, et
`app/core/albert_client.py` (qui a des modifs `Task.FR_COMP_*`
attendues côté français).

---

*Fin du handoff. Fichier généré automatiquement — ne pas éditer à la main
sauf pour ajouter des notes de passation additionnelles.*
