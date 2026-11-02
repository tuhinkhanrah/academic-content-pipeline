Translate the supplied English HTML assessment into a bilingual HTML5 document.

Target language: {{target_language}}

Requirements:
- Return the complete standalone HTML5 document, preserving its document structure and styling.
- For each question, place its complete original English block first, followed by `<hr/>`, followed by a complete target-language version of that same question.
- Include all choices, the correct answer indication, and the complete solution/reasoning in both language blocks. Preserve option ordering and answer identity exactly.
- Translate only natural-language prose and labels. Do not change question logic, values, numbers, answer keys, marks, identifiers, equations, formulas, units, code, diagram geometry, SVG paths, image references, CSS, or scripts.
- Preserve the English wording exactly. Do not correct or rewrite the English source.
- Do not add, omit, duplicate, reorder, or solve any questions.
- Keep valid HTML and preserve all existing local/inline visuals and references. Return no Markdown fences or commentary.
- Also follow this existing bilingual output contract:

{{language_instruction}}

English master HTML:

{{source_artifact}}
