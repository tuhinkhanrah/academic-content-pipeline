# Moodle XML Extraction Rules

- Extract only complete questions that conclude on the supplied page or document context.
- If a question starts in the current context but ends later, defer it until the complete question is available.
- If a question ends in the current context but started earlier, reconstruct and extract the complete question.
- For every extracted question, preserve the source wording, answer choices, labels, units, and relevant visual information.

## Source Visual Preservation
- A question that cannot be understood without a visual must carry that visual into the output.
- Inspect rendered source pages for circuits, graphs, ray diagrams, geometry figures, chemical or biological structures, apparatus, photographs, and data tables.
- For XML output, reference supplied images with `@@PLUGINFILE@@/EXACT_IMAGE_ID` and let the post-processor inject the matching file payload.
- Do not invent, crop away, or silently omit labels, units, axes, or answer-option figures.

## Online Answer Verification
- When online verification is enabled, search for each extracted question using distinctive text fragments.
- Retrieve an authoritative answer key or solution and verify the designated answer before emitting XML.
- Do not claim online verification when it was not performed.
