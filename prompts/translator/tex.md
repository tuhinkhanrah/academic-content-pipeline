Translate the supplied English LaTeX assessment into a bilingual XeLaTeX document.

Target language: {{target_language}}

Requirements:
- Return the complete compilable LaTeX document, preserving its preamble, packages, commands, environments, layout, and document structure.
- For each question, retain the complete original English block first, followed by `\par\medskip`, followed by a complete target-language version of that same question.
- Include all choices, the correct answer indication, and the complete solution/reasoning in both language blocks. Preserve option ordering and answer identity exactly.
- Translate only natural-language prose and labels. Do not change question logic, values, numbers, answer keys, marks, identifiers, equations, formulas, units, code, diagram source, image references, or LaTeX commands.
- Preserve the English wording exactly. Do not correct or rewrite the English source.
- Configure and use the necessary target-language support while retaining the existing XeLaTeX setup. Follow the source document's conventions for marking English text inside the target-language block.
- Do not add, omit, duplicate, reorder, or solve any questions. Return no Markdown fences or commentary.
- Also follow this existing bilingual output contract, including its target-language preamble and font usage:

{{language_instruction}}

English master TeX:

{{source_artifact}}
