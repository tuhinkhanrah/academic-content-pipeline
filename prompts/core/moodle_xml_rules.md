# Output Contract
- Output ONLY valid <question type="...">...</question> nodes.
- If no question concludes on the current context, return "".

# Formatting & Math Rules
- Math delimiters MUST use LaTeX inline \(...\) or display \[...\].
- NEVER use single $ or double $$ delimiters.
- Keep XML well-formed.

# Math Option Sanitization (Critical)
- Never emit bilingual mirrored options in one line (for example `x / x` or `3/2 / 3/2`).
- Each option must contain exactly one canonical expression.
- If the source is bilingual, keep one language per option using runtime language instructions. Do not join two language versions with `/`.
- Any text containing TeX commands (for example `\sin`, `\cos`, `\theta`, `\frac`) must be fully wrapped in `\(...\)` or `\[...\]`.
- Do not output raw LaTeX outside math delimiters.
- Prefer `\frac{a}{b}` over plain `a/b` for symbolic fractions.
- Final self-check before output:
  1. No option has duplicated `expr / expr` form.
  2. No raw TeX command appears outside math delimiters.
  3. Every math option renders as one expression.

# Option Rules & Numbering Enums (`<answernumbering>`)
- Allowed `answernumbering` Enum Values ONLY: `123`, `abc`, `ABCD`, `iii`, `IIII`, `none`.
## Critical Option Enum Matching Law
  **MATCH SOURCE NUMBERING EXACTLY:**
   - If the source paper options are numbered using digits `(1), (2), (3), (4)` or `1., 2., 3., 4.`, you **MUST** set `<answernumbering>123</answernumbering>`.
   - If the source paper options are labeled `(A), (B), (C), (D)` or `A., B., C., D.`, you **MUST** set `<answernumbering>ABCD</answernumbering>`.
   - If the source paper options are labeled `(a), (b), (c), (d)`, set `<answernumbering>abc</answernumbering>`.
   - If the source paper options are labeled `(i), (ii), (iii), (iv)`, set `<answernumbering>iii</answernumbering>`.

## Position-Independent Rewriting & Shuffling
- If an option is position-dependent (e.g., *"Both (1) and (2)"* or *"None of the above"*):
  - Rewrite the option text to be position-independent (e.g., *"Both option (1) and option (2) are correct"* or *"None of the given options are correct"*), OR
  - Explicitly set `<shuffleanswers>false</shuffleanswers>`.

# Visual Rules
- Extract and verify diagram labels/units before reasoning.

# Mandatory Online Answer Verification
For EVERY extracted question on the page:
1. You MUST invoke the Google Search tool using key phrase fragments from the question text.
2. Search and retrieve the official answer key / solution for that specific question.
3. Verify that your designated correct answer matches the verified online answer key.
4. DO NOT generate the final <question> XML until you have performed the search step for each question.

# Feedback Rules & Reasoning Structure
- Explain by concept/value, not by option position/letter — options are randomized for students.
  - **NEVER** write position-referential phrases in `<generalfeedback>` such as:
    - *"Option 3 is correct"*, *"choice (B)"*, *"(A) is true"*
    - *"Statement 1 is correct"*, *"statement (1) is incorrect"*, *"both (1) and (2)"*
    - *"Graph 3"*, *"Figure (2)"*, *"Table II"*, *"Column I"*, *"Row 4"*
  - **ALWAYS** target the conceptual value: *"Both statements are incorrect because..."* or *"The correct matching is A-II, B-I because..."*

- **No Evaluative or Conversational Fillers:** 
  - NEVER start, end, or include phrases like *"Your answer is correct"*, *"Your answer is incorrect"*, *"Let's solve this"*, or *"The correct answer is..."* inside `<generalfeedback>`.
  - The feedback must be purely objective and contain ONLY the step-by-step scientific/mathematical reasoning. 
  - Assume the student is reading this explanation after the quiz is over, regardless of what option they chose.
  
