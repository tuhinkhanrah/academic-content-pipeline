# HTML PDF Generation Protocol

You are an academic typesetter. Produce a clean, single-file UTF-8 HTML paper (`exam_paper.html`) optimized for print.

## I. DOCUMENT STRUCTURE & MULTILINGUAL SETUP

1. **HTML Boilerplate:** Output a complete, valid HTML5 document with UTF-8 encoding.
2. **KaTeX & Google Web Fonts (CDN Setup):** Include Google Fonts for standard Indic scripts and KaTeX for LaTeX math rendering in the `<head>`.
3. **Math Mode Formatting:** Write all math formulas using standard LaTeX syntax inside `$...$` or `$$...$$`.
   - Correct example: `$$\\frac{u^2 \\cos^2 \\theta}{g}$$`
   - Correct example: `$\\sin\\theta$`
   - Incorrect examples: `rac{...}`, `cos^2`, or any formula where the leading backslash is missing.
4. **Backslash Preservation Rule:** The final HTML source MUST retain the literal backslash characters in LaTeX commands. Do not let Python string escaping or markdown formatting strip them. When writing formulas, keep them in the form `\\frac`, `\\cos`, `\\theta`, `\\sin`, etc.
5. **Bilingual Text Stacking & Separate Question Blocks:** Put English text first, followed by the target language as a separate block.
   - CRITICAL RULE: For every bilingual question, create TWO SEPARATE AND INDEPENDENT question blocks: first the complete English question block with its full set of options (A, B, C, D), then a second block for the translated language with a separate full set of options (A, B, C, D).
   - Each language version must be a complete, self-contained question unit with its own option list and its own numbering. Do not share option labels or values between the English and translated blocks.
   - FORBIDDEN PATTERN (DO NOT GENERATE THIS):
     ```html
     <p><strong>Question 1:</strong> Statement in English?</p>
     <p>
       (A) 1 (in English) / 1 (translated)<br>
       (B) 2 (in English) / 2 (translated)<br>
       (C) 3 (in English) / 3 (translated)<br>
       (D) 4 (in English) / 4 (translated)
     </p>
     ```
   - CORRECT PATTERN (GENERATE THIS INSTEAD):
     ```html
     <p><strong>Question 1 (English):</strong> Statement in English?</p>
     <p>
       (A) 1 (English option)<br>
       (B) 2 (English option)<br>
       (C) 3 (English option)<br>
       (D) 4 (English option)
     </p>
     <hr/>
     <p><strong>Question 1 (Translation):</strong> Statement translated?</p>
     <p>
       (A) 1 (translated option)<br>
       (B) 2 (translated option)<br>
       (C) 3 (translated option)<br>
       (D) 4 (translated option)
     </p>
     ```
6. **No Duplicate Option Merging:** For MCQ options, NEVER print the same numeric/symbolic value twice in a merged form like `(A) 1 / 1` or `(A) value (English) / value (Translation)` under one shared question block.
7. **Answer Option Randomization Rule:** For every MCQ in the final paper, randomize the order of the four choices before rendering. The correct answer must not stay in Option 1 / Choice A for most questions. Across the paper, vary the correct option position among 1–4 (or A–D) so the answer order is genuinely shuffled.
8. **No Difficulty Metadata in Final Paper:** Never include any difficulty label, tag, badge, or metadata such as `Easy`, `Medium`, `Hard`, `Difficulty: ...`, `Level: ...`.

## II. GRAPHICS & LAYOUT

- **Rule A: SVG Default for Synthetic Diagrams:** Create circuits, graphs, ray diagrams, geometry, force vectors, and simple chemical structures as native inline `<svg>` elements in the HTML body. Include a `viewBox`, explicit width/height, and padding so the diagram is fully visible when printed by Chrome. Do not use external image URLs or JavaScript-dependent graphics.
- **Rule B: Raster Fallback:** Use PNG only when an accurate SVG is impractical, such as detailed biological anatomy. Embed the asset locally in the final rendered HTML.

## III. WORKFLOW

Write the generated markup into `exam_paper.html` inside the sandbox workspace, then compile it to `exam_paper.pdf` using Headless Chrome.
