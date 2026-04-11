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

---

## ADDENDUM 2026-04-10 — Workstream français compréhension (PR #4, #7, #9)

Une matière complète ajoutée à la plateforme le même jour que l'addendum
HG-repères. Trois PR mergées en séquence sur `main` :

- **PR #4** `feature/francais-comprehension` — MVP compréhension (268 items)
- **PR #7** `feature/francais-grammaire-reecriture` — grammaire + réécriture (+255 items)
- **PR #9** `feature/francais-rag-methodo` — RAG programme + méthodo actif

### État final du workstream

```
app/francais/
  __init__.py
  routes.py                        # router racine matière : /francais/
  templates/index.html             # liste des sous-épreuves (1 active, 2 placeholders)
  comprehension/
    routes.py, pedagogy.py, prompts.py, models.py, loader.py
    templates/
      home.html, exercise.html, synthese.html
      _partials/feedback.html, hint.html, reveal.html

content/francais/
  comprehension/
    annales/*.pdf                  # 38 sujets bruts 2018-2025, tous centres
    exercises/*.json               # 38 JSON extraits + _all.json
  programme/                       # 5 PDF (cycle 4 + attendus 3e/4e/5e + repères)
  methodologie/                    # 9 PDF (8 fiches + cadrage épreuve)
  notation/                        # grilles officielles

scripts/
  extract_french_exercises.py      # extraction Opus multimodal (offline, idempotent)
  ingest.py                        # étendu avec fr_programme / fr_methodo
                                   # + flag --matiere qui compose avec --only
```

### Contenu du corpus

- **38 sujets d'annales** (compréhension + grammaire + réécriture) couvrant
  2018-2025, tous centres d'examen. Extraction via Opus multimodal :
  2 sujets pilotes via le script + API Anthropic, 36 via 4 sous-agents
  Claude Code en parallèle pour économiser les crédits API.
- **523 items MVP** exploitables dans `flatten_items()` :
  230 compréhension + 255 grammaire + 38 réécriture.
- **51 items « texte_image »** filtrés par défaut (voir issue #10 pour la
  réactivation : `include_image_questions=True` + rendu côté UI à
  implémenter).
- Barèmes observés : 32/18 (25 sujets), 30/20 (10), 26/24 (2), 30/18 (1 —
  le 2019 Amérique du Nord fait 48 pts total, anomalie du sujet officiel).
- Deux sujets à re-vérifier manuellement : 2019_amerique-nord (48 pts) et
  2025_nouvelle-caledonie (numérotation grammaire qui saute Q6→Q9, total
  30 au lieu de 32 annoncés).

### Pédagogie français compréhension

Règle cardinale identique au DC : l'IA ne donne jamais la bonne réponse.
Trois niveaux d'indice gradués, puis révélation si l'élève bloque après
le 3e indice.

Trois variantes de prompts dans `app/francais/comprehension/prompts.py` :

1. **Générique** (compréhension + grammaire) : `build_first_eval`,
   `build_hint`, `build_reveal_answer`. La grammaire hérite de ces
   builders parce que le pattern question-par-question est le même et
   que le prompt générique s'adapte à la compétence via le champ
   `item.competence` injecté dans la balise `<question>`.
2. **Réécriture** : `build_reecriture_eval`, `build_reecriture_hint`,
   `build_reecriture_reveal`. Grille d'évaluation totalement différente —
   on vérifie contrainte par contrainte qu'une transformation mécanique a
   été appliquée, et que toutes les concordances (accords, temps, pronoms,
   participes passés) ont été propagées. Les 3 niveaux d'indice gradués
   sont spécifiques : niveau 1 = rappel de contrainte non respectée,
   niveau 2 = catégorie grammaticale de l'erreur, niveau 3 = désignation
   d'un mot précis à revoir sans donner la forme corrigée.
3. **Synthèse de fin de session** : `build_session_synthese` — bilan
   encourageant, nomme 1-2 compétences à retravailler, propose une fiche
   méthodo concrète à relire.

