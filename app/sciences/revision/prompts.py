"""Prompts LLM de l'épreuve Révision sciences.

Trois builders, deux modèles Albert (cf. `app.core.albert_client.Task` ::
`SCIENCES_REV_*`) :

1. `build_hint_prompt`      : indice gradué (Mistral-Small).
2. `build_reveal_prompt`    : révélation pédagogique (Mistral-Small).
3. `build_open_eval_prompt` : évaluation d'une réponse ouverte courte
                              (gpt-oss-120b, JSON strict).

Règle cardinale : la bonne réponse EST l'objectif d'apprentissage. Les
indices peuvent et doivent finir par révéler la réponse au niveau 3.
Pas de ghostwriting possible (réponses courtes).

Le RAG (méthodo + programme + annales) est utilisé uniquement pour
l'évaluation ouverte (`SCIENCES_REV_EVAL_OPEN`) côté `pedagogy.py`. Les
indices et les révélations sont générés à partir du contenu de la
question seule, pour rester rapides et ne pas rendre les collections
RAG indispensables à la V1.
"""

from __future__ import annotations

import random

from app.sciences.revision import models as science_models
from app.sciences.revision.loader import DISCIPLINE_LABELS, THEME_LABELS


# ============================================================================
# Persona commun
# ============================================================================


SYSTEM_PERSONA = """Tu es un tuteur bienveillant qui aide un·e élève de 3e à réviser ses cours de sciences (Physique-Chimie, SVT, Technologie) pour le DNB.

RÈGLES GÉNÉRALES :
- Tu t'adresses à l'élève en le tutoyant.
- Ton ton est chaleureux, encourageant, jamais moqueur ni condescendant.
- Tes messages sont COURTS (1 à 3 phrases max).
- Vocabulaire simple, mais terminologie scientifique correcte (pas « le truc qui bouge » mais « le mobile »).
- Pas d'emojis, pas de formules type « Bien sûr ! » ou « Absolument ! ».
- Tu réponds uniquement avec le message destiné à l'élève, rien d'autre."""


# ============================================================================
# Sérialisation question → bloc compact pour les prompts
# ============================================================================


def _format_question(question: science_models.SciencesQuestionRow) -> str:
    """Sérialise une question pour injection dans un prompt."""
    scoring = question.scoring or {}
    type_rep = scoring.get("type_reponse") or scoring.get("mode") or "?"
    rep = scoring.get("reponse_canonique") or scoring.get("reponse_modele") or "?"

    discipline_label = DISCIPLINE_LABELS.get(question.discipline, question.discipline)
    theme_label = THEME_LABELS.get(question.theme, question.theme)

    lines = [
        f"discipline: {discipline_label}",
        f"theme: {theme_label}",
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
        "Niveau 1 (large) : rappelle la notion scientifique en jeu sans "
        "donner d'information décisive. Pas de mot-clé de la réponse. "
        "L'élève doit encore réfléchir."
    ),
    2: (
        "Niveau 2 (ciblé) : oriente vers la bonne catégorie de réponse "
        "(« pense à ce qui se passe au niveau des cellules », « c'est "
        "lié à la conservation de quelque chose »), mais ne donne PAS "
        "la réponse en clair."
    ),
    3: (
        "Niveau 3 (quasi-réponse) : donne l'information décisive ou le "
        "premier mot-clé de la réponse. Tu peux presque tout dire, mais "
        "laisse l'élève finaliser la formulation."
    ),
}


def build_hint_prompt(
    question: science_models.SciencesQuestionRow,
    hint_level: int,
    previous_answers: list[str],
) -> list[dict]:
    """Indice gradué adapté au niveau demandé.

    Si la question porte un indice pré-calculé (champ `indices.niveau_X`),
    on l'injecte comme suggestion, mais le modèle le reformule dans son
    propre ton de tuteur pour l'élève courant.
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

    user_msg = f"""L'élève bloque sur cette question de révision sciences :

<question>
{_format_question(question)}
</question>
{previous_block}{pre_hint_block}

Tu dois lui donner un INDICE, pas la réponse.

{guideline}

Contraintes :
- 1 à 2 phrases maximum.
- Tutoiement, ton bienveillant.
- Ne donne JAMAIS la réponse en clair (sauf au niveau 3 où tu peux donner un mot-clé décisif).
- Pas de préambule type « Voici un indice : ».

Écris l'indice maintenant."""

    return [
        {"role": "system", "content": SYSTEM_PERSONA},
        {"role": "user", "content": user_msg},
    ]


# ============================================================================
# 2. Révélation de la réponse
# ============================================================================


def build_reveal_prompt(
    question: science_models.SciencesQuestionRow,
) -> list[dict]:
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

    user_msg = f"""L'élève n'a pas trouvé la réponse. Tu dois la lui révéler avec bienveillance et expliquer rapidement la notion.

<question>
{_format_question(question)}
</question>
{pre_block}

Bonne réponse à annoncer : **{rep_label}**

Contraintes :
- Commence par une formule courte et bienveillante (« Pas grave, on regarde ensemble. »).
- Donne la bonne réponse clairement.
- Ajoute UNE phrase d'explication sur la notion scientifique (la définition, la propriété, l'astuce de mémorisation).
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
    question: science_models.SciencesQuestionRow,
    student_answer: str,
    rag_passages: list | None = None,
) -> list[dict]:
    """Évalue une réponse ouverte courte. Albert répond en JSON strict.

    Format de sortie attendu :
        {"correct": true|false, "feedback_court": "..."}

    Le `feedback_court` reste très court (1 phrase) et peut citer
    `[methodo]`, `[programme]` ou `[annale]` si le RAG a remonté quelque
    chose d'utile.
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

    user_msg = f"""Tu évalues une réponse d'élève à une question de révision scientifique.

<question>
enonce: {question.enonce}
discipline: {question.discipline}
theme: {question.theme}
reponse_modele: {reponse_modele}
</question>{criteres_block}{context_block}

<copie_eleve>
{student_answer.strip()}
</copie_eleve>

Tâche :
1. Décide si la réponse de l'élève est acceptable au regard des critères et de la réponse modèle.
2. Sois souple sur la forme (formulation, ordre des arguments) tant que le contenu est juste.
3. Cite la source [methodo], [programme] ou [annale] dans `feedback_court` UNIQUEMENT si tu t'appuies sur un passage du <context> ci-dessus.

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
