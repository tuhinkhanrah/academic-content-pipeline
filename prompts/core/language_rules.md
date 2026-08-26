## LANGUAGE AND FORMAT LAWS

### English XML
=== LANGUAGE & FORMAT LAWS ===
- Output all questions, choices, and explanations strictly in English.
- Output only complete `<question ...>...</question>` nodes.
- The question type must be in the root attribute, for example `<question type="multichoice">`.

### Bilingual XML
=== BILINGUAL LANGUAGE & FORMAT LAWS ({{primary_lang}} + {{target_secondary}}) ===
Generate every question, choice, and feedback explanation in a stacked bilingual format:
1. In `<questiontext>` and `<generalfeedback>`, provide the complete {{primary_lang}} text first, followed by the complete {{target_secondary}} translation.
2. `<generalfeedback>` must be fully bilingual. Every reasoning step and calculation step must appear in both languages.
3. Separate language versions in `<questiontext>` and `<generalfeedback>` with a clean line break or `<hr/>`.
4. For choices, output only the {{primary_lang}} option text.
5. Output only complete `<question ...>...</question>` nodes.
6. Do not translate mathematical symbols, formulas, chemical equations, or LaTeX variables inside `\(...\)` or `\[...\]` delimiters.
7. Emit only individual language tags, never combined tags.

### English HTML
=== LANGUAGE & FORMAT LAWS (HTML5) ===
- Output all questions, choices, and statements strictly in English using clean HTML5.

### Bilingual HTML
=== BILINGUAL LANGUAGE & FORMAT LAWS (HTML5: {{primary_lang}} + {{target_secondary}}) ===
1. For every bilingual question, provide the complete English question block first, followed by `<hr/>`, followed by the complete {{target_secondary}} translated block.
2. Each language version must be a self-contained question unit with its own full set of choices.
3. Do not merge English and translated option values into one list.
4. Each language block must include the complete solution and reasoning.

### English TeX
=== LANGUAGE & FORMAT LAWS (LaTeX XeLaTeX) ===
- Output all questions, choices, statements, and solutions strictly in English using native LaTeX.

### Bilingual TeX
=== BILINGUAL LANGUAGE & FORMAT LAWS (LaTeX / XeLaTeX: {{primary_lang}} + {{target_secondary}}) ===
1. Configure the requested secondary languages in the preamble:
```latex
{{preamble_snippet}}
```
2. For every bilingual question, render the complete English question and solution first, followed by `\par\medskip`, followed by the complete translated block.
3. Each language version must be a self-contained question unit with its own full set of choices.
{{font_usage_notes}}
4. Use native LaTeX and do not use HTML document tags.