Le dispatch vers la variante réécriture se fait dans
`app/francais/comprehension/pedagogy.py` via `if item.type == "reecriture"`.
Les 3 fonctions publiques `evaluate_answer`, `generate_hint` et
`reveal_answer` gardent une signature unique, la pédagogie reste
transparente côté routes.

### RAG programme + méthodo

Deux nouvelles collections Albert, créées et ingérées le 2026-04-10 :

- `dnb_francais_programme` — id 184943, 5 documents (programme cycle 4
  BO 2020, attendus fin 3e/4e/5e, repères progression)
- `dnb_francais_methodo` — id 184944, 9 documents (fiches 1-8 + cadrage
  épreuve)

Allocation des tâches (`app/core/rag.py::TASK_COLLECTIONS`) :

- `FR_COMP_EVAL` → méthodo uniquement
- `FR_COMP_HINT` → méthodo uniquement
- `FR_COMP_REVEAL` → méthodo + programme (règle + autorité officielle)
- `FR_COMP_SYNTHESE` → programme + méthodo (pour renvoyer à un attendu
  ou une fiche concrète à retravailler)

La requête sémantique est construite par `_build_rag_query(item)` qui
concatène `{compétence} — {énoncé}`. La compétence en première position
améliore le match sur les fiches méthodo organisées thématiquement.

Gestion d'erreur : `_search_rag()` catch tout et retourne `[]` en cas
d'échec (réseau, collection manquante, timeout...). Les builders acceptent
`passages=None` et injectent alors un placeholder neutre dans la balise
`<context>`. **L'app reste fonctionnelle sans RAG**, juste moins ancrée
dans les sources officielles.

### Ré-ingérer les collections françaises si besoin

```bash
source .env
.venv/bin/python -m scripts.ingest --matiere francais           # tout le français
.venv/bin/python -m scripts.ingest --only fr_programme          # programme seul
.venv/bin/python -m scripts.ingest --only fr_methodo            # méthodo seule
.venv/bin/python -m scripts.ingest --matiere francais --force   # re-push total
.venv/bin/python -m scripts.ingest --matiere francais --dry-run # simule
```

Idempotence SHA256 dans `data/ingest_state.db` — un re-run skip les
14 fichiers inchangés.

### Ré-extraire un sujet de compréhension si besoin

```bash
source .env
.venv/bin/python -m scripts.extract_french_exercises content/francais/comprehension/annales/
.venv/bin/python -m scripts.extract_french_exercises content/francais/comprehension/annales/2019_Amerique-Nord_francais_questions-grammaire-comp.pdf --force
```

Le script appelle l'API Anthropic directement (Claude Opus multimodal).
Pour économiser les crédits API, la plupart des sujets du corpus initial
ont été extraits via des sous-agents Claude Code (outil Read sur PDF +
outil Write sur JSON) — voir l'historique de la PR #4 pour le pattern.

### Sous-épreuves non implémentées (issues ouvertes)

