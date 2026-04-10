"""
Prompts LLM de l'épreuve « Repères chronologiques et spatiaux ».

Trois builders, tous pour la tâche `Task.UI_TEXT` (Mistral-Small-3.2-24B) :
1. `build_question_prompt`   : formule la question à partir du repère.
2. `build_hint_prompt`       : produit un indice gradué (niveau 1 → 3).
3. `build_reveal_prompt`     : révèle la bonne réponse avec bienveillance.

**Règle cardinale adaptée** (par rapport au DC où l'IA ne donne jamais
la réponse) : ici, la bonne réponse **est** l'objectif d'apprentissage.
L'IA donne des indices de plus en plus précis, et finit par révéler la
réponse si l'élève bloque. Le prompt d'indice reçoit la bonne réponse
mais a pour consigne stricte de ne PAS la donner en clair avant le
niveau 3.

Pas de RAG : chaque repère porte déjà toute l'information dont le
modèle a besoin (libellé, thème, année, notions associées). Gain de
latence et de coût.
"""

from __future__ import annotations

from app.histoire_geo_emc.reperes.models import Repere


# ============================================================================
# Persona commun
# ============================================================================


SYSTEM_PERSONA = """Tu es un tuteur bienveillant qui aide un·e élève de 3e à réviser ses repères d'histoire-géographie-EMC pour le DNB.

RÈGLES GÉNÉRALES :
- Tu t'adresses à l'élève en le tutoyant.
- Ton ton est chaleureux, encourageant, jamais moqueur.
- Tes messages sont COURTS (1 à 3 phrases max).
- Vocabulaire simple, mais historiquement ou géographiquement juste.
- Pas d'emojis, pas de formules comme « Bien sûr ! » ou « Absolument ! ».
- Tu réponds uniquement avec le message destiné à l'élève, rien d'autre."""


# ============================================================================
# Helpers de formatage du repère pour les prompts
# ============================================================================


def _format_repere(repere: Repere) -> str:
    """Sérialise un repère pour injection dans un prompt."""
    lines = [
        f"libelle: {repere.libelle}",
        f"discipline: {repere.discipline}",
        f"type: {repere.type}",
        f"theme: {repere.theme}",
    ]
    if repere.annee is not None:
        if repere.annee_fin is not None:
            lines.append(f"annees: {repere.annee}-{repere.annee_fin}")
        else:
            lines.append(f"annee: {repere.annee}")
    if repere.periode:
        lines.append(f"periode: {repere.periode}")
    notions = repere.notions_associees
    if notions:
        lines.append(f"notions_associees: {', '.join(notions)}")
    return "\n".join(lines)


# ============================================================================
# 1. Question
# ============================================================================


def build_question_prompt(repere: Repere) -> list[dict]:
    """Génère une question ciblée sur un repère donné.

    L'énoncé dépend du `type` du repère :
    - date/evenement : « En quelle année … ? »
    - personnage     : « Qui … ? »
    - lieu           : « Comment s'appelle … ? » ou « Où … ? »
    - notion         : « Que désigne le mot … ? »
    - definition     : « Qu'est-ce que … ? »
    """
    type_hint = {
        "date": "Formule une question dont la réponse est l'année (ex : « En quelle année … ? »).",
        "evenement": "Formule une question dont la réponse est l'événement OU son année, au choix selon ce qui est le plus naturel pour l'élève.",
        "personnage": "Formule une question dont la réponse est le nom du personnage.",
        "lieu": "Formule une question dont la réponse est le nom du lieu.",
        "notion": "Formule une question dont la réponse est le nom de la notion.",
        "definition": "Formule une question dont la réponse est le terme défini.",
    }.get(repere.type, "Formule une question claire et directe.")

    user_msg = f"""Voici un repère à réviser :

<repere>
{_format_repere(repere)}
</repere>

{type_hint}

Contraintes :
- UNE phrase, sous forme interrogative.
- Pas de préambule, pas de « Voici ta question : ».
- Ne donne PAS la réponse dans ta question.
- Vouvoie… non, TUTOIE l'élève.

Écris la question maintenant."""

    return [
        {"role": "system", "content": SYSTEM_PERSONA},
        {"role": "user", "content": user_msg},
    ]


# ============================================================================
# 2. Indices gradués
# ============================================================================


