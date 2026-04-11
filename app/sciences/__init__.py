"""Package de la matière DNB Sciences.

L'épreuve Sciences du DNB (1 h, 50 points, coefficient 2) porte sur
**deux disciplines** tirées parmi Physique-Chimie, SVT et Technologie
(30 min et 25 points chacune). On ne sait pas d'avance lesquelles
tombent le jour J — d'où le choix de couvrir les trois disciplines
côté entraînement.

Vague 1 : une seule forme d'entraînement, l'épreuve « Révision par
thème » (cf. `app/sciences/revision/`). Les questions sont texte-only
(pas de figure obligatoire pour répondre) et scorées en hybride
déterministe / Albert. L'épreuve blanche chronométrée et les annales
complètes restent V2 (bloquées par le rendu des documents visuels).
"""

# Identifiant de matière utilisé comme clé dans les dicts matière-indexés
# de `app.core.rag` (COLLECTION_LABELS, TASK_COLLECTIONS,
# FALLBACK_COLLECTION_IDS). Doit matcher le nom du package Python.
SUBJECT_KIND = "sciences"
