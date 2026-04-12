"""Prompts LLM de l'epreuve Simulation sciences.

Trois builders, deux modeles Albert :

1. ``build_hint_prompt``      : indice gradue (Mistral-Small).
2. ``build_reveal_prompt``    : revelation pedagogique (Mistral-Small).
3. ``build_open_eval_prompt`` : evaluation d'une reponse ouverte courte
                                (gpt-oss-120b, JSON strict).

Adapte de ``app/sciences/revision/prompts.py`` avec ajout du contexte
de simulation (documents, discipline, theme du sujet).
"""

from __future__ import annotations

import random

from app.sciences.simulation.loader import DISCIPLINE_LABELS


# ============================================================================
# Persona commun
# ============================================================================


SYSTEM_PERSONA = """Tu es un tuteur bienveillant qui aide un·e eleve de 3e a s'entrainer a l'epreuve de sciences du DNB dans les conditions du jour J.

REGLES GENERALES :
- Tu t'adresses a l'eleve en le tutoyant.
- Ton ton est chaleureux, encourageant, jamais moqueur ni condescendant.
- Tes messages sont COURTS (1 a 3 phrases max).
- Vocabulaire simple, mais terminologie scientifique correcte.
- Pas d'emojis, pas de formules type « Bien sur ! » ou « Absolument ! ».
- Tu reponds uniquement avec le message destine a l'eleve, rien d'autre."""


# ============================================================================
# Serialisation question -> bloc compact pour les prompts
# ============================================================================


def _format_question(question: dict, discipline: str, theme_titre: str) -> str:
    scoring = question.get("scoring") or {}
    type_rep = scoring.get("type_reponse") or scoring.get("mode") or "?"
    rep = scoring.get("reponse_canonique") or scoring.get("reponse_modele") or "?"

    discipline_label = DISCIPLINE_LABELS.get(discipline, discipline)

    lines = [
        f"discipline: {discipline_label}",
        f"theme: {theme_titre}",
        f"numero: {question.get('numero', '?')}",
        f"enonce: {question.get('texte', '')}",
        f"type_reponse: {type_rep}",
        f"reponse_attendue: {rep}",
    ]
    if scoring.get("unite"):
        lines.append(f"unite: {scoring['unite']}")
    if question.get("points"):
        lines.append(f"points: {question['points']}")
    return "\n".join(lines)


# ============================================================================
# 1. Indice gradue
# ============================================================================


HINT_LEVEL_GUIDELINES = {
    1: (
        "Niveau 1 (large) : rappelle la notion scientifique en jeu sans "
        "donner d'information decisive. Pas de mot-cle de la reponse. "
        "L'eleve doit encore reflechir."
    ),
    2: (
        "Niveau 2 (cible) : oriente vers la bonne categorie de reponse "
        "(« pense a ce qui se passe au niveau des cellules », « c'est "
        "lie a la conservation de quelque chose »), mais ne donne PAS "
        "la reponse en clair."
    ),
    3: (
        "Niveau 3 (quasi-reponse) : donne l'information decisive ou le "
        "premier mot-cle de la reponse. Tu peux presque tout dire, mais "
        "laisse l'eleve finaliser la formulation."
    ),
}


def build_hint_prompt(
    question: dict,
    discipline: str,
    theme_titre: str,
    hint_level: int,
    previous_answers: list[str],
) -> list[dict]:
    guideline = HINT_LEVEL_GUIDELINES.get(hint_level, HINT_LEVEL_GUIDELINES[1])

    indices = question.get("indices") or {}
    pre_hint = indices.get(f"niveau_{hint_level}")
    pre_hint_block = ""
    if pre_hint:
        pre_hint_block = (
            f"\n\nIndice prepare en amont (a reformuler a ta maniere) :\n"
            f"<< {pre_hint} >>\n"
        )

    previous_block = ""
    if previous_answers:
        tries = "\n".join(f"- {a}" for a in previous_answers[-3:])
        previous_block = (
            f"\n\nReponses deja tentees par l'eleve (incorrectes) :\n{tries}\n"
            "Ne ridiculise jamais une erreur."
        )

    user_msg = f"""L'eleve bloque sur cette question de simulation sciences (epreuve blanche DNB) :

<question>
{_format_question(question, discipline, theme_titre)}
</question>
{previous_block}{pre_hint_block}

Tu dois lui donner un INDICE, pas la reponse.

{guideline}

Contraintes :
- 1 a 2 phrases maximum.
- Tutoiement, ton bienveillant.
- Ne donne JAMAIS la reponse en clair (sauf au niveau 3 ou tu peux donner un mot-cle decisif).
- Pas de preambule type « Voici un indice : ».

Ecris l'indice maintenant."""

    return [
        {"role": "system", "content": SYSTEM_PERSONA},
        {"role": "user", "content": user_msg},
    ]


