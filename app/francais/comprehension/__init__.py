"""Sous-épreuve « Compréhension et compétences d'interprétation » du DNB français.

Parcours élève : l'élève lit un texte littéraire, puis répond question par
question. L'IA n'écrit jamais la réponse à sa place — elle propose jusqu'à
3 indices gradués, puis révèle la bonne réponse uniquement après le 3e indice
si l'élève bloque encore.

Modules :
- `models.py`    : table `FrenchExercise` (banque d'annales) + schémas Pydantic.
- `loader.py`    : charge les JSON extraits par `scripts/extract_french_exercises.py`.
- `prompts.py`   : persona système + builders d'indices et d'évaluation.
- `pedagogy.py`  : orchestration (évaluation d'une réponse, génération d'indice).
- `routes.py`    : routes FastAPI de la sous-épreuve.
"""
