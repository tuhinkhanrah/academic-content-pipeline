# Output Contract
- Output ONLY valid <question type="...">...</question> nodes.

## XML Solution Rendering
- Apply the complete format-neutral reasoning and multilingual solution contract from `reasoning_rules.md`.
- Place the complete solution for each question only inside `<generalfeedback format="html">`.
- Render each reasoning step as an HTML paragraph using `<p><strong>Step ...</strong><br/>...</p>`.
- Do not place solution prose directly inside `<quiz>` or outside a question node.

# Feedback Tag Prohibition
- NEVER emit `<correctfeedback>`, `<partiallycorrectfeedback>`, or `<incorrectfeedback>` anywhere in Moodle XML.
- Do not emit these tags even with empty content or default text such as "Your answer is correct.", "Your answer is partially correct.", or "Your answer is incorrect.".
- Put the complete solution explanation only in `<generalfeedback format="html">`.
- Before writing XML, verify that none of the three prohibited tag names appears anywhere in the question node.

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
- Final self-check: no option has duplicated expressions, no raw TeX command appears outside math delimiters, and every math option renders as one expression.

# Option Rules & Numbering Enums (`<answernumbering>`)
- Allowed values are only: `123`, `abc`, `ABCD`, `iii`, `IIII`, `none`.
- Match source numbering exactly: digits use `123`; uppercase letters use `ABCD`; lowercase letters use `abc`; Roman numerals use `iii`.

## Position-Independent Rewriting & Shuffling
- Rewrite position-dependent options such as "Both (1) and (2)" or "None of the above" to be position-independent, or set `<shuffleanswers>false</shuffleanswers>`.

## XML Visual Embedding
- Use `@@PLUGINFILE@@/EXACT_IMAGE_ID` for supplied source visuals and let the post-processor inject the matching `<file>` payload.
- For generated diagrams, use inline SVG inside question HTML CDATA with a `viewBox`, explicit dimensions, and sufficient padding.
- Do not use external URLs, local filesystem paths, or base64 data URIs.
