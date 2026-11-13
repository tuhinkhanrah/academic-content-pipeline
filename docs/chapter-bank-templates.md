# Subject chapter-bank templates

The subject chapter-bank specs are in
[`src/academic_content_pipeline/resources/spec_templates`](../src/academic_content_pipeline/resources/spec_templates).
The paper specs are the curated structural source, and the exam manifest lists
which papers and instruction files belong to each exam. The commands derive
batch specs and per-subject adaptive question-type weights from the paper
sections; they do not use AI to guess rules from instruction prose.

Regenerate all managed paper copies, batch specs, and chapter-bank specs after
editing a paper spec or the manifest:

```powershell
python -m academic_content_pipeline.unified_pipeline generate-templates --force
```

Validate the manifest, paper structure and scoring totals, syllabus and
instruction references, generated batch specs, and chapter-bank weights:

```powershell
python -m academic_content_pipeline.unified_pipeline validate-templates
```

Generation refuses to overwrite existing files unless `--force` is supplied.
Both commands accept `--manifest`; generation accepts `--output-dir`, and
validation accepts `--template-dir`.

| Exam | Subject | Spec |
|---|---|---|
| NEET UG | Physics | [physics_chapter_bank.template.yaml](../src/academic_content_pipeline/resources/spec_templates/neet_ug/physics_chapter_bank.template.yaml) |
| NEET UG | Chemistry | [chemistry_chapter_bank.template.yaml](../src/academic_content_pipeline/resources/spec_templates/neet_ug/chemistry_chapter_bank.template.yaml) |
| NEET UG | Biology | [biology_chapter_bank.template.yaml](../src/academic_content_pipeline/resources/spec_templates/neet_ug/biology_chapter_bank.template.yaml) |
| JEE Main | Mathematics | [mathematics_chapter_bank.template.yaml](../src/academic_content_pipeline/resources/spec_templates/jee_main/mathematics_chapter_bank.template.yaml) |
| JEE Main | Physics | [physics_chapter_bank.template.yaml](../src/academic_content_pipeline/resources/spec_templates/jee_main/physics_chapter_bank.template.yaml) |
| JEE Main | Chemistry | [chemistry_chapter_bank.template.yaml](../src/academic_content_pipeline/resources/spec_templates/jee_main/chemistry_chapter_bank.template.yaml) |
| JEE Advanced | Mathematics | [mathematics_chapter_bank.template.yaml](../src/academic_content_pipeline/resources/spec_templates/jee_advanced/mathematics_chapter_bank.template.yaml) |
| JEE Advanced | Physics | [physics_chapter_bank.template.yaml](../src/academic_content_pipeline/resources/spec_templates/jee_advanced/physics_chapter_bank.template.yaml) |
| JEE Advanced | Chemistry | [chemistry_chapter_bank.template.yaml](../src/academic_content_pipeline/resources/spec_templates/jee_advanced/chemistry_chapter_bank.template.yaml) |
| WBJEE | Mathematics | [mathematics_chapter_bank.template.yaml](../src/academic_content_pipeline/resources/spec_templates/wbjee/mathematics_chapter_bank.template.yaml) |
| WBJEE | Physics | [physics_chapter_bank.template.yaml](../src/academic_content_pipeline/resources/spec_templates/wbjee/physics_chapter_bank.template.yaml) |
| WBJEE | Chemistry | [chemistry_chapter_bank.template.yaml](../src/academic_content_pipeline/resources/spec_templates/wbjee/chemistry_chapter_bank.template.yaml) |

The NEET UG templates reflect the subject-specific paper allocations. NEET UG
has no true numerical type, so numerical-style questions are distributed across
standard MCQ, diagram/image, and match-list allocations. JEE Main
weights reflect its 20 MCQs to 5 numerical questions per subject.
JEE Advanced weights aggregate the corresponding question formats in Papers 1
and 2; linked numerical-stem questions are represented by the registered
`numerical` type. WBJEE weights reflect the section counts and distinguish
single-correct MCQs from multiple-correct questions.

Physics and Chemistry paper templates explicitly allocate diagram/image-based,
matching-list, and statement-based questions within their existing MCQ section
counts. Physics has a higher diagram allocation than Chemistry. These
allocations flow into the generated chapter-bank adaptive weights; those
weights are preferences, so the generator may still adjust the mix to suit a
chapter.

Physics paper targets are 16 of 45 questions for NEET UG, 7 of 25 for JEE
Main, 3 of 16 and 3 of 18 for JEE Advanced Papers 1 and 2, and 11 of 40 for
WBJEE. JEE Advanced's diagram targets are limited to its single-correct
section; numerical and multiple-correct allocations remain unchanged.

Use the corresponding subject spec when generating questions from a chapter
source file or directory. For example:

```powershell
python -m academic_content_pipeline.unified_pipeline agent generate-questions `
  --input-dir path/to/jee_main/mathematics/chapters `
  --spec src/academic_content_pipeline/resources/spec_templates/jee_main/mathematics_chapter_bank.template.yaml `
  --output-dir output/jee_main/mathematics
```

`total_questions: 120` is the target per input chapter. Change it in the spec
when a different target is desired. The adaptive type weights are preferences;
the generator may adjust allocations to fit the chapter content.
