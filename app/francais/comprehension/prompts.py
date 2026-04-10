"""Prompts Albert pour la sous-épreuve compréhension/interprétation français.

## Règles cardinales (à ne pas relâcher sans forte raison)

1. **L'IA ne donne jamais la réponse** tant que l'élève n'a pas épuisé ses
   3 indices. Après le 3e indice, si l'élève bloque encore, la réponse est
   révélée via `build_reveal_answer` avec une explication pédagogique.

2. **Pas d'hallucination méta** : l'IA ne doit jamais attribuer à l'élève des
   faits présents dans le texte littéraire mais absents de sa réponse. Les
   balises XML séparent strictement texte source, question et réponse élève
   (gotcha §5.1 de HANDOFF).

3. **Pas de ghostwriting** : si un indice contient un extrait que l'élève
   pourrait recopier comme réponse, c'est une violation. Les indices orientent,
   ils n'exposent pas.

## Découpage des modèles Albert (voir `app.core.albert_client`)

- Évaluation d'une réponse → `gpt-oss-120b` (tâche `FR_COMP_EVAL`)
- Génération d'indice → `mistral-small` (tâche `FR_COMP_HINT`)
- Révélation finale → `gpt-oss-120b` (tâche `FR_COMP_REVEAL`)
- Synthèse de fin de session → `gpt-oss-120b` (tâche `FR_COMP_SYNTHESE`)

## Cas particulier : réécriture

Les questions de type `reecriture` utilisent les builders `*_reecriture`
plutôt que les builders génériques. Le contrat d'évaluation est
différent : on ne juge pas si l'élève a « compris » quelque chose, on
vérifie qu'il a bien appliqué une transformation mécanique (changer un
pronom, un temps, un nombre…) en faisant toutes les modifications
nécessaires de concordance. L'éval passe contrainte par contrainte et
signale les erreurs sans donner la version corrigée. Les indices gradués
fonctionnent aussi : niveau 1 = contrainte violée, niveau 2 = catégorie
d'erreur, niveau 3 = orientation sur un mot précis.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.core.rag import RagPassage

from dataclasses import dataclass

from app.francais.comprehension.models import ExerciseItem, Ligne, NoteTexte


# ============================================================================
# Persona système
# ============================================================================

SYSTEM_PERSONA = """\
Tu es un assistant pédagogique français qui aide un·e élève de 3e à \
s'entraîner à l'épreuve de « Compréhension et compétences d'interprétation » \
du Diplôme National du Brevet.

Ton rôle est d'accompagner l'élève dans son raisonnement, PAS de répondre à sa \
place. Tu suis rigoureusement les règles suivantes :

1. **Tu ne donnes jamais directement la réponse à une question.** Ton travail \
consiste à guider l'élève par des questions, des reformulations et des \
orientations méthodologiques jusqu'à ce qu'il trouve lui-même la réponse.

2. **Quand tu cites le texte ou la réponse de l'élève, tu vérifies toujours** \
que ce que tu cites se trouve littéralement dans la balise correspondante. \
N'invente jamais une phrase que l'élève n'a pas écrite. N'attribue jamais à \
l'élève une idée qu'il n'a pas exprimée.

3. **Tu t'adresses à l'élève en le tutoyant**, dans un français simple, \
bienveillant et encourageant. Ton ton est celui d'un·e professeur·e qui croit \
en la capacité de l'élève à trouver par lui-même.

4. **Tu ne recopies jamais d'extraits longs du texte littéraire** dans tes \
indices : cela reviendrait à donner la réponse. Tu peux renvoyer à des lignes \
précises (« regarde ce que dit le narrateur lignes 15 à 17 »), mais sans \
copier leur contenu intégral.

5. **Tu restes strictement dans le périmètre de la question en cours.** Tu ne \
commentes pas les réponses précédentes, tu ne donnes pas de conseils \
généraux sur la dissertation, tu ne digresses pas.

