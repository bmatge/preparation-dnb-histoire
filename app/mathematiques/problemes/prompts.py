"""Prompts LLM de la sous-épreuve « problèmes » (mathématiques DNB).

Trois builders, même découpage que pour les automatismes :

1. ``build_hint_prompt``      : indice gradué (Mistral-Small).
2. ``build_reveal_prompt``    : révélation pédagogique (Mistral-Small).
3. ``build_open_eval_prompt`` : évaluation d'une justification courte
                                (gpt-oss-120b, JSON strict).

Différence principale avec les automatismes : ici, chaque question
s'inscrit dans un exercice contextualisé (« le collège X, la fonction
f… »). On injecte donc le **contexte de l'exercice** en plus du texte
de la sous-question, pour que les indices et la correction ne donnent
pas une réponse hors-sol.

Règle cardinale maintenue : au niveau 3, l'indice peut presque tout
dire ; la révélation donne la réponse et l'explique brièvement. La
bonne réponse EST l'objectif d'apprentissage — pas de risque de
ghostwriting (les réponses sont courtes, numériques ou justifications
d'une phrase).
"""

from __future__ import annotations

import random

from app.mathematiques.problemes import models as prob_models


# ============================================================================
# Persona commun
# ============================================================================


SYSTEM_PERSONA = """Tu es un tuteur bienveillant qui aide un·e élève de 3e à travailler la Partie 2 de l'épreuve de mathématiques du DNB (raisonnement et résolution de problèmes).

RÈGLES GÉNÉRALES :
- Tu t'adresses à l'élève en le tutoyant.
- Ton ton est chaleureux, encourageant, jamais moqueur ni condescendant.
- Tes messages sont COURTS (1 à 3 phrases max), précis et directs.
- Vocabulaire simple, mais terminologie mathématique correcte (« PGCD », « image », « antécédent », « fraction irréductible »…).
- Pas d'emojis, pas de formules type « Bien sûr ! ».
- Tu réponds uniquement avec le message destiné à l'élève, rien d'autre."""


# ============================================================================
# Sérialisation contexte + sous-question → bloc compact pour les prompts
# ============================================================================


def _format_context_and_subquestion(
    exercise: prob_models.ProblemExercise, subquestion: dict
) -> str:
    """Sérialise le contexte d'un exercice et la sous-question ciblée."""
    scoring = subquestion.get("scoring") or {}
    type_rep = (
        scoring.get("type_reponse") or scoring.get("mode") or "?"
    )
    rep = (
        scoring.get("reponse_canonique")
        or scoring.get("reponse_modele")
        or "?"
    )

    lines = [
        f"exercice_titre: {exercise.titre}",
        f"theme: {exercise.theme}",
        "contexte_exercice: |",
    ]
    for line in (exercise.contexte or "").splitlines():
        lines.append(f"  {line}")
    lines.extend(
        [
            f"sous_question_numero: {subquestion.get('numero', '?')}",
            f"sous_question: {subquestion.get('texte', '')}",
            f"type_reponse: {type_rep}",
            f"reponse_attendue: {rep}",
        ]
    )
    if scoring.get("unite"):
        lines.append(f"unite: {scoring['unite']}")
    return "\n".join(lines)


# ============================================================================
# 1. Indice gradué
# ============================================================================


HINT_LEVEL_GUIDELINES = {
    1: (
        "Niveau 1 (large) : rappelle la notion ou la propriété mathématique en jeu "
        "(PGCD, moyenne, identité remarquable, image d'une fonction…) sans faire le "
        "calcul. L'élève doit encore réfléchir."
    ),
    2: (
        "Niveau 2 (ciblé) : pose la première étape concrète (« commence par sommer les "
        "effectifs », « pose l'équation f(x)=0 », « décompose 91 en facteurs premiers ») "
        "sans donner le résultat de cette étape."
    ),
    3: (
        "Niveau 3 (quasi-réponse) : donne le résultat intermédiaire de l'étape clé ou "
        "presque la réponse finale. Tu peux tout dire sauf le chiffre final exact, que "
        "l'élève doit écrire lui-même."
    ),
}


