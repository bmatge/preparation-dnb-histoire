"""Prompts LLM de l'épreuve « Automatismes » (mathématiques DNB).

Quatre builders, deux modèles cibles côté Albert (cf.
`app.core.albert_client.Task` :: `MATH_AUTO_*`) :

1. `build_hint_prompt`        : indice gradué (Mistral-Small).
2. `build_reveal_prompt`      : révélation pédagogique (Mistral-Small).
3. `build_open_eval_prompt`   : évaluation d'une réponse ouverte courte
                                (gpt-oss-120b, JSON strict).
4. `build_synthese_fallback`  : (réservé V2) synthèse de fin de session.

**Règle cardinale héritée des repères HG** : la bonne réponse EST
l'objectif d'apprentissage. Les indices peuvent (et doivent) finir par
révéler la réponse. Pas de risque de ghostwriting (réponses courtes).

Pas de RAG en V1 sur les indices et les révélations : chaque question
porte tout ce qu'il faut au modèle. Le RAG (méthodo + programme) est
utilisé seulement pour l'évaluation ouverte (`MATH_AUTO_EVAL_OPEN`)
côté `pedagogy.py`.
"""

from __future__ import annotations

import random

from app.mathematiques.automatismes import models as auto_models


# ============================================================================
# Persona commun
# ============================================================================


SYSTEM_PERSONA = """Tu es un tuteur bienveillant qui aide un·e élève de 3e à réviser ses automatismes de mathématiques pour le DNB.

RÈGLES GÉNÉRALES :
- Tu t'adresses à l'élève en le tutoyant.
- Ton ton est chaleureux, encourageant, jamais moqueur ni condescendant.
- Tes messages sont COURTS (1 à 3 phrases max).
- Vocabulaire simple, mais terminologie mathématique correcte (pas « le truc qui multiplie » mais « le coefficient »).
- Pas d'emojis, pas de formules type « Bien sûr ! » ou « Absolument ! ».
- Tu réponds uniquement avec le message destiné à l'élève, rien d'autre.

FORMAT DES MATHS (très important) :
- Écris TOUJOURS les maths en texte brut, jamais en LaTeX.
- Utilise les symboles Unicode : × (multiplication), ÷ ou / (division), π (pi), ² ³ (carré, cube), √ (racine), ≈ (environ), ≤ ≥ (inégalités), ° (degré).
- Les fractions s'écrivent « a/b » (par exemple « 3/4 »).
- Les puissances s'écrivent « 2³ » ou « 10^3 » si l'exposant est long.
- N'utilise JAMAIS \\times, \\pi, \\frac, \\sqrt, ni aucune commande commençant par un backslash.
- N'entoure JAMAIS les formules de $...$, \\(...\\) ou \\[...\\]."""


# ============================================================================
# Sérialisation question → bloc compact pour les prompts
# ============================================================================


def _format_question(question: auto_models.AutoQuestion) -> str:
    """Sérialise une question pour injection dans un prompt."""
    scoring = question.scoring or {}
    type_rep = scoring.get("type_reponse") or scoring.get("mode") or "?"
    rep = scoring.get("reponse_canonique") or scoring.get("reponse_modele") or "?"

    lines = [
        f"theme: {question.theme}",
        f"competence: {question.competence}",
        f"enonce: {question.enonce}",
        f"type_reponse: {type_rep}",
        f"reponse_attendue: {rep}",
    ]
    if scoring.get("unite"):
        lines.append(f"unite: {scoring['unite']}")
    return "\n".join(lines)


# ============================================================================
# 1. Indice gradué
# ============================================================================


HINT_LEVEL_GUIDELINES = {
    1: (
        "Niveau 1 (large) : rappelle l'opération ou la propriété mathématique en jeu, "
        "sans poser le calcul. Pas de chiffre de l'énoncé. L'élève doit encore réfléchir."
    ),
    2: (
        "Niveau 2 (ciblé) : pose la première étape du calcul (par exemple « commence par "
        "convertir », « écris a/b sous forme décimale », « pose l'opération »), mais "
        "ne donne PAS le résultat de cette étape."
    ),
    3: (
        "Niveau 3 (quasi-réponse) : donne le résultat intermédiaire de l'étape clé, "
        "ou le premier chiffre de la réponse. Tu peux presque tout dire, mais laisse "
        "l'élève finaliser. Ne donne PAS encore la réponse complète en clair."
    ),
}


def build_hint_prompt(
    question: auto_models.AutoQuestion,
    hint_level: int,
    previous_answers: list[str],
) -> list[dict]:
    """Indice gradué adapté au niveau demandé.

    Si la question porte un indice pré-calculé (champ `indices.niveau_X`),
    on l'injecte comme suggestion préférentielle, mais on demande quand
    même au modèle de le reformuler en français de tuteur (rendu plus
    naturel et adapté aux réponses précédentes).
    """
    guideline = HINT_LEVEL_GUIDELINES.get(hint_level, HINT_LEVEL_GUIDELINES[1])

    indices = question.indices or {}
    pre_hint = indices.get(f"niveau_{hint_level}")
    pre_hint_block = ""
    if pre_hint:
        pre_hint_block = (
            f"\n\nIndice préparé en amont (à reformuler à ta manière) :\n"
            f"« {pre_hint} »\n"
        )

    previous_block = ""
    if previous_answers:
        tries = "\n".join(f"- {a}" for a in previous_answers[-3:])
        previous_block = (
            f"\n\nRéponses déjà tentées par l'élève (incorrectes) :\n{tries}\n"
            "Ne ridiculise jamais une erreur."
        )

    user_msg = f"""L'élève bloque sur cette question d'automatismes :

<question>
{_format_question(question)}
</question>
{previous_block}{pre_hint_block}

Tu dois lui donner un INDICE, pas la réponse.

{guideline}

Contraintes :
- 1 à 2 phrases maximum.
- Tutoiement, ton bienveillant.
- Ne donne JAMAIS la réponse en clair (sauf au niveau 3 où tu peux donner un résultat intermédiaire).
- Pas de préambule type « Voici un indice : ».

Écris l'indice maintenant."""

    return [
        {"role": "system", "content": SYSTEM_PERSONA},
        {"role": "user", "content": user_msg},
    ]