6. **Tu réponds en français correct** : orthographe, ponctuation, guillemets \
français « ». Pas d'emojis.
"""


# ============================================================================
# Builders
# ============================================================================


@dataclass
class ExerciseContext:
    """Contexte partagé par tous les prompts d'un item."""

    texte_lignes: list[Ligne]
    notes: list[NoteTexte]
    paratexte: str | None
    item: ExerciseItem

    def texte_balise(self) -> str:
        """Rend le texte littéraire numéroté ligne par ligne dans une balise XML."""
        parts = []
        if self.paratexte:
            parts.append(f"[Paratexte] {self.paratexte}")
            parts.append("")
        for ligne in self.texte_lignes:
            parts.append(f"{ligne.n:>3}  {ligne.texte}")
        return "\n".join(parts)

    def notes_balise(self) -> str:
        if not self.notes:
            return "(aucune note)"
        return "\n".join(f"[{n.n}] {n.terme} : {n.definition}" for n in self.notes)

    def question_balise(self) -> str:
        parts = [f"Question {self.item.label} ({self.item.points} point(s))"]
        if self.item.citation:
            parts.append(f"Citation de référence : « {self.item.citation} »")
        if self.item.lignes_ciblees:
            plages = ", ".join(
                f"{p.start}" if p.start == p.end else f"{p.start}-{p.end}"
                for p in self.item.lignes_ciblees
            )
            parts.append(f"Lignes visées : {plages}")
        if self.item.competence:
            parts.append(f"Compétence évaluée : {self.item.competence}")
        parts.append("")
        parts.append(f"Énoncé :\n{self.item.enonce_complet}")
        return "\n".join(parts)


def _passages_balise(passages: "list[RagPassage] | None") -> str:
    """Rend les passages RAG en un bloc balisé pour injection dans le prompt.

    Chaque passage est préfixé par son étiquette de source entre crochets
    (`[méthodo]`, `[programme]`), ce qui permet au modèle de citer la source
    dans son verdict sans avoir à connaître les noms techniques des
    collections Albert.

    Si `passages` est vide ou None, retourne une balise <context> vide —
    le modèle saura qu'il n'y a pas de source externe à exploiter.
    """
    if not passages:
        return "(aucun extrait de méthodologie ou de programme disponible)"
    parts = []
    for p in passages:
        content = p.content.strip().replace("\n\n\n", "\n\n")
        parts.append(f"[{p.source}]\n{content}")
    return "\n\n---\n\n".join(parts)


def build_first_eval(
    ctx: ExerciseContext,
    reponse_eleve: str,
    passages: "list[RagPassage] | None" = None,
) -> str:
    """Évalue la première tentative de l'élève.

    Retour attendu du LLM : verdict structuré (correcte / partielle /
    insuffisante) + commentaire court. PAS d'indice, PAS de réponse.

    Si `passages` est fourni, les extraits des fiches méthodo et du programme
    officiel sont injectés dans une balise `<context>` séparée de la réponse
    élève. Le modèle est invité à s'y appuyer pour justifier son verdict,
    mais la séparation stricte via les balises XML garantit qu'il ne
    confondra pas les sources externes avec ce que l'élève a écrit.
    """
    return f"""\
Un·e élève de 3e tente de répondre à une question de compréhension d'un texte \
littéraire. Voici le contexte.

<texte_litteraire>
{ctx.texte_balise()}
</texte_litteraire>

<notes_du_texte>
{ctx.notes_balise()}
</notes_du_texte>

<context>
{_passages_balise(passages)}
</context>

<question>
{ctx.question_balise()}
</question>

<reponse_eleve>
{reponse_eleve.strip()}
</reponse_eleve>

Ta tâche : évaluer cette réponse SANS donner la bonne réponse, SANS proposer \
d'indice, SANS rédiger ce que l'élève aurait dû écrire.

Tu dois produire exactement trois sections, dans cet ordre, séparées par des \
sauts de ligne :

VERDICT : un seul mot parmi {{CORRECTE, PARTIELLE, INSUFFISANTE}}.
- CORRECTE : la réponse couvre l'essentiel de ce qui est attendu par la \
question, même si la rédaction peut être améliorée.
- PARTIELLE : la réponse contient un élément valide mais il en manque \
d'autres OU la justification est incomplète.
- INSUFFISANTE : la réponse est hors-sujet, trop vague, recopie le texte \
sans analyse, ou ne répond pas à ce qui est demandé.

COMMENTAIRE : deux à trois phrases, en t'adressant directement à l'élève \
(« tu as... », « ta réponse... »). Explique ce qui va ou ne va pas, sans \
jamais révéler ce qu'il fallait répondre. N'écris rien que l'élève pourrait \
recopier tel quel comme réponse.

PROCHAINE_ACTION : un seul mot parmi {{VALIDER, INDICE, RETENTER}}.
- VALIDER : VERDICT = CORRECTE, l'élève peut passer à la question suivante.
- INDICE : VERDICT = PARTIELLE ou INSUFFISANTE et tu penses qu'un indice \
aiderait l'élève à progresser.
- RETENTER : VERDICT = PARTIELLE, mais l'élève est sur la bonne voie et doit \
juste approfondir ; pas besoin d'indice, juste une relance.

Contraintes strictes :
- Quand tu cites « tu as dit X », vérifie que X se trouve LITTÉRALEMENT dans \
<reponse_eleve>. Jamais dans <texte_litteraire>.
- Ne cite jamais plus de 5 mots consécutifs du texte littéraire.
- Si tu t'appuies sur un extrait de <context> (fiche méthodologique ou \
programme), tu peux indiquer la règle en quelques mots mais tu ne recopies \
pas le passage complet. Tu peux signaler l'origine entre crochets, ex : \
« la fiche [méthodo] rappelle que... ». Ne jamais copier un extrait du \
<context> dans une phrase qui ressemble à une réponse.
- Pas de liste à puces, pas de titres markdown, juste les trois sections.
"""


def build_hint(
    ctx: ExerciseContext,
    reponse_eleve: str,
    level: int,
    passages: "list[RagPassage] | None" = None,
) -> str:
    """Construit un indice gradué (1, 2 ou 3).

    Niveau 1 : reformulation / recentrage sur la question.
    Niveau 2 : piste méthodologique (type d'outil d'analyse à mobiliser).
    Niveau 3 : orientation concrète vers une zone du texte, sans révéler.
    """
    if level not in (1, 2, 3):
        raise ValueError(f"hint level must be 1, 2 or 3, got {level}")

    niveaux = {
        1: (
            "INDICE DE NIVEAU 1 : reformulation et recentrage.\n"
            "Reformule la question autrement, rappelle à l'élève sur quoi "
            "elle porte exactement (le narrateur ? un personnage ? un passage "
            "précis ?), et éventuellement pose-lui une petite question de "
            "relance qui l'oriente vers ce qu'il faut chercher. N'indique PAS "
            "de méthode d'analyse, n'oriente PAS vers une zone particulière "
            "du texte."
        ),
        2: (
            "INDICE DE NIVEAU 2 : piste méthodologique.\n"
            "Propose à l'élève un outil d'analyse qu'il pourrait mobiliser : "
            "champ lexical, figure de style, connecteur logique, temps verbal, "
            "ponctuation, inférence sur un personnage, contexte historique... "
            "Mentionne l'outil mais ne dis pas où chercher. N'indique PAS de "
            "ligne précise, ne donne PAS la réponse."
        ),
        3: (
            "INDICE DE NIVEAU 3 : orientation concrète.\n"
            "Oriente l'élève vers une zone précise du texte (ex : « regarde "
            "aux lignes 15 à 18 ») ou vers un élément concret à chercher "
            "(ex : « cherche un mot qui désigne la peur »). Mais NE CITE PAS "
            "le contenu de ces lignes ni le mot recherché. Ne rédige pas la "
            "réponse. L'élève doit encore faire le dernier pas lui-même."
        ),
    }

    return f"""\
Un·e élève de 3e n'a pas encore trouvé la réponse à une question de \
compréhension. C'est sa {_ordinal(level + 1)} tentative : tu dois lui \
fournir un indice de niveau {level}.

<texte_litteraire>
{ctx.texte_balise()}
</texte_litteraire>

<notes_du_texte>
{ctx.notes_balise()}
</notes_du_texte>

<context>
{_passages_balise(passages)}
</context>

<question>
{ctx.question_balise()}
</question>

<reponse_eleve_actuelle>
{reponse_eleve.strip()}
</reponse_eleve_actuelle>

{niveaux[level]}

Contraintes strictes, valables à tous les niveaux d'indice :
- Tu ne donnes JAMAIS la réponse, même partielle.
- Tu ne cites jamais plus de 5 mots consécutifs du texte littéraire.
- Tu ne rédiges jamais une phrase que l'élève pourrait recopier comme réponse.
- Si tu t'appuies sur une fiche méthodologique du <context>, tu peux nommer \
la règle en 3-4 mots mais tu ne recopies pas le passage complet. L'indice \
doit rester un coup de pouce, pas un cours magistral.
- Tu t'adresses directement à l'élève en le tutoyant.
- Maximum 4 phrases. Court, net, pédagogique.
- Pas de titre, pas de liste à puces, pas de balises markdown. Juste le texte \
de l'indice.
"""


def build_reveal_answer(
    ctx: ExerciseContext,
    reponse_eleve: str,
    passages: "list[RagPassage] | None" = None,
) -> str:
    """Révélation de la réponse après l'épuisement des 3 indices.

    C'est le SEUL cas où l'IA donne la bonne réponse, et elle le fait sous
    forme pédagogique : raisonnement complet, et non simple énoncé. Quand
    des passages RAG sont injectés, l'IA peut citer « la fiche [méthodo] »
    ou « le programme officiel [programme] » pour ancrer le raisonnement
    dans une source d'autorité.
    """
    return f"""\
Un·e élève de 3e a épuisé ses 3 indices sans trouver la réponse. Il est \
temps de lui expliquer la bonne réponse, mais d'une manière qui reste \
pédagogique : tu expliques le raisonnement et pas seulement le résultat, \
pour qu'il comprenne comment faire la prochaine fois.

<texte_litteraire>
{ctx.texte_balise()}
</texte_litteraire>

<notes_du_texte>
{ctx.notes_balise()}
</notes_du_texte>

<context>
{_passages_balise(passages)}
</context>

<question>
{ctx.question_balise()}
</question>

<reponse_eleve_finale>
{reponse_eleve.strip()}
</reponse_eleve_finale>

Ta réponse doit comporter trois parties courtes, dans cet ordre :

RAISONNEMENT : deux à quatre phrases qui expliquent comment on trouve la \
réponse à partir du texte. Montre le chemin : où chercher, quoi repérer, \
quoi en déduire. Tu peux t'appuyer sur une règle de la fiche [méthodo] ou \
sur un attendu du [programme] si c'est utile, en le citant explicitement \
(« la fiche méthodo rappelle que... »), sans recopier plus de 2 lignes.

REPONSE : une ou deux phrases qui formulent la bonne réponse de manière \
claire et complète, comme l'attendrait un correcteur du DNB.

POUR_LA_PROCHAINE_FOIS : une seule phrase d'orientation générale qui résume \
ce que l'élève devra mobiliser la prochaine fois face à ce type de question. \
PAS un plan, PAS une méthode détaillée — juste une idée-phare.

Contraintes :
- Tu t'adresses à l'élève en le tutoyant, avec bienveillance.
- Tu peux citer des passages courts (maxi une dizaine de mots) du texte, \
en les mettant entre guillemets français « » et en indiquant la ligne.
- Pas de liste à puces, pas de titres markdown, juste les trois sections.
"""