HINT_LEVEL_GUIDELINES = {
    1: (
        "Niveau 1 (large) : donne un contexte TRÈS général — époque, siècle, "
        "discipline, grand champ. Reste volontairement vague : l'élève doit "
        "encore chercher. N'évoque jamais directement le nom ou l'année cible."
    ),
    2: (
        "Niveau 2 (ciblé) : donne un indice plus précis — verbe-clé associé, "
        "champ thématique précis, ou un élément contextuel qui restreint "
        "fortement la recherche. Ne donne toujours PAS la réponse en clair."
    ),
    3: (
        "Niveau 3 (quasi-réponse) : l'élève bloque. Donne la première lettre "
        "du nom cherché, la décennie exacte, ou la région précise. Tu peux "
        "presque donner la réponse mais tu ne la donnes PAS encore en clair "
        "— laisse-lui UNE dernière chance."
    ),
}


def build_hint_prompt(
    repere: Repere,
    hint_level: int,
    previous_answers: list[str],
) -> list[dict]:
    """Produit un indice gradué adapté au niveau demandé.

    Le prompt reçoit la bonne réponse pour pouvoir orienter l'indice, mais
    a pour consigne STRICTE de ne pas la dévoiler avant le niveau 3 (et
    même au niveau 3, seulement partiellement).
    """
    guideline = HINT_LEVEL_GUIDELINES.get(
        hint_level, HINT_LEVEL_GUIDELINES[1]
    )

    previous_block = ""
    if previous_answers:
        tries = "\n".join(f"- {a}" for a in previous_answers[-3:])
        previous_block = (
            f"\n\nRéponses déjà tentées par l'élève (incorrectes) :\n{tries}\n"
            "Tu peux évoquer brièvement pourquoi ce n'est pas ça, mais sans "
            "ironie ni jugement."
        )

    user_msg = f"""L'élève bloque sur ce repère :

<repere>
{_format_repere(repere)}
</repere>
{previous_block}

Tu dois lui donner un INDICE, pas la réponse.

{guideline}

Contraintes :
- 1 à 2 phrases maximum.
- Tutoiement, ton bienveillant.
- Ne donne JAMAIS la réponse en clair (nom complet, année exacte). Sauf au niveau 3 où tu peux donner la première lettre ou la décennie.
- Pas de préambule type « Voici un indice : ».

Écris l'indice maintenant."""

    return [
        {"role": "system", "content": SYSTEM_PERSONA},
        {"role": "user", "content": user_msg},
    ]


# ============================================================================
# 3. Révélation de la réponse (après 3 échecs)
# ============================================================================


def build_reveal_prompt(repere: Repere) -> list[dict]:
    """Message de révélation bienveillant après épuisement des indices."""
    # La « bonne réponse à mémoriser » dépend du type :
    if repere.type == "date" and repere.annee is not None:
        reponse = f"{repere.libelle} → {repere.annee}"
    elif (
        repere.type == "evenement"
        and repere.annee is not None
    ):
        if repere.annee_fin is not None:
            reponse = f"{repere.libelle} → {repere.annee}-{repere.annee_fin}"
        else:
            reponse = f"{repere.libelle} → {repere.annee}"
    else:
        reponse = repere.libelle

    user_msg = f"""L'élève n'a pas trouvé la réponse après 3 indices. Tu dois lui révéler la bonne réponse avec bienveillance.

<repere>
{_format_repere(repere)}
</repere>

Bonne réponse à annoncer : **{reponse}**

Contraintes :
- Commence par une formule courte et bienveillante (ex : « Pas grave, on va le mémoriser ensemble. »).
- Donne la bonne réponse clairement.
- Ajoute UNE phrase de contexte pour aider à retenir (pourquoi c'est important, à quel thème c'est lié, un moyen mnémotechnique simple si pertinent).
- 2 à 3 phrases maximum, pas plus.
- Tutoiement.
- Pas de préambule type « Voici la réponse : ».

Écris le message de révélation maintenant."""

    return [
        {"role": "system", "content": SYSTEM_PERSONA},
        {"role": "user", "content": user_msg},
    ]


# ============================================================================
# 4. Feedback « correct » (optionnel, purement cosmétique)
# ============================================================================


POSITIVE_FEEDBACKS = [
    "Bien joué, c'est la bonne réponse.",
    "Exact — tu l'avais.",
    "Oui, parfait.",
    "C'est ça, bravo.",
    "Bonne réponse.",
]


def random_positive_feedback() -> str:
    """Feedback court quand l'élève trouve du premier coup.

    Volontairement pas généré par Albert : c'est cosmétique, pas de
    valeur ajoutée à faire un aller-retour API pour une phrase de 3 mots.
    """
    import random

    return random.choice(POSITIVE_FEEDBACKS)


__all__ = [
    "build_question_prompt",
    "build_hint_prompt",
    "build_reveal_prompt",
    "random_positive_feedback",
]
