Translate the supplied Moodle XML into a bilingual Moodle XML document.

Target language: {{target_language}}

Requirements:
- Return one complete, well-formed Moodle `<quiz>` document.
- Preserve the XML structure, question order, question types, attributes, IDs, grades, fractions, answer keys, penalties, hints, existing tags, names, and all non-translatable values exactly. The pipeline adds the target language tag after validating your XML.
- In each `<questiontext>` and `<generalfeedback>`, retain the complete original English content first, then add `<hr/>`, then the complete target-language translation. Keep any existing HTML markup valid.
- Translate only natural-language prose in `<questiontext>` and `<generalfeedback>`. Keep answer choices and their values in English, as required by the existing bilingual Moodle XML convention.
- Do not translate or change formulas, mathematical notation, chemical equations, code, numbers, units, variables, SVG markup, image references, or Moodle placeholders.
- Do not add, remove, combine, split, or reorder questions, choices, answers, feedback nodes, or any XML elements.
- Escape XML correctly. Do not wrap the result in a code fence or include explanations outside the XML.
- Also follow this existing bilingual output contract:

{{language_instruction}}

English master XML:

{{source_artifact}}
