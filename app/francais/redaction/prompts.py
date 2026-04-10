"""
Prompts pédagogiques pour la sous-épreuve « Rédaction » du DNB français.

Quatre builders, un par étape Albert du parcours :

- ``build_help_choose`` (étape 1) — coup de pouce socratique pour aider
  l'élève à choisir entre imagination et réflexion + repérer ce qui est
  attendu (sortie en prose, pas de plan).
- ``build_first_eval_redaction`` (étape 3) — première évaluation du brouillon.
- ``build_second_eval_redaction`` (étape 5) — comparaison v1/v2.
- ``build_final_correction_redaction`` (étape 7) — correction finale fond + forme.

Règles cardinales (toutes héritées du DC histoire-géo, cf. gotchas §5.1 et
§5.2 de HANDOFF.md) :

1. **Anti-hallucination méta** — strict cloisonnement par balises XML :
   ``<sujet>``, ``<option_choisie>``, ``<contexte_texte_support>`` (référence
   au texte support si elle existe), ``<proposition_eleve>`` /
   ``<proposition_eleve_v1/v2>`` / ``<copie_eleve>``. Quand l'IA dit « tu as
   bien fait X », elle doit vérifier que X est dans une balise élève, jamais
   dans le contexte.

2. **L'IA ne rédige jamais à la place** — pas de plan tout fait, pas de
   passages rédigés. Les questions socratiques orientent, l'élève cherche.

3. **Conseil final = une seule phrase d'orientation** — interdiction de
   dicter un plan en prose (« commence par X, puis Y… ») ou en liste
   numérotée. Une seule phrase qui pointe une priorité (ex. « la prochaine
   fois, étoffe ta partie sur les sentiments du narrateur »).

4. **Pas d'invention de citations littéraires** — si l'IA n'est pas sûre
   d'un nom, d'une date, d'un titre, elle dit « à vérifier dans ton manuel »
   plutôt que d'inventer. Les sources extraites par RAG sont citées entre
   crochets : ``[méthodo]``, ``[programme]``, ``[corrigé]``.

Le contexte RAG (méthodo rédaction, programme français) est injecté dans
une balise ``<context>...</context>`` à l'intérieur du message user, jamais
dans le system prompt.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.rag import RagPassage
from app.francais.redaction.models import RedactionSubject, SujetOption


# ============================================================================
# Persona système
# ============================================================================

SYSTEM_PERSONA = """\
Tu es Albert, un tuteur bienveillant qui aide un·e élève de 3e à préparer le \
DNB de français, plus précisément l'épreuve de RÉDACTION (40 points sur 50).

Comment tu parles :
- Tu tutoies l'élève. Tu t'adresses à un·e ado de 14 ans.
- Phrases courtes. Mots simples mais corrects. Tu utilises le vocabulaire \
littéraire de base quand c'est utile (« narrateur », « registre », « point \
de vue », « champ lexical », « argumentation »).
- Tu es chaleureux sans être mièvre. Tu valorises d'abord ce qui est \
réussi, puis seulement tu signales ce qui manque.
- Tu signales UNE priorité d'amélioration à la fois, jamais dix.
- Tu juges le travail fourni, jamais l'élève.

Ce que tu ne fais JAMAIS :
- Tu ne rédiges JAMAIS la rédaction à la place de l'élève, même sur \
demande explicite. Pas de passage tout prêt, pas de phrase d'introduction \
modèle, pas de paragraphe-exemple. Si on te le demande, tu expliques \
gentiment que ton rôle est d'aider à progresser, pas de faire à la place.
- Tu ne donnes JAMAIS de plan tout fait (ni numéroté « I.1, I.2 », ni en \
prose « commence par X, puis parle de Y »). C'est à l'élève de construire \
son plan.
- Tu n'inventes JAMAIS de noms d'auteurs, de titres d'œuvres, de citations. \
Si tu hésites, tu dis « vérifie dans ton manuel » plutôt que de deviner.
- Tu ne donnes que des éléments présents dans les sources fournies entre \
balises <context>...</context>. Si une information n'y figure pas et que \
tu n'en es pas sûr, tu ne l'utilises pas.

RÈGLE ABSOLUE sur les sources d'information (à ne jamais confondre) :
- Tout ce qui est entre balises <context>...</context> est une RÉFÉRENCE \
extérieure (programme officiel, fiche méthodo, corrigé). L'élève ne l'a \
PAS écrite, ne l'a PAS forcément lue.
- Tout ce qui est entre balises <proposition_eleve>...</proposition_eleve> \
ou <copie_eleve>...</copie_eleve> est ce que l'élève a RÉELLEMENT écrit. \
C'est la seule chose à laquelle tu dois te référer quand tu dis « tu as \
bien fait X » ou « tu n'as pas mentionné Y ».
- Quand tu valorises ou reproches quelque chose à l'élève, vérifie d'abord \
que ce quelque chose est bien (ou n'est bien PAS) dans <proposition_eleve> \
/ <copie_eleve>. Ne confonds JAMAIS ce qui vient du <context> avec ce qui \
vient de l'élève. C'est une erreur très grave qui détruit la confiance.

Format de tes réponses :
- Pas de listes à rallonge. Maximum 3 points quand tu listes quelque chose.
- Pas de titres en gras sauf si c'est vraiment utile pour la lisibilité.
- Réponses concises : l'élève doit pouvoir te lire en 30 secondes (sauf \
pour la correction finale, qui peut être plus longue).
"""


# ============================================================================
# Helpers
# ============================================================================


@dataclass
class RedactionContext:
    """Vue minimale d'un sujet de rédaction passée aux builders.

    Construite dans pedagogy.py à partir du JSON chargé en DB. On garde
    seulement ce qui est utile aux prompts (pas la table SQLModel brute)
    pour ne pas exposer de couplage.
    """

    annee: int
    centre: str
    option: SujetOption
    texte_support_ref: str | None  # courte étiquette si le sujet renvoie au texte


def _format_context(passages: list[RagPassage]) -> str:
    if not passages:
        return ""
    blocks = []
    for p in passages:
        blocks.append(f"[{p.source}]\n{p.content.strip()}")
    body = "\n\n---\n\n".join(blocks)
    return f"<context>\n{body}\n</context>\n\n"


_OPTION_LABELS = {
    "imagination": "écrit d'invention",
    "reflexion": "écrit d'argumentation",
}


def _format_option(opt: SujetOption) -> str:
    nature = _OPTION_LABELS.get(opt.type, opt.type)
    parts = [
        f"Type : {opt.type} ({nature})",
        f"Étiquette officielle : {opt.numero}",
    ]
    if opt.amorce:
        parts.append(f"Amorce : {opt.amorce}")
    parts.append(f"Consigne : {opt.consigne}")
    if opt.contraintes:
        parts.append("Contraintes explicites :")
        for c in opt.contraintes:
            parts.append(f"  - {c}")
    if opt.longueur_min_lignes:
        parts.append(f"Longueur indicative : ~{opt.longueur_min_lignes} lignes minimum")
    if opt.reference_texte_support:
        parts.append(f"Renvoi au texte support : {opt.reference_texte_support}")
    return "\n".join(parts)


def _format_subject_block(ctx: RedactionContext) -> str:
    lines = [
        f"Annale DNB {ctx.annee} — {ctx.centre}",
        "",
        _format_option(ctx.option),
    ]
    if ctx.texte_support_ref:
        lines.append("")
        lines.append(
            f"<contexte_texte_support>\n{ctx.texte_support_ref}\n</contexte_texte_support>"
        )
        lines.append(
            "(Le sujet renvoie à ce texte support de l'épreuve de compréhension. "
            "Tu n'as pas le texte intégral — n'invente pas son contenu, parle-en "
            "uniquement comme d'un appui que l'élève doit aller relire.)"
        )
    return "\n".join(lines)


# ============================================================================
# Étape 1 — Aide au choix / décryptage du sujet
# ============================================================================
#
# Déclenchée à la demande de l'élève depuis la page des deux options. Pas
# de plan, pas de pistes : seulement des questions ciblées qui aident à
# comparer imagination vs réflexion, à repérer les contraintes et à éviter
# de partir hors sujet.
# ============================================================================


def build_help_choose(
    subject: RedactionSubject,
    rag: list[RagPassage],
) -> list[dict]:
    context_block = _format_context(rag)
    user = f"""{context_block}\
L'élève hésite encore entre les deux sujets de rédaction proposés ci-dessous \
et te demande un coup de pouce pour comprendre ce qu'on attend de chacun.

<sujet>
Annale DNB {subject.source.annee} — {subject.source.centre}

OPTION A (imagination) :
{_format_option(subject.sujet_imagination)}

OPTION B (réflexion) :
{_format_option(subject.sujet_reflexion)}
</sujet>

Ta mission : aider l'élève à mieux COMPRENDRE chaque option, sans choisir à \
sa place et sans lui souffler d'idées. Tu ne donnes aucune piste de récit, \
aucun argument tout prêt, aucun exemple littéraire.

Structure EXACTE de ta réponse (français, phrases courtes, niveau 3e) :

1. Une phrase qui dit en quoi les deux options sont fondamentalement \
différentes (« l'option A te demande de raconter / inventer, l'option B te \
demande de défendre une idée » — adapte au sujet précis).

2. Un petit bloc « Pour l'imagination, demande-toi : » suivi de 2 questions \
ciblées qui aident l'élève à voir si ce sujet lui parle (capacité à \
inventer ce qu'on demande, contraintes à respecter, type de récit attendu).

3. Un petit bloc « Pour la réflexion, demande-toi : » suivi de 2 questions \
ciblées qui aident l'élève à voir s'il a assez à dire (exemples concrets \
qu'il pourrait mobiliser, position personnelle, capacité à argumenter).

4. Une dernière phrase d'encouragement qui rappelle qu'il n'y a pas de \
« meilleure » option : la bonne, c'est celle où l'élève se sent le plus \
inspiré·e.

Règles strictes :
- UNIQUEMENT des questions dans les blocs 2 et 3. Aucune affirmation qui \
donnerait la réponse (ex : ne dis PAS « parle de l'amitié », mais « as-tu \
en tête une situation personnelle qui colle au thème ? »).
- Tu ne suggères AUCUN récit, AUCUN argument, AUCUN exemple littéraire \
précis.
- Tu ne dis pas laquelle choisir.
- Pas de citations entre crochets ici (le contexte te sert à calibrer tes \
questions, pas à être cité).
- Tutoiement, ton chaleureux. Maximum ~180 mots au total.
"""
    return [
        {"role": "system", "content": SYSTEM_PERSONA},
        {"role": "user", "content": user},
    ]


# ============================================================================
# Étape 3 — Première évaluation du brouillon / plan
# ============================================================================
#
# L'élève a choisi son option, écrit un plan + premières idées. On évalue
# en mode socratique : valoriser ce qui tient, poser 3 questions ouvertes
# pour creuser ce qui manque.
# ============================================================================


def build_first_eval_redaction(
    ctx: RedactionContext,
    student_proposal: str,
    rag: list[RagPassage],
) -> list[dict]:
    context_block = _format_context(rag)
    subject_block = _format_subject_block(ctx)

    user = f"""{context_block}\
Voici le sujet choisi par l'élève :

<sujet>
{subject_block}
</sujet>

<option_choisie>
{ctx.option.type}
</option_choisie>

Voici ce que l'élève a RÉELLEMENT écrit comme brouillon (plan + idées). \
C'est la SEULE chose à laquelle tu dois te référer pour dire « tu as bien \
fait X » ou « il te manque Y » :

<proposition_eleve>
{student_proposal.strip()}
</proposition_eleve>

Ta mission : aider l'élève à creuser son brouillon SANS lui donner le plan \
ni rédiger un passage à sa place.

Structure exacte de ta réponse :

1. UNE phrase qui valorise concrètement quelque chose de PRÉSENT dans son \
brouillon (« tu as bien identifié X », « ton point de départ sur Y est \
prometteur »).

2. Trois questions ouvertes, dans cet ordre :
   - Une question qui vérifie qu'il a bien compris ce que la consigne \
attend (en s'appuyant sur le verbe-clé / les contraintes du sujet).
   - Une question qui pointe une dimension importante mais absente ou \
floue dans son brouillon (selon le type de sujet : pour l'imagination → \
descriptions, sentiments, déroulement, registre ; pour la réflexion → \
exemple précis, contre-argument, ouverture).
   - Une question qui vérifie l'équilibre / l'organisation de son plan.

3. Pour chaque question, tu peux indiquer entre crochets la source qui \
justifie ton choix : [méthodo], [programme], [corrigé]. Tu n'es pas obligé \
si la question est purement organisationnelle.

Règles strictes :
- AUCUNE affirmation directe du type « il manque X » : seulement des \
questions.
- AUCUN plan rédigé, aucune idée toute prête, aucun passage modèle.
- Pas plus de 3 questions au total dans le bloc 2.
- Tu ne réécris pas le brouillon de l'élève. Tu pointes ce qui mérite \
d'être creusé, c'est tout.
- Si l'élève a écrit un récit (option imagination), tu ne juges pas la \
qualité littéraire d'un brouillon — c'est trop tôt. Tu interroges la \
direction qu'il prend.
- Maximum ~200 mots au total.
"""
    return [
        {"role": "system", "content": SYSTEM_PERSONA},
        {"role": "user", "content": user},
    ]


# ============================================================================
# Étape 5 — Seconde évaluation (après re-proposition de l'élève)
# ============================================================================
#
# Compare v1 / v2, valorise les progrès AVANT de signaler ce qui reste à
# travailler. Si la v2 est solide, on ouvre la voie vers la rédaction.
# ============================================================================


def build_second_eval_redaction(
    ctx: RedactionContext,
    first_proposal: str,
    second_proposal: str,
    rag: list[RagPassage],
) -> list[dict]:
    context_block = _format_context(rag)
    subject_block = _format_subject_block(ctx)

    user = f"""{context_block}\
Voici le sujet :

<sujet>
{subject_block}
</sujet>

<option_choisie>
{ctx.option.type}
</option_choisie>

Première proposition que l'élève avait RÉELLEMENT écrite :

<proposition_eleve_v1>
{first_proposal.strip()}
</proposition_eleve_v1>

Nouvelle proposition que l'élève vient de RÉELLEMENT écrire :

<proposition_eleve_v2>
{second_proposal.strip()}
</proposition_eleve_v2>

Rappel critique : quand tu dis « tu as progressé sur X » ou « il te manque \
toujours Y », tu te réfères UNIQUEMENT à ce qui est dans les balises \
ci-dessus. Jamais à ce qui est dans <context>.

Ta mission : évaluer la progression entre les deux versions et orienter la \
suite.

Structure exacte de ta réponse :

1. UNE phrase qui souligne concrètement un progrès réel observé entre v1 \
et v2 (« tu as bien ajouté X », « ton plan est maintenant plus équilibré \
parce que… »).

2. UNE question ouverte sur ce qui reste flou ou manquant dans la v2.

3. Si tu estimes que la v2 est désormais assez solide pour rédiger, dis-le \
clairement : « tu peux passer à la rédaction ». Sinon, suggère un dernier \
point à clarifier en UNE phrase.

Règles strictes :
- Pas plus de 2 points abordés au total. On ne surcharge pas l'élève.
- Pas de plan tout fait, pas de passage rédigé.
- Ne reproche RIEN qui aurait déjà été corrigé entre v1 et v2.
- Si tu dois citer une source du contexte, fais-le entre crochets : \
[méthodo], [programme], [corrigé].
- Maximum ~150 mots au total.
"""
    return [
        {"role": "system", "content": SYSTEM_PERSONA},
        {"role": "user", "content": user},
    ]


# ============================================================================
# Étape 7 — Correction finale de la rédaction
# ============================================================================
#
# La sortie la plus longue et la plus importante. Deux blocs FOND et FORME,
# puis UNE seule phrase d'orientation. Pas de version corrigée, pas de
# réécriture, pas de plan dicté.
# ============================================================================


_FOND_CHECKLIST_IMAGINATION = (
    "Pour ce sujet d'imagination, regarde :\n"
    "- Adéquation au sujet : l'élève raconte-t-il bien ce qui était "
    "demandé ? Respect des contraintes (point de vue, registre, "
    "personnages, époque) ?\n"
    "- Construction du récit : le récit a-t-il un début, un milieu, une "
    "fin clairs ? Y a-t-il une progression ?\n"
    "- Richesse : descriptions, sentiments, dialogues éventuels, "
    "vocabulaire varié, registre adapté.\n"
    "- Cohérence : pas de contradictions internes, logique du récit tenue "
    "jusqu'au bout."
)

_FOND_CHECKLIST_REFLEXION = (
    "Pour ce sujet de réflexion, regarde :\n"
    "- Adéquation au sujet : l'élève répond-il vraiment à la question "
    "posée ? Sans se contenter d'une opinion personnelle non argumentée ?\n"
    "- Argumentation : les arguments sont-ils clairement formulés ? "
    "Y a-t-il au moins 2 arguments distincts ?\n"
    "- Exemples : les exemples sont-ils précis (œuvres, situations "
    "concrètes) et VRAIMENT illustratifs de l'argument ? Pas d'exemples "
    "vagues.\n"
    "- Position personnelle : l'élève prend-il position clairement ?"
)


def build_final_correction_redaction(
    ctx: RedactionContext,
    student_text: str,
    rag: list[RagPassage],
) -> list[dict]:
    context_block = _format_context(rag)
    subject_block = _format_subject_block(ctx)
    fond_checklist = (
        _FOND_CHECKLIST_IMAGINATION
        if ctx.option.type == "imagination"
        else _FOND_CHECKLIST_REFLEXION
    )

    user = f"""{context_block}\
Voici le sujet :

<sujet>
{subject_block}
</sujet>

<option_choisie>
{ctx.option.type}
</option_choisie>

Voici la rédaction que l'élève a RÉELLEMENT écrite. C'est la SEULE chose à \
laquelle tu dois te référer quand tu dis « tu as bien fait X » ou « tu \
n'as pas fait Y ». Ne confonds JAMAIS ce texte avec les éléments du \
<context> (qui sont des références extérieures, pas ce que l'élève a écrit) :

<copie_eleve>
{student_text.strip()}
</copie_eleve>

Ta mission : faire une correction structurée fond + forme. Tu n'es pas un \
correcteur d'orthographe automatique — tu es un tuteur qui aide à \
progresser pour la prochaine rédaction.

Structure EXACTE :

===== FOND =====

{fond_checklist}

Donne au maximum 2 points forts puis au maximum 2 manques importants. \
Pour chaque manque, indique entre crochets la source qui le justifie : \
[méthodo], [programme] ou [corrigé].

===== FORME =====

- Introduction : pose-t-elle le contexte (récit) ou la question (réflexion) ?
- Organisation : paragraphes distincts ? transitions entre eux ?
- Conclusion : ferme-t-elle bien le récit ou la réflexion ?
- Longueur : suffisante pour le sujet (en général 30 à 50 lignes attendues) ?
- Langue : présence d'erreurs d'orthographe / syntaxe — signale qu'il y en \
a et cite 2 à 3 exemples (sans corriger toute la copie).

Termine par UNE SEULE phrase d'encouragement et UNE SEULE phrase de \
conseil prioritaire pour la prochaine rédaction.

Règles STRICTES (à respecter à la lettre) :
- Tu ne réécris JAMAIS la rédaction de l'élève, ni un paragraphe, ni une \
phrase modèle. Tu signales, tu pointes, tu interroges, mais tu ne produis \
aucun texte « comme exemple ».
- Pour ton conseil final, tu ne donnes JAMAIS un plan, ni sous forme \
numérotée (« I.1, I.2 »), ni sous forme de prose (« commence par X, puis \
parle de Y, termine par Z »), ni avec des titres de parties suggérés. \
Ton conseil final doit tenir en UNE SEULE phrase qui donne une orientation \
générale sans dire comment structurer le texte. Exemples acceptables : \
« la prochaine fois, étoffe tes descriptions sensorielles » ou « travaille \
surtout l'enchaînement entre tes arguments ». Exemples INTERDITS : toute \
phrase qui énumère les parties du plan, suggère des titres, ou dicte \
l'ordre des idées.
- Tu n'inventes AUCUNE référence littéraire (auteur, titre, date) que tu \
ne serais pas sûr de trouver dans le contexte fourni. Dans le doute, dis \
« vérifie dans ton manuel ».
- Pour l'orthographe : signale la présence d'erreurs et cite 2 ou 3 \
exemples, mais ne corrige PAS la copie entière.
- Cite toujours tes sources entre crochets quand tu mentionnes un attendu \
méthodologique ou un point de programme : [méthodo], [programme], [corrigé].
"""
    return [
        {"role": "system", "content": SYSTEM_PERSONA},
        {"role": "user", "content": user},
    ]


# ============================================================================
# Refus — quand l'élève demande à Albert de rédiger à sa place
# ============================================================================


REFUSAL_REDACTION = (
    "Je ne vais pas rédiger à ta place — mon rôle, c'est de t'aider à "
    "progresser, pas de faire le travail pour toi (et au DNB, je ne serai "
    "pas là !). En revanche, je peux t'aider autrement : tu veux qu'on "
    "reprenne ton plan ensemble, ou qu'on creuse une idée précise ?"
)


__all__ = [
    "SYSTEM_PERSONA",
    "RedactionContext",
    "build_help_choose",
    "build_first_eval_redaction",
    "build_second_eval_redaction",
    "build_final_correction_redaction",
    "REFUSAL_REDACTION",
]
