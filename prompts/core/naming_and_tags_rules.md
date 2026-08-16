# Question Naming

## 1. Dynamic Question Name Assembly
Construct the question name dynamically using **ONLY** the available metadata tokens, strictly joined by single underscores, followed by a plain-text snippet:
`AVAILABLE_TOKENS_JOINED_BY_UNDERSCORE - snippet`

* **Allowed Tokens (In Order if Present):** `EXAM`, `SUBJECT`, `YEAR`, `SECTION`, `CHAPTER`, `TYPOLOGY`, `QNUM`
* **NO PLACEHOLDERS / NO GUESSING:**
  * NEVER invent or assume missing tokens (e.g., do **NOT** add `SECTION_A`, `SECTION`, `SEC1`, `CHAPTER`, or `GENERAL` unless explicitly printed in the paper).
  * Skip any token that is not explicitly present in the source text. Do NOT leave trailing or double underscores (`__`).
* **STRICT FORBIDDEN FORMATS:** Do NOT use spaces, parentheses `()`, or hyphens `-` between the prefix tokens.
* **Formatting:**
  * Tokens MUST be UPPERCASE alphanumeric with spaces removed and joined strictly by underscores (e.g., `JEEMAIN_MATHEMATICS_2025_Q01`).
  * You MUST append a 5–8 word plain-text snippet at the very end, separated by a single hyphen and a space. Example: `_Q01 - Area bounded by the curves` (no HTML, LaTeX, or math delimiters).

## Canonical Examples

* **When Section & Chapter are Explicitly Present:**
  * Name: `WBJEE_CHEMISTRY_2026_CAT1_ORGANIC_MCQ_Q12 - Reaction of benzyl chloride with potassium hydroxide`

* **When Section is Missing (Skip `SECTION` token and tag completely):**
  * Name: `NEET_PHYSICS_2026_THERMODYNAMICS_MCQ_Q05 - Work done during an isothermal expansion process`

* **When Section, Chapter, and Year are Missing:**
  * Name: `NEET_BIOLOGY_MCQ_Q01 - Main function of chloroplast in plant cells`  

# Tag Schema Rules
- Embed taxonomy tags inside a single `<tags>` block placed directly before `</question>`.
- Emit tags strictly in this fixed key order:
  - standard (MANDATORY)
  - year
  - shift
  - lang
  - subject
  - section
  - class
  - topic
  - chapter
  - typology
  - difficulty
  - blooms
  - calculation
  - media
  - multiconcept

## Mandatory Standard Tag (`standard`)
- Every question MUST include a `standard` tag representing the target exams.
- NEVER omit the `standard` tag from the `<tags>` container.
- Tag values must be lowercase `snake_case` (except numeric year).

## Mandatory Tag Formatting Laws
- **CRITICAL:** EVERY single tag MUST follow the `key:value` format. 
- **FORBIDDEN:** NEVER output raw values like `jee_main` or `mathematics`. You MUST output `standard:jee_main` and `subject:mathematics`.
- Tag keys must be lowercase.
- Tag values must be lowercase `snake_case` (except numeric year).
- Tag text format: `<tag><text>key:value</text></tag>`
- Do not emit duplicate keys.
- Do not emit keys outside the fixed schema.
- Emit only tags whose values are available/inferable from the source; if unavailable, omit that key.
- Enum normalization:
  - difficulty: `easy` | `medium` | `hard`
  - blooms: `remember` | `understand` | `apply` | `analyze`
  - calculation: `light` | `moderate` | `heavy`
  - media: `text` | `diagram` | `graph` | `table` | `circuit` | `equation`
  - multiconcept: `true` | `false`

## Language Tag Law (`lang`)
* Every target language must be emitted as its own individual tag using its 2-letter ISO code (e.g., `lang:en`, `lang:bn`).
* **FORBIDDEN:** combined/hyphenated/underscored tags like `lang:en_bn` or `lang:en-bn`. Never join multiple language codes into a single tag string — always emit one `<tag>` per language.
    `<tag><text>lang:bn</text></tag>`