def build_session_synthese(
    items_resolved: list[tuple[ExerciseItem, str, bool]],
    passages: "list[RagPassage] | None" = None,
) -> str:
    """Bilan de fin de session.

    `items_resolved` : liste de (item, réponse finale élève, trouvée_seul).

    Quand des passages RAG sont fournis (programme cycle 4 + fiches méthodo),
    la synthèse peut pointer vers une fiche concrète à relire ou vers un
    attendu officiel du programme à retravailler, plutôt qu'une suggestion
    vague.
    """
    lignes_items = []
    for item, rep, seul in items_resolved:
        statut = "trouvée seul·e" if seul else "révélée après indices"
        lignes_items.append(
            f"- Question {item.label} ({item.partie}, compétence={item.competence}) : "
            f"{statut}."
        )
    items_block = "\n".join(lignes_items)

    return f"""\
Un·e élève de 3e vient de terminer une séance d'entraînement sur un sujet de \
compréhension. Voici le bilan question par question :

<bilan_session>
{items_block}
</bilan_session>

<context>
{_passages_balise(passages)}
</context>

Rédige pour l'élève une synthèse courte (8 phrases maximum) qui comprend, \
dans cet ordre :

1. Une phrase d'encouragement concrète qui reprend UN point fort observable \
dans le bilan (ex : « tu as bien réussi les questions de repérage explicite »).

2. Une identification des 1 à 2 compétences qu'il devrait retravailler en \
priorité, basée sur les questions où la réponse a été révélée. Nomme la \
compétence en mots simples (ex : « l'interprétation des intentions du \
narrateur »).

3. Une suggestion concrète d'entraînement : quoi relire, quel type de \
question refaire la prochaine fois. Si le <context> contient une fiche \
[méthodo] ou un attendu [programme] qui correspond à la compétence faible, \
tu peux nommer la fiche (« la fiche sur les classes grammaticales ») ou \
l'attendu officiel — c'est plus concret qu'une suggestion vague. Une seule \
suggestion, courte.

4. Une dernière phrase d'encouragement.

Contraintes :
- Tu t'adresses à l'élève en le tutoyant.
- Pas de liste à puces, pas de titres, juste un paragraphe ou deux.
- Pas de jugement sur la personne (« tu es... »), seulement sur le travail \
(« ton travail montre... »).
- Pas d'emojis.
"""


# ============================================================================
# Builders spécifiques à la réécriture
# ============================================================================


def _format_contraintes(contraintes: list[str]) -> str:
    if not contraintes:
        return "(aucune contrainte explicite)"
    return "\n".join(f"- {c}" for c in contraintes)


def build_reecriture_eval(
    ctx: ExerciseContext,
    reponse_eleve: str,
    passages: "list[RagPassage] | None" = None,
) -> str:
    """Évalue une réécriture : le passage transformé par l'élève.

    Contrairement à une question de compréhension, on ne juge pas une
    « bonne interprétation » — on vérifie que chaque contrainte imposée
    par la consigne a été correctement appliquée, et que l'élève a bien
    propagé toutes les modifications de concordance (accords, temps,
    pronoms...).
    """
    item = ctx.item
    passage = item.passage_a_reecrire or "(passage manquant dans le JSON)"
    contraintes = _format_contraintes(item.contraintes)

    return f"""\
Un·e élève de 3e vient de réécrire un passage d'un texte littéraire \
selon des contraintes précises. Tu dois évaluer sa réécriture sans lui \
donner la version corrigée.

<texte_litteraire>
{ctx.texte_balise()}
</texte_litteraire>

<context>
{_passages_balise(passages)}
</context>

<consigne_reecriture>
{item.enonce_complet}

Contraintes à respecter :
{contraintes}
</consigne_reecriture>

<passage_original>
{passage}
</passage_original>

<reecriture_eleve>
{reponse_eleve.strip()}
</reecriture_eleve>

Ta tâche : vérifier contrainte par contrainte si la réécriture est \
correcte. Produis exactement trois sections :

VERDICT : un seul mot parmi {{CORRECTE, PARTIELLE, INSUFFISANTE}}.
- CORRECTE : toutes les contraintes sont respectées ET toutes les \
modifications de concordance (accords sujet-verbe, accords en genre et \
nombre, temps composés, pronoms possessifs, participes passés…) ont été \
correctement propagées dans le passage. Les éléments qui ne devaient pas \
bouger n'ont effectivement pas bougé.
- PARTIELLE : les contraintes principales sont appliquées mais il reste \
1 ou 2 erreurs de concordance oubliées (ex : un accord manqué, un \
participe passé non adapté).
- INSUFFISANTE : une contrainte principale n'est pas appliquée, ou il y \
a plusieurs erreurs de concordance, ou l'élève a modifié des éléments \
qui ne devaient pas l'être.

COMMENTAIRE : deux à trois phrases qui indiquent ce qui va et ce qui \
cloche, en pointant le TYPE d'erreur (accord, temps, pronom, \
concordance…) mais SANS donner la version corrigée du passage et SANS \
réécrire les mots que l'élève devrait trouver lui-même. Tu peux dire \
« ton accord avec 'ils' n'a pas été propagé sur le verbe » mais pas \
« il faut écrire 'étaient' ».

PROCHAINE_ACTION : un seul mot parmi {{VALIDER, INDICE, RETENTER}}.
- VALIDER : VERDICT = CORRECTE.
- INDICE : VERDICT = INSUFFISANTE, l'élève a besoin d'un coup de pouce.
- RETENTER : VERDICT = PARTIELLE, l'élève doit juste corriger les \
dernières erreurs.

Contraintes strictes :
- N'écris JAMAIS le passage corrigé, même partiellement.
- N'écris aucune phrase que l'élève pourrait recopier comme réponse.
- Quand tu cites « tu as écrit X », vérifie que X est LITTÉRALEMENT dans \
<reecriture_eleve>.
- Pas de liste à puces, pas de titres markdown, juste les trois sections.
"""


def build_reecriture_hint(
    ctx: ExerciseContext,
    reponse_eleve: str,
    level: int,
    passages: "list[RagPassage] | None" = None,
) -> str:
    """Indice gradué pour une question de réécriture.

    Niveau 1 : rappeler la contrainte principale qui n'est pas respectée.
    Niveau 2 : nommer la catégorie d'erreur (accord, temps, pronom…).
    Niveau 3 : orienter vers un mot précis à corriger, sans donner le mot
               corrigé.
    """
    if level not in (1, 2, 3):
        raise ValueError(f"hint level must be 1, 2 or 3, got {level}")

    item = ctx.item
    passage = item.passage_a_reecrire or ""
    contraintes = _format_contraintes(item.contraintes)

    niveaux = {
        1: (
            "INDICE NIVEAU 1 : rappel de contrainte.\n"
            "Identifie UNE contrainte parmi celles de la consigne qui n'est "
            "pas respectée dans la réécriture de l'élève, et rappelle-la lui "
            "sans lui dire comment la corriger. Ne nomme pas encore la "
            "catégorie grammaticale de l'erreur, reste au niveau de la "
            "consigne."
        ),
        2: (
            "INDICE NIVEAU 2 : catégorie d'erreur.\n"
            "Nomme la catégorie grammaticale de l'erreur principale : accord "
            "sujet-verbe, accord en genre/nombre, concordance des temps, "
            "participe passé, pronom, etc. Dis dans quel type de mot ça "
            "coince, mais ne montre pas encore quel mot précis."
        ),
        3: (
            "INDICE NIVEAU 3 : mot à revoir.\n"
            "Désigne un mot précis dans la réécriture de l'élève qui doit "
            "être modifié (« regarde le verbe conjugué au début de la "
            "deuxième ligne », « regarde l'adjectif qui suit ce nom »…). "
            "Mais ne donne PAS la forme corrigée du mot. L'élève doit "
            "encore faire le dernier pas lui-même."
        ),
    }

    return f"""\
Un·e élève de 3e a réécrit un passage mais sa réécriture contient des \
erreurs. Tu dois lui donner un indice de niveau {level}.

<context>
{_passages_balise(passages)}
</context>

<consigne_reecriture>
{item.enonce_complet}

Contraintes :
{contraintes}
</consigne_reecriture>

<passage_original>
{passage}
</passage_original>

<reecriture_eleve_actuelle>
{reponse_eleve.strip()}
</reecriture_eleve_actuelle>

{niveaux[level]}

Contraintes strictes :
- Tu ne donnes JAMAIS le mot corrigé ou la phrase corrigée.
- Tu ne réécris JAMAIS un fragment tel qu'il devrait être.
- Tu tutoies l'élève.
- Maximum 3 phrases. Court et précis.
- Pas de titre, pas de liste à puces, juste le texte de l'indice.
"""


def build_reecriture_reveal(
    ctx: ExerciseContext,
    reponse_eleve: str,
    passages: "list[RagPassage] | None" = None,
) -> str:
    """Révélation de la réécriture corrigée après épuisement des 3 indices.

    Quand des passages RAG sont fournis (fiches méthodo + programme), le
    raisonnement grammatical peut s'appuyer sur une règle officielle en
    citant la fiche [méthodo], plutôt que de poser les choses comme si
    elles tombaient du ciel.
    """
    item = ctx.item
    passage = item.passage_a_reecrire or ""
    contraintes = _format_contraintes(item.contraintes)

    return f"""\
Un·e élève de 3e a épuisé ses 3 indices sans réussir à corriger sa \
réécriture. Il est temps de lui montrer la version corrigée, mais de \
manière pédagogique pour qu'il comprenne pourquoi c'est comme ça et \
pas autrement.

<context>
{_passages_balise(passages)}
</context>

<consigne_reecriture>
{item.enonce_complet}

Contraintes :
{contraintes}
</consigne_reecriture>

<passage_original>
{passage}
</passage_original>

<reecriture_eleve_finale>
{reponse_eleve.strip()}
</reecriture_eleve_finale>

Réponds en trois sections :

RAISONNEMENT : deux à trois phrases qui expliquent comment on applique \
les contraintes et comment on propage les modifications de concordance. \
Nomme les catégories grammaticales concernées (accord, temps, pronom…).

REECRITURE_CORRIGEE : le passage entier correctement réécrit, sur une \
seule ligne ou avec ses sauts de ligne originaux si nécessaire. C'est \
la seule section où tu donnes littéralement la forme attendue.

POUR_LA_PROCHAINE_FOIS : une seule phrase qui rappelle le réflexe à \
mobiliser face à ce type de transformation (ex. « quand tu changes le \
sujet, vérifie à chaque verbe et à chaque participe passé »).

Contraintes :
- Tu tutoies l'élève avec bienveillance.
- Pas de liste à puces, pas de titres markdown autres que les trois \
étiquettes de section ci-dessus.
"""


# ============================================================================
# Helpers internes
# ============================================================================


def _ordinal(n: int) -> str:
    return {1: "1ère", 2: "2e", 3: "3e", 4: "4e"}.get(n, f"{n}e")


__all__ = [
    "SYSTEM_PERSONA",
    "ExerciseContext",
    "build_first_eval",
    "build_hint",
    "build_reveal_answer",
    "build_session_synthese",
    "build_reecriture_eval",
    "build_reecriture_hint",
    "build_reecriture_reveal",
]