- **#5 Dictée** — priorité moyenne. Design TTS à trancher : Web Speech
  API côté navigateur (gratuit, qualité variable) vs TTS serveur local
  (meilleur, dépendance supplémentaire dans Docker) vs TTS externe
  (exclu, viole la règle « pas d'appel externe en runtime »).
- **#6 Rédaction** — priorité haute (40 pts sur 50). Session dédiée à
  prévoir. Réutilisation massive du pattern 7 étapes du DC HG-EMC.
  Nouveau corpus à extraire (les sujets de rédaction, distincts des
  sujets de compréhension).
- **#10 Images** — priorité basse/moyenne. 51 items `texte_image` à
  débloquer une fois l'extraction et le rendu d'images opérationnels.
  Recommandation : extraction PNG offline via pdfplumber vers
  `content/francais/comprehension/images/<slug>.png`, rendu simple dans
  `exercise.html` via `<img>` classique.

### Fichiers 100 % à moi (workstream français)

Cohérents avec les 3 PR mergées :
- `app/francais/**` (tout nouveau)
- `content/francais/**` (tout nouveau)
- `scripts/extract_french_exercises.py` (tout nouveau)
- `scripts/ingest.py` (modifié : +francais specs + --matiere flag)
- `app/core/rag.py` (modifié : +francais_comprehension entries)
- `app/core/albert_client.py` (modifié : +4 tâches FR_COMP_*)
- `app/core/main.py` (modifié : include_router francais + init_french_comprehension)
- `app/core/templates/home.html` (modifié : carte Français)

### Nettoyages ponctuels

- `.claude/settings.json` commité avec autorisation wildcard `Bash(*)`
  (PR #11) — les futures sessions Claude Code ne redemandent plus la
  confirmation des commandes Bash.
- 22 doublons « fichier 2.ext » supprimés pendant le workstream (outil de
  sync macOS, pattern find/rm documenté dans la mémoire Claude Code).
  Seul le PDF méthodo légitime `Developpement construit DNB histographie 2.pdf`
  est préservé.

---

## ADDENDUM 2026-04-11 — Mathématiques / épreuve Automatismes (PR feature/math-automatismes)

### Périmètre livré

Première matière maths sur la plateforme. Une seule épreuve active :
**Automatismes** (20 min sans calculatrice, ~10 questions courtes,
format DNB 2026). L'épreuve « Raisonnement et résolution de problèmes »
reste un placeholder visible dans `app/mathematiques/templates/index.html`,
en attendant la décision sur le rendu des figures.

URLs exposées :

  GET  /mathematiques/                                accueil matière
  GET  /mathematiques/automatismes/                   accueil épreuve
  POST /mathematiques/automatismes/quiz/new           création quiz
  GET  /mathematiques/automatismes/quiz               question courante
  POST /mathematiques/automatismes/quiz/answer        évaluation
  POST /mathematiques/automatismes/quiz/hint          indice gradué
  POST /mathematiques/automatismes/quiz/reveal        révélation explicite
  GET  /mathematiques/automatismes/quiz/synthese      bilan
  GET  /mathematiques/automatismes/restart            efface l'état

### Structure du module `app/mathematiques/`

```
app/mathematiques/
  __init__.py            # SUBJECT_KIND = "mathematiques"
  routes.py              # router racine /mathematiques/
  templates/index.html   # liste épreuves (1 active + 2 placeholders)
  automatismes/
    __init__.py
    routes.py            # 8 endpoints HTTP
    models.py            # Pydantic Question + SQLModel AutoQuestion/AutoAttempt
    scoring.py           # check() déterministe : entier/decimal/fraction/%
    pedagogy.py          # dispatch python/albert + _safe_chat
    prompts.py           # 3 builders (hint, reveal, eval ouverte) + persona
    loader.py            # THEME_LABELS + pick_for_quiz
    templates/
      home.html          # accueil épreuve + sélecteur thème/longueur
      quiz.html          # une question, formulaire HTMX
      synthese.html      # bilan + questions à retravailler
      _partials/feedback.html  # variantes correct/incorrect/hint/revealed/error
```

### Pédagogie hybride

- **Scoring déterministe Python** (mode `python`) pour ~95 % du corpus :
  parsers `normalize_number` / `normalize_fraction` / `normalize_percentage`
  + tolérances par type (entier 0, décimal ±0.01, pourcentage ±0.5,
  fraction stricte). `formes_acceptees` court-circuite le parser quand
  on veut valider une forme exacte (« x = 5 », « 2,5 m »).

- **Scoring Albert** (mode `albert`) pour les questions ouvertes courtes
  (cosinus en fonction des côtés, notation scientifique, factorisation).
  Le builder `build_open_eval_prompt` force un JSON strict
  `{"correct": bool, "feedback_court": str}`. Évalué par
  `Task.MATH_AUTO_EVAL_OPEN` (gpt-oss-120b, max_tokens 800).

- **Indices gradués** (3 niveaux) via `Task.MATH_AUTO_HINT` (Mistral-Small).
  Si Albert plante, fallback déterministe basé sur le type de réponse
  attendu et le premier caractère de la réponse canonique.

- **Révélation** via `Task.MATH_AUTO_REVEAL` + fallback déterministe avec
  la réponse canonique brute.

- **Règle cardinale héritée des repères HG** : la bonne réponse EST
  l'objectif d'apprentissage. Pas de risque de ghostwriting (réponses
  courtes), `check_no_ghostwriting=False` partout côté math.

### Corpus committé : 175 questions, 8 thèmes

Pipeline 100 % offline en session Claude Code (Read PDF + Write JSON).
Aucun appel à l'API Anthropic.

| Thème                              | # questions |
|------------------------------------|-------------|
| calcul_numerique                   | 22          |
| calcul_litteral                    | 21          |
| fractions                          | 22          |
| pourcentages_proportionnalite      | 25          |
| stats_probas                       | 21          |
| grandeurs_mesures                  | 20          |
| geometrie_numerique                | 27          |
| programmes_calcul                  | 17          |
| **TOTAL**                          | **175**     |

Dont **19 questions extraites des sujets zéro DNB 2026 officiels** (sujet
A et sujet B — le PDF Sujet_zero_DNB_2026_maths.pdf est identique au
sujet A et n'a pas été ré-extrait pour éviter les doublons). La cible
initiale ≥ 20 sujets zéro a été ajustée à 19 unique : seules 18 questions
distinctes existent dans les 3 sujets zéro publiés, plus 1 question
issue d'un découpage utile (auto_sjz_A_q9 → q9a + q9b pour les deux
paramètres du programme Scratch).

Layout :

```
content/mathematiques/
  automatismes/
    sujets_zero/                           # 3 PDF officiels (DV : doublon maths/A)
    Liste_automatismes_DNB_2026.pdf
    Liste_indicative_automatismes_DNB.pdf  # ancienne version (référence)
    _liste_officielle.json                 # liste normalisée pour ancrage
    questions/                             # banque chargée par init_automatismes
      sujets_zero.json                     # 19 questions extraites
      calcul_numerique.json                # 22 générées
      calcul_litteral.json                 # 20 générées
      fractions.json                       # 20 générées
      pourcentages_proportionnalite.json   # 22 générées
      stats_probas.json                    # 18 générées
      grandeurs_mesures.json               # 18 générées
      geometrie_numerique.json             # 22 générées
      programmes_calcul.json               # 14 générées
  programme/                               # 5 PDF (programme cycle 4 + attendus + repères)
  methodologie/                            # 11 PDF (8 fiches + automatismes/cadrage/évaluation)
```

**Convention loader** : `init_automatismes` lit tous les `*.json` du
dossier `questions/` *sauf* ceux qui commencent par `_` (méta-fichiers
type `_liste_officielle.json`, ou aggrégat legacy `_all.json`).

### Collections Albert

Trois collections ajoutées dans `scripts/ingest.py` (`COLLECTIONS` +
`MATIERE_COLLECTIONS["mathematiques"]`) :

| Clé ingest      | Collection Albert                  | Source                                |
|-----------------|------------------------------------|---------------------------------------|
| `math_programmes` | `dnb_math_programmes`             | content/mathematiques/programme/      |
| `math_methodo`    | `dnb_math_methodo`                | content/mathematiques/methodologie/   |
| `math_sujets`     | `dnb_math_automatismes_sujets`    | questions/*.json (md à la volée)      |

Conversion JSON → markdown spécifique côté math via
`_math_questions_json_to_markdown` : un bloc par question avec énoncé,
réponse attendue/modèle et critères de validation pour les questions
ouvertes. Les indices et `reveal_explication` sont volontairement
**omis** de l'indexation (Albert les régénère côté runtime).

`FALLBACK_COLLECTION_IDS["mathematiques"]` est laissé vide (`{}`) en
attendant le premier run de `python -m scripts.ingest --matiere mathematiques`
qui créera les 3 collections côté Albert et permettra de copier les IDs
ici. Le client RAG résout les IDs via `/v1/collections` à chaud, donc le
fallback ne sert qu'en cas d'incident sur cette API.

**À faire après le merge** : lancer
`python -m scripts.ingest --matiere mathematiques` (`--dry-run` validé,
25 fichiers candidats) et reporter les IDs générés dans
`FALLBACK_COLLECTION_IDS["mathematiques"]`.

### Tests (vague 1)

187 nouveaux tests dans `tests/mathematiques/automatismes/` +
`tests/corpus/test_corpus_validation.py` (étendu) :

- `test_scoring.py` (153 tests) : 4 types numériques + texte_court avec
  ≥ 20 cas par type. Couvre virgule FR, fraction non simplifiée,
  pourcentage avec/sans `%`, formes_acceptees prioritaires, modes inconnus.
- `test_loader.py` (10 tests) : `init_automatismes` charge ≥ 150
  questions, idempotent, couvre les 8 thèmes officiels, ≥ 10 questions
  par thème (pour qu'un quiz de 10 sur thème unique tourne). `pick_for_quiz`
  filtre/respecte n, ne crash pas si n trop grand.
- `test_routes.py` (12 tests) : smoke HTTP via `TestClient` sur les
  8 endpoints, parcours start → question → answer → hint → reveal →
  synthese → restart, avec `FakeAlbertClient` mocké via la fixture
  `test_client` du conftest racine (étendu).
- `test_corpus_validation.py` (étendu, +13 tests) : valide chaque batch
  contre `Question` Pydantic, vérifie taille corpus ≥ 150, slugs uniques,
  ≥ 15 questions sujets zéro avec source.type = `sujet_zero_officiel`.

Suite complète au moment du commit : **368 tests passent** (181
existants + 187 maths).

### Validation E2E manuelle (uvicorn local)

Effectuée le 2026-04-11 sur `http://127.0.0.1:8767/mathematiques/automatismes/` :

1. `GET /mathematiques/` → 200, page d'index matière OK
2. `GET /mathematiques/automatismes/` → 200, formulaire avec sélecteur thèmes
3. `POST /quiz/new theme=fractions length=5` → 303 → quiz
4. `GET /quiz` → 200, première question affichée avec le bon label thème
5. `POST /quiz/answer answer=` → fragment "Écris une réponse"
6. `POST /quiz/answer answer=999` → fragment ✗ (incorrect)
7. `POST /quiz/hint` → fragment "Indice 1/3" (fallback déterministe car
   pas de clé Albert valide en dev)
8. `POST /quiz/reveal` → fragment "Pas grave… bonne réponse"
9. `GET /quiz/synthese` → 200, page de bilan

Logs startup : `175 questions automatismes maths chargées` après
`rm -f data/app.db` (idempotent : second startup → même comptage).

### Fichiers 100 % à moi (workstream automatismes maths)

- `app/mathematiques/**` (nouveau, 11 fichiers Python + 5 templates)
- `content/mathematiques/**` (nouveau, dont 9 fichiers JSON questions +
  1 `_liste_officielle.json` + 24 PDF copiés depuis `RevisionDNB/`)
- `tests/mathematiques/**` (nouveau, 3 fichiers test_*.py)
- `app/core/main.py` (modifié : import + include_router + init_automatismes
  dans on_startup)
- `app/core/albert_client.py` (modifié : +3 tâches MATH_AUTO_*)
- `app/core/rag.py` (modifié : +entries `mathematiques` dans
  COLLECTION_LABELS, TASK_COLLECTIONS, FALLBACK_COLLECTION_IDS)
- `app/core/templates/home.html` (modifié : carte Mathématiques
  remplace le placeholder « Bientôt »)
- `app/core/templates/base.html` (modifié : entrée « Maths » dans le
  menu matières)
- `scripts/ingest.py` (modifié : +3 specs math + converter
  `_math_questions_json_to_markdown` + `EXCLUDED_PREFIXES` pour ignorer
  les méta-fichiers `_*.json`)
- `tests/conftest.py` (modifié : import `_math_auto_models` pour
  enregistrer les tables, monkeypatch du singleton `_albert_client` côté
  math dans la fixture `test_client`)
- `tests/corpus/test_corpus_validation.py` (étendu : 13 tests
  automatismes maths)

### Points d'attention pour la suite

1. **IDs collections Albert** à reporter dans
   `FALLBACK_COLLECTION_IDS["mathematiques"]` après le premier `ingest`.
2. La règle « ≥ 20 sujets zéro » de l'issue est ajustée à 19 (les 3
   sujets zéro publiés en contiennent 18 distincts ; un découpage utile
   amène à 19).
3. Le scoring `texte_court` accepte une comparaison sans-espaces en
   dernier recours (« 8x » == « 8 x ») pour les expressions littérales
   courtes. Validé sur 153 cas, mais à surveiller si on étend le type à
   des phrases plus longues.
4. Les questions à scoring Albert n'ont pas de retry automatique sur
   citations manquantes (le builder ne demande des citations que si le
   RAG a remonté des passages, et le fallback retourne `False` si Albert
   plante — l'élève est juste marqué « pas trouvé »).
5. Les sujets zéro qui dépendent d'une figure (Q4/A abscisse, Q4/B
   graphique, Q7/A et Q8/B Thalès, Q9/A Scratch, Q9/B algorithme) ont
   été reformulés pour être lisibles sans image. Toutes les valeurs
   numériques d'origine sont préservées.

---

## ADDENDUM 2026-04-11 — Pattern « Bouton Outils » par matière (FAB)

Depuis la PR #26, la matière mathématiques expose un bouton flottant
**« Outils »** en bas à droite de toutes ses pages, qui déplie une mini
popin contenant la calculette. Le pattern est réutilisable tel quel
pour ajouter un outil matière-spécifique côté français (dictionnaire,
définitions…) ou histoire-géo (chronologie, repère…).

### Architecture

- `app/core/templates/base.html` expose un block Jinja vide en fin de
  `<body>` :
  ```jinja
  {% block fab %}{% endblock %}
  ```
  Ce block est le **point d'injection transverse** : par défaut rien
  n'est rendu, chaque matière décide ou non de le remplir.

- Chaque matière qui veut son propre FAB crée **deux fichiers** dans
  `app/<matière>/templates/` :
  1. `_<matière>_base.html` — layout intermédiaire qui étend
     `base.html` et remplit le block `fab` avec un include.
  2. `_tools_fab.html` — le contenu effectif (bouton + popin + logique).

- **Tous les templates de la matière étendent `_<matière>_base.html`**
  au lieu de `base.html`. Une seule ligne à changer par template.

### Recette pour ajouter un FAB à une nouvelle matière

1. **Créer `_<matière>_base.html`** (exemple français) :
   ```jinja
   {% extends "base.html" %}

   {% block fab %}
     {% include "_tools_fab.html" %}
   {% endblock %}
   ```

2. **Créer `_tools_fab.html`** — reprendre la structure HTML de
   `app/mathematiques/templates/_tools_fab.html` comme point de départ
   (bouton rond `fixed bottom-5 right-5 z-50`, popin dépliante au clic,
   fermeture via croix / Échap / clic hors-popin). Remplacer le bloc
   formulaire et le `<script>` par ce qui est spécifique à la matière.
   Le corps du popin est totalement libre : JS vanilla, HTMX vers une
   route `/français/outils/definition`, contenu statique…

3. **Faire pointer les templates de la matière vers le nouveau layout**.
   Pour chaque template qui affiche une page (`index.html`, `home.html`,
   `step_*.html`, `quiz.html`, etc.), remplacer :
   ```jinja
   {% extends "base.html" %}
   ```
   par :
   ```jinja
   {% extends "_<matière>_base.html" %}
   ```

4. **Exposer `_<matière>_TEMPLATES` aux Jinja2Templates des
   sous-épreuves**. Chaque sous-épreuve a son propre
   `Jinja2Templates(directory=[...])` dans `routes.py`. Il faut y
   ajouter le dossier de templates de la matière racine, sinon
   `_<matière>_base.html` et `_tools_fab.html` ne seront pas
   résolvables.

   Exemple côté maths (pattern à reproduire pour français / HG) :
   ```python
   _HERE = Path(__file__).resolve().parent
   _APP_DIR = _HERE.parent.parent
   _CORE_TEMPLATES = _APP_DIR / "core" / "templates"
   _MATH_TEMPLATES = _HERE.parent / "templates"  # <-- ajout
   _AUTO_TEMPLATES = _HERE / "templates"
   templates = Jinja2Templates(
       directory=[str(_AUTO_TEMPLATES), str(_MATH_TEMPLATES), str(_CORE_TEMPLATES)]
   )
   ```

### Gotchas à connaître

- **Ordre des directories Jinja** : la sous-épreuve d'abord, puis la
  matière racine, puis core. Jinja cherche dans l'ordre — si deux
  partiels portent le même nom, c'est le plus spécifique qui gagne.

- **Ne pas oublier un template** : en maths il y a 7 templates qui
  étendent `_maths_base.html` (index matière + 3 templates automatismes
  + 3 templates problèmes). Oublier un seul fichier = page sans FAB.
  Les tests `tests/mathematiques/test_tools_fab.py` vérifient la
  présence du marqueur `id="math-fab-root"` sur chaque type de page et
  son absence sur les autres matières — **copier ce pattern de test**
  pour valider le nouvel outil côté français / HG.

- **Le block est positionné *avant* `</body>`**, donc il reste visible
  au-dessus des formulaires HTMX sans interférer avec eux (`z-50` sur
  le conteneur racine du FAB).

- **Content Security Policy** : `_tools_fab.html` utilise un
  `<script>` inline. Si une CSP stricte est introduite plus tard, il
  faudra déplacer les handlers dans un fichier statique dédié
  (`app/static/<matière>_tools_fab.js`) et ajouter un `<script src="…">`
  — pas bloquant pour l'instant, mais à garder en tête.

- **Pas de persistance côté serveur** dans l'exemple maths : la
  calculette est 100 % client-side. Pour un outil qui appellerait
  Albert (p. ex. une définition côté français), préférer une route
  HTMX POST qui rend un fragment à injecter dans la popin — éviter
  `fetch` à la main si on peut, pour rester cohérent avec le reste du
  projet.

### Idées de contenu par matière

- **Français** : lookup d'une définition (RAG contre
  `dnb_francais_methodo` ou un dictionnaire committé), conjugaison d'un
  verbe, rappel des règles d'accord de base.
- **Histoire-géo-EMC** : recherche d'un repère chronologique ou spatial
  par mot-clé, conversion d'une date en siècle, rappel d'un personnage.

Dans tous les cas : le pattern fournit **uniquement le container
(bouton + popin)**, la logique interne est libre. Inspiration pour le
style : gradient `from-brand-500 to-purple-600` en header du popin,
`rounded-2xl shadow-2xl`, fermeture par Échap / clic extérieur.

### Fichiers de référence côté maths

- `app/core/templates/base.html` — block `fab` en fin de body.
- `app/mathematiques/templates/_maths_base.html` — layout maths.
- `app/mathematiques/templates/_tools_fab.html` — calculette JS
  (bouton + popin + whitelist stricte + historique).
- `app/mathematiques/{automatismes,problemes}/routes.py` — exemple de
  Jinja2Templates avec `_MATH_TEMPLATES` dans la liste.
- `tests/mathematiques/test_tools_fab.py` — pattern de test à recopier.

---

*Fin du handoff. Fichier généré automatiquement — ne pas éditer à la main
sauf pour ajouter des notes de passation additionnelles.*