# ============================================================================
# 2. Révélation de la réponse
# ============================================================================


def build_reveal_prompt(question: auto_models.AutoQuestion) -> list[dict]:
    """Message de révélation bienveillant + mini-explication."""
    scoring = question.scoring or {}
    rep = (
        scoring.get("reponse_canonique")
        or scoring.get("reponse_modele")
        or "(réponse non trouvée dans la fiche)"
    )
    unite = scoring.get("unite")
    rep_label = f"{rep} {unite}" if unite else rep

    pre_explication = question.reveal_explication or ""
    pre_block = ""
    if pre_explication:
        pre_block = (
            f"\n\nExplication préparée en amont (à reformuler à ta manière) :\n"
            f"« {pre_explication} »\n"
        )

    user_msg = f"""L'élève n'a pas trouvé la réponse. Tu dois la lui révéler avec bienveillance et expliquer rapidement la méthode.

<question>
{_format_question(question)}
</question>
{pre_block}

Bonne réponse à annoncer : **{rep_label}**

Contraintes :
- Commence par une formule courte et bienveillante (« Pas grave, on regarde ensemble. »).
- Donne la bonne réponse clairement.
- Ajoute UNE phrase d'explication sur la méthode (l'opération clé, la propriété, l'astuce mentale).
- 2 à 3 phrases maximum.
- Tutoiement.

Écris le message maintenant."""

    return [
        {"role": "system", "content": SYSTEM_PERSONA},
        {"role": "user", "content": user_msg},
    ]


# ============================================================================
# 3. Évaluation d'une réponse ouverte (mode scoring=albert)
# ============================================================================


def build_open_eval_prompt(
    question: auto_models.AutoQuestion,
    student_answer: str,
    rag_passages: list | None = None,
) -> list[dict]:
    """Évalue une réponse ouverte courte. Albert répond en JSON strict.

    Format de sortie attendu :
        {"correct": true|false, "feedback_court": "..."}

    Le `feedback_court` reste très court (1 phrase) et peut citer
    `[methodo]` ou `[programme]` si le RAG a remonté quelque chose.
    """
    scoring = question.scoring or {}
    reponse_modele = scoring.get("reponse_modele") or "(non précisée)"
    criteres = scoring.get("criteres_validation") or []
    criteres_block = ""
    if criteres:
        bullets = "\n".join(f"- {c}" for c in criteres)
        criteres_block = f"\n\nCritères de validation :\n{bullets}"

    context_block = ""
    if rag_passages:
        chunks = []
        for p in rag_passages[:3]:
            label = getattr(p, "source", "?")
            content = getattr(p, "content", "")
            if content:
                chunks.append(f"[{label}] {content[:600]}")
        if chunks:
            context_block = (
                "\n\n<context>\n" + "\n\n".join(chunks) + "\n</context>"
            )

    user_msg = f"""Tu évalues une réponse d'élève à une question d'automatismes mathématiques.

<question>
enonce: {question.enonce}
theme: {question.theme}
reponse_modele: {reponse_modele}
</question>{criteres_block}{context_block}

<copie_eleve>
{student_answer.strip()}
</copie_eleve>

Tâche :
1. Décide si la réponse de l'élève est acceptable au regard des critères et de la réponse modèle.
2. Sois souple sur la forme (formulation, ordre des arguments) tant que le contenu est juste.
3. Cite la source [methodo] ou [programme] dans `feedback_court` UNIQUEMENT si tu t'appuies sur un passage du <context> ci-dessus.

Réponds UNIQUEMENT avec un objet JSON, sans markdown, sans préambule, au format strict suivant :
{{"correct": true, "feedback_court": "Une phrase au tutoiement, ≤ 25 mots."}}

ou

{{"correct": false, "feedback_court": "Une phrase au tutoiement, ≤ 25 mots."}}
"""

    return [
        {"role": "system", "content": SYSTEM_PERSONA},
        {"role": "user", "content": user_msg},
    ]


# ============================================================================
# 4. Feedbacks positifs (cosmétique, pas d'Albert)
# ============================================================================


POSITIVE_FEEDBACKS = [
    "Bien joué, c'est ça.",
    "Exact — tu l'avais.",
    "Oui, parfait.",
    "C'est ça, bravo.",
    "Bonne réponse.",
    "Nickel.",
]


def random_positive_feedback() -> str:
    return random.choice(POSITIVE_FEEDBACKS)


__all__ = [
    "SYSTEM_PERSONA",
    "build_hint_prompt",
    "build_reveal_prompt",
    "build_open_eval_prompt",
    "random_positive_feedback",
]