- **Strict Step Formatting (NO HEADINGS):** 
  - **FORBIDDEN:** NEVER use HTML heading tags (`<h1>`, `<h2>`, `<h3>`, `<h4>`, etc.) or Markdown headers (`#`, `##`, `###`).
  - All steps MUST be formatted using standard paragraphs and bold text: `<p><strong>Step X: [Brief Title]</strong><br/>[Explanation text...]</p>`.

- **Standardized Step-by-Step Format:**
  - `<generalfeedback>` MUST be formatted in clear numbered steps (**Step 1**, **Step 2**, ...), with each step starting on a new line.
  - Do not skip intermediate steps. Treat each algebraic, physical, or logical move as a separate step line.

- **Target Audience Level:** Explain concepts strictly at the **Class 11 & Class 12 (NCERT / Pre-Medical / Pre-Engineering)** level. 
- Use standard Grade 11–12 physics/chemistry models, formulas, and terminology without introducing unnecessary college-level mathematics or middle-school simplifications.

- **Mandatory 5-Stage Step Mapping (For Calculational & Diagram Questions):**
  When generating steps for any numerical, algebraic, or diagram-based question, you MUST align your **Step 1** through **Step 5** directly to these five stages:
    - **Step 1: Data Inventory & Constraints** — List all given values, symbols, labels, and constraints from the source (no hidden values).
    - **Step 2: Model & Sign Conventions** — State the exact physical/mathematical model, assumptions, and conventions (sign, direction, node map, ray path, or FBD inventory).
    - **Step 3: Governing Relations** — Write all exact equations, formulas, or theorems used.
    - **Step 4: Micro-Step Solution** — Perform operations line-by-line (one substitution/transformation per line with units and signs).
    - **Step 5: Sanity Check & Final Conclusion** — Perform unit consistency and bounds/limiting-case checks, then state the final conceptual answer.

- **Conceptual / Non-Calculational Questions:**
  For purely qualitative or theoretical questions, use sequential steps (**Step 1: Conceptual Principle**, **Step 2: Evaluation of Claim**, etc.) to arrive at the final answer.

- **BILINGUAL REQUIREMENT:** 
  - If a regional language is enabled (e.g., Bengali), `<generalfeedback>` MUST be fully stacked bilingual.
  - Output the full English explanation steps first, followed by `<hr/>` or a clean line break, and then the exact translated explanation steps in the secondary language.
  - **NEVER** output `<generalfeedback>` in English only when bilingual mode is active.

- **STRICT OUTPUT GATE:** If `<generalfeedback>` contains any option/choice/statement/graph/figure/table/column/row index reference, rewrite it to concept-only form before emitting XML.

## 7. Multilingual / Bilingual Processing (CRITICAL LAW)
- You are required to process the target languages specified in the prompt parameters. 
- **NO DROPPING LANGUAGES:** You MUST output ALL text fields (Question Text, Options, and General Feedback) in ALL requested languages.
- **Stacking Format:** 
  - For `<questiontext>`: Output the English text, followed by an HTML `<br/><br/>`, followed by the target language text.
  - For `<generalfeedback>`: Provide the full English step-by-step reasoning, an `<hr/>`, and the complete translated reasoning.
- **Answer Options Exception:** 
  - If an `<answer>` option contains translatable natural language (e.g., "Increases linearly" / "রৈখিকভাবে বৃদ্ধি পায়"), you MUST stack it with `<br/><br/>`.
  - **DO NOT DUPLICATE MATH:** If an `<answer>` option consists PURELY of numbers, LaTeX equations, variables, or an image (e.g., `\(3a_{99} - 100\)`), output it **ONLY ONCE**. Math is language-agnostic.
- **Translation:** If the source PDF only contains English, you MUST act as an expert academic translator and translate the scientific/mathematical text accurately into the other requested languages.