# ============================================================================
# 2. Revelation de la reponse
# ============================================================================


def build_reveal_prompt(
    question: dict,
    discipline: str,
    theme_titre: str,
) -> list[dict]:
    scoring = question.get("scoring") or {}
    rep = (
        scoring.get("reponse_canonique")
        or scoring.get("reponse_modele")
        or "(reponse non trouvee dans la fiche)"
    )
    unite = scoring.get("unite")
    rep_label = f"{rep} {unite}" if unite else rep

    pre_explication = question.get("reveal_explication") or ""
    pre_block = ""
    if pre_explication:
        pre_block = (
            f"\n\nExplication preparee en amont (a reformuler a ta maniere) :\n"
            f"<< {pre_explication} >>\n"
        )

    user_msg = f"""L'eleve n'a pas trouve la reponse. Tu dois la lui reveler avec bienveillance et expliquer rapidement la notion.

<question>
{_format_question(question, discipline, theme_titre)}
</question>
{pre_block}

Bonne reponse a annoncer : **{rep_label}**

Contraintes :
- Commence par une formule courte et bienveillante (« Pas grave, on regarde ensemble. »).
- Donne la bonne reponse clairement.
- Ajoute UNE phrase d'explication sur la notion scientifique.
- 2 a 3 phrases maximum.
- Tutoiement.

Ecris le message maintenant."""

    return [
        {"role": "system", "content": SYSTEM_PERSONA},
        {"role": "user", "content": user_msg},
    ]


# ============================================================================
# 3. Evaluation d'une reponse ouverte (mode scoring=albert)
# ============================================================================


def build_open_eval_prompt(
    question: dict,
    discipline: str,
    theme_titre: str,
    student_answer: str,
    rag_passages: list | None = None,
) -> list[dict]:
    scoring = question.get("scoring") or {}
    reponse_modele = scoring.get("reponse_modele") or "(non precisee)"
    criteres = scoring.get("criteres_validation") or []
    criteres_block = ""
    if criteres:
        bullets = "\n".join(f"- {c}" for c in criteres)
        criteres_block = f"\n\nCriteres de validation :\n{bullets}"

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

    discipline_label = DISCIPLINE_LABELS.get(discipline, discipline)

    user_msg = f"""Tu evalues une reponse d'eleve a une question de simulation sciences (epreuve blanche DNB).

<question>
enonce: {question.get('texte', '')}
discipline: {discipline_label}
theme: {theme_titre}
reponse_modele: {reponse_modele}
</question>{criteres_block}{context_block}

<copie_eleve>
{student_answer.strip()}
</copie_eleve>

Tache :
1. Decide si la reponse de l'eleve est acceptable au regard des criteres et de la reponse modele.
2. Sois souple sur la forme (formulation, ordre des arguments) tant que le contenu est juste.
3. Cite la source [methodo], [programme] ou [annale] dans ``feedback_court`` UNIQUEMENT si tu t'appuies sur un passage du <context> ci-dessus.

Reponds UNIQUEMENT avec un objet JSON, sans markdown, sans preambule, au format strict suivant :
{{"correct": true, "feedback_court": "Une phrase au tutoiement, <= 25 mots."}}

ou

{{"correct": false, "feedback_court": "Une phrase au tutoiement, <= 25 mots."}}
"""

    return [
        {"role": "system", "content": SYSTEM_PERSONA},
        {"role": "user", "content": user_msg},
    ]


# ============================================================================
# 4. Feedbacks positifs (cosmetique, pas d'Albert)
# ============================================================================


POSITIVE_FEEDBACKS = [
    "Bien joue, c'est ca.",
    "Exact -- tu l'avais.",
    "Oui, parfait.",
    "C'est ca, bravo.",
    "Bonne reponse.",
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
