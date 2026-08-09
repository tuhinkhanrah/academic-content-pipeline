# Output Contract
- Output ONLY valid <question type="...">...</question> nodes.
- No markdown code fences (e.g., ```xml).
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
  - Rewrite the option text to be position-independent (e.g., *"Both option (1) and option (2) are correct"*), OR
  - Explicitly set `<shuffleanswers>false</shuffleanswers>`.

# Visual Rules
- You must output **ONLY** the exact, raw text string `[CROP_BOX:ymin,xmin,ymax,xmax]` inside a standard paragraph tag. The external backend will automatically build the image tags later.
- Never embed CROP_BOX in img/src markdown.
- Extract and verify diagram labels/units before reasoning.

# Mandatory Online Answer Verification
For EVERY extracted question on the page:
1. You MUST invoke the Google Search tool using key phrase fragments from the question text.
2. Search and retrieve the official answer key / solution for that specific question.
3. Verify that your designated correct answer matches the verified online answer key.
4. DO NOT generate the final <question> XML until you have performed the search step for each question.

# Feedback Rules
- Do not reference option letters/numbers in <generalfeedback>.
- Explain by concept/value, not by option position.
- Explain conceptually with clear K-12 steps.
- In generalfeedback, explain the solution in clear numbered steps (Step 1, Step 2, ...) with each step starting on a new line, then state the final answer.
- Do not skip any intermediate step, however small. Include every transformation, substitution, simplification, and unit/sign check explicitly.
- **BILINGUAL REQUIREMENT:** 
  - If a regional language is enabled (e.g., Bengali), `<generalfeedback>` MUST be fully stacked bilingual.
  - Output the full English explanation steps first, followed by `<hr/>` or a clean line break, and then the exact translated explanation steps in the secondary language.
  - **NEVER** output `<generalfeedback>` in English only when bilingual mode is active.

# Shuffling-Safe General Feedback (`<generalfeedback>`)
Because options are randomized for students, explanations cannot point to alphanumeric option labels.
* **NEVER** write phrases like: *"Option 3 is correct"* or *"Hence, (A) is the true choice."* or *"Statement 1 is correct"* in `<generalfeedback>` node.
* **ALWAYS** target the conceptual value: *"Both statements are incorrect because..."* or *"The correct matching is A-II, B-I because..."*

# Position-Independent Choice Rewriting
* If an option reads *"None of these"* or *"Both (1) and (2)"*, rewrite it to position-neutral wording unless shuffling is explicitly disabled:
  * *"None of the given options are correct"*
  * *"Both option (1) and option (2) are correct"*