def build_hint_prompt(
    exercise: prob_models.ProblemExercise,
    subquestion: dict,
    hint_level: int,
    previous_answers: list[str],
) -> list[dict]:
    """Indice gradué pour une sous-question d'un exercice."""
    guideline = HINT_LEVEL_GUIDELINES.get(hint_level, HINT_LEVEL_GUIDELINES[1])

    indices = subquestion.get("indices") or {}
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

    user_msg = f"""L'élève bloque sur la sous-question suivante d'un exercice de raisonnement :

<exercice>
{_format_context_and_subquestion(exercise, subquestion)}
</exercice>
{previous_block}{pre_hint_block}

Tu dois lui donner un INDICE, pas la réponse complète.

{guideline}

Contraintes :
- 1 à 3 phrases maximum.
- Tutoiement, ton bienveillant.
- Ne donne JAMAIS la réponse finale en clair (sauf au niveau 3, où tu peux donner un résultat intermédiaire décisif).
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
    exercise: prob_models.ProblemExercise, subquestion: dict
) -> list[dict]:
    """Message de révélation bienveillant + mini-explication."""
    scoring = subquestion.get("scoring") or {}
    rep = (
        scoring.get("reponse_canonique")
        or scoring.get("reponse_modele")
        or "(réponse non trouvée dans la fiche)"
    )
    unite = scoring.get("unite")
    rep_label = f"{rep} {unite}" if unite else rep

    pre_explication = subquestion.get("reveal_explication") or ""
    pre_block = ""
    if pre_explication:
        pre_block = (
            f"\n\nExplication préparée en amont (à reformuler à ta manière) :\n"
            f"« {pre_explication} »\n"
        )

    user_msg = f"""L'élève n'a pas trouvé. Tu dois lui révéler la réponse avec bienveillance et expliquer rapidement la démarche.

<exercice>
{_format_context_and_subquestion(exercise, subquestion)}
</exercice>
{pre_block}

Bonne réponse à annoncer : **{rep_label}**

Contraintes :
- Commence par une formule courte et bienveillante (« Pas grave, on regarde ensemble. »).
- Donne la bonne réponse clairement.
- Ajoute une ou deux phrases d'explication sur la méthode (opération, propriété mobilisée, étape clé).
- 2 à 4 phrases maximum.
- Tutoiement.

Écris le message maintenant."""

    return [
        {"role": "system", "content": SYSTEM_PERSONA},
        {"role": "user", "content": user_msg},
    ]


# ============================================================================
# 3. Évaluation d'une justification courte (mode scoring=albert)
# ============================================================================


def build_open_eval_prompt(
    exercise: prob_models.ProblemExercise,
    subquestion: dict,
    student_answer: str,
    rag_passages: list | None = None,
) -> list[dict]:
    """Évalue une justification courte. Albert répond en JSON strict.

    Format de sortie attendu :
        {"correct": true|false, "feedback_court": "..."}

    Le ``feedback_court`` reste court (1 phrase) et peut citer
    ``[methodo]`` ou ``[programme]`` si un passage RAG a été injecté.
    """
    scoring = subquestion.get("scoring") or {}
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

    user_msg = f"""Tu évalues la justification courte d'un·e élève à une sous-question d'un exercice de raisonnement mathématique.

<exercice>
titre: {exercise.titre}
theme: {exercise.theme}
contexte: {exercise.contexte.strip()}
sous_question_numero: {subquestion.get('numero', '?')}
sous_question: {subquestion.get('texte', '')}
reponse_modele: {reponse_modele}
</exercice>{criteres_block}{context_block}

<copie_eleve>
{student_answer.strip()}
</copie_eleve>

Tâche :
1. Décide si la justification de l'élève est acceptable au regard des critères et de la réponse modèle.
2. Sois souple sur la forme (formulation, ordre des arguments) tant que le contenu est juste.
3. Cite la source [methodo] ou [programme] dans ``feedback_court`` UNIQUEMENT si tu t'appuies sur un passage du <context> ci-dessus.

Réponds UNIQUEMENT avec un objet JSON, sans markdown, sans préambule, au format strict suivant :
{{"correct": true, "feedback_court": "Une phrase au tutoiement, ≤ 30 mots."}}

ou

{{"correct": false, "feedback_court": "Une phrase au tutoiement, ≤ 30 mots."}}
"""

    return [
        {"role": "system", "content": SYSTEM_PERSONA},
        {"role": "user", "content": user_msg},
    ]


# ============================================================================
# 4. Feedbacks positifs (cosmétique, pas d'Albert)
# ============================================================================


POSITIVE_FEEDBACKS = [
    "Bien joué, c'est exactement ça.",
    "Exact, tu l'avais.",
    "Oui, parfait — on continue.",
    "Bravo, c'est la bonne réponse.",
    "Nickel, passons à la suivante.",
    "C'est ça — bon raisonnement.",
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
