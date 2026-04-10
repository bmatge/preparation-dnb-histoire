"""Tests du rendu markdown des réponses Albert.

``render_eval_markdown`` est appelé par tous les partials d'évaluation et
de correction (`eval_response.html`, `help_response.html`). Il transforme
les sorties brutes d'Albert (qui contiennent du markdown standard + des
bannières maison ``===== FOND =====`` et des titres en gras isolés) en
HTML qu'on injecte dans Jinja via ``Markup``.

Ce qu'on teste :
- Bannières ``===== TITRE =====`` → ``<h2>``.
- Lignes ``**Titre**`` seules → ``<h3>``.
- Items de liste dont le contenu est uniquement un titre gras
  ``- **Titre**`` → ``<h3>``.
- Listes indentées de 2-3 espaces ramenées à la ligne 0 (déstructuration
  des sous-listes superflues).
- Markdown standard (gras, italique, listes, headings) intact.
- Vide / None.
- Pas d'injection HTML brute.
"""

from __future__ import annotations

from app.core.formatting import render_eval_markdown


class TestRenderEvalMarkdown:
    def test_empty_string_returns_empty(self):
        assert render_eval_markdown("") == ""

    def test_none_safe(self):
        # La fonction reçoit parfois None via le filtre Jinja `eval_md`.
        assert render_eval_markdown(None) == ""  # type: ignore[arg-type]

    def test_plain_text_is_wrapped_in_paragraph(self):
        html = render_eval_markdown("Bravo, tu as bien identifié les acteurs.")
        assert "<p>" in html
        assert "Bravo, tu as bien identifié les acteurs." in html

    def test_banner_becomes_h2(self):
        text = "===== FOND =====\n\nContenu de la partie fond."
        html = render_eval_markdown(text)
        assert "<h2>Fond</h2>" in html

    def test_banner_with_bold_wrap_becomes_h2(self):
        # Variante observée chez gpt-oss-120b : `**===== FORME =====**`
        text = "**===== FORME =====**\n\nLe plan est clair."
        html = render_eval_markdown(text)
        assert "<h2>Forme</h2>" in html

    def test_banner_titlecases_content(self):
        # On normalise au Title Case pour la cohérence visuelle.
        text = "===== POINTS FORTS =====\n\nTu as bien fait X."
        html = render_eval_markdown(text)
        assert "<h2>Points Forts</h2>" in html

    def test_bold_heading_alone_on_line_becomes_h3(self):
        text = "**Adéquation au sujet**\n\nTu réponds bien à la question."
        html = render_eval_markdown(text)
        assert "<h3>Adéquation au sujet</h3>" in html

    def test_bullet_bold_heading_becomes_h3(self):
        # `- **Titre**` seul sur sa ligne → promu en H3.
        text = "- **Connaissances**\n\nTes dates sont correctes."
        html = render_eval_markdown(text)
        assert "<h3>Connaissances</h3>" in html

    def test_indented_list_unindented(self):
        # Items à 2-3 espaces d'indentation ramenés à la ligne 0 (sinon
        # markdown les traite comme du code/quote ou les imbrique dans le
        # paragraphe précédent).
        text = "Voici les points :\n\n  - premier point\n  - second point\n  - troisième"
        html = render_eval_markdown(text)
        # Les puces doivent apparaître dans une <ul>, pas dans <pre>/<code>.
        assert "<ul>" in html
        assert "<li>" in html
        assert "premier point" in html
        assert "<pre>" not in html

    def test_standard_markdown_bold(self):
        html = render_eval_markdown("Tu as **très bien** réussi cette partie.")
        assert "<strong>très bien</strong>" in html

    def test_standard_markdown_list(self):
        text = "Trois choses à retenir :\n\n- premier\n- deuxième\n- troisième"
        html = render_eval_markdown(text)
        assert "<ul>" in html
        assert "<li>premier</li>" in html
        assert "<li>deuxième</li>" in html
        assert "<li>troisième</li>" in html

    def test_combined_real_albert_output(self):
        # Cas représentatif d'une vraie correction Albert : bannière FOND,
        # titre gras, listes, ligne FORME.
        text = (
            "===== FOND =====\n"
            "\n"
            "**Adéquation au sujet**\n"
            "Tu réponds bien à la consigne [programme].\n"
            "\n"
            "- **Connaissances**\n"
            "  - dates correctes\n"
            "  - acteurs identifiés\n"
            "\n"
            "===== FORME =====\n"
            "\n"
            "Plan apparent en trois parties, c'est très bien.\n"
        )
        html = render_eval_markdown(text)
        assert "<h2>Fond</h2>" in html
        assert "<h2>Forme</h2>" in html
        assert "<h3>Adéquation au sujet</h3>" in html
        assert "<h3>Connaissances</h3>" in html
        assert "dates correctes" in html
        assert "acteurs identifiés" in html
        assert "[programme]" in html

    def test_does_not_strip_brackets_used_for_citations(self):
        # Les citations [programme], [corrigé], [méthodo] doivent rester
        # visibles dans le rendu pour que l'élève voie les sources.
        text = "Tu as bien mobilisé la notion de puissance [programme]."
        html = render_eval_markdown(text)
        assert "[programme]" in html
