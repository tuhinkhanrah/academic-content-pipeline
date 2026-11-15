# NCERT Exemplar extraction

These source-specific rules take precedence over conflicting general extraction,
exam-profile and template instructions. They apply only to NCERT Exemplar
extraction, not to question generation or other source materials.

Extract every complete eligible source question, in source order, from MCQ I,
MCQ II, multiple-choice comprehension, Matching and Assertion-Reason sections.
Exclude all questions from VSA, SA and LA sections (including their full heading
names) and any other non-eligible sections. Do not convert them into MCQs, even if
they are on the same page as eligible questions or continue onto another page.
Do not treat answer keys, worked solutions, instructions or section headings as
additional questions. Keep the section and original source number associated
with each question, including when a section continues onto another page. A
preceding-section hint describes earlier context only; a new heading in the
current page takes precedence. If a batch has no eligible complete questions,
return exactly <quiz></quiz>.

Produce one question for each eligible source question. Preserve an existing MCQ's stem,
choices, labels, correct answers, multi-correct semantics, and required visuals.
For Matching, use the native Moodle matching template when the source has
unambiguous item-to-answer pairs. Preserve source distractors as unmatched
choices. If the matching cannot be represented faithfully with native matching,
use the standard MCQ table and matching-combination choices instead.
For Assertion-Reason, use the MCQ structure but preserve the actual source
choices and their ordering; do not replace them with a fixed set that excludes
an available outcome such as both statements being false.

Do not invent missing source facts or pretend an uncertain answer has been
verified. If an eligible question cannot be represented faithfully or answered
with the available source, report an error rather than emitting an unsupported
question or silently dropping it.

For Moodle XML, use type="multichoice" or type="matching" as specified above. For
native matching, include at least two items and three choices; every item and
unmatched choice must have a nonempty answer. For multichoice questions, for
single-correct questions, set <single>true</single> and mark exactly one
choice 100%. For multi-correct questions, set <single>false</single>, retain all
correct choices, and allocate positive credit totaling 100%. In either case,
include at least two answer choices and a complete general feedback explanation.
