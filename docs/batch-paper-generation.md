# Batch paper generation

`generate-paper` can run one paper blueprint repeatedly or combine multiple
blueprints into each test set. Each paper is generated, validated, checked for
exact duplicate questions, and committed separately. A JSON checkpoint and
saved AI responses are kept in the output directory so interrupted runs can be
continued.

## Language-specific outputs

Paper batches use the configured language combination in their filenames and
checkpoint directories. For example, with `--output-dir output/wbjee`:

```text
wbjee_english_test_001_mathematics.xml
wbjee_english_batch_generation.json
.wbjee_english_batch_generation/
wbjee_english_bengali_test_001_mathematics.xml
wbjee_english_bengali_batch_generation.json
.wbjee_english_bengali_batch_generation/
```

`--languages english,hindi` similarly uses `english_hindi`. Different language
runs can share an output directory, but each has independent generation and
reference-profile checkpoints. They generate separate papers, not translations
of one English paper. `--force` affects the selected language's batch.

On `--resume`, if no language-specific checkpoint exists and an older
language-independent checkpoint has compatible settings, the runner
continues that legacy batch with its original filenames and state. Checkpoints
without a recorded analysis model have the limitations described below. Other
language combinations start their own new checkpoints. To obtain
language-specific names for an existing legacy batch, run a new batch rather
than resuming it.

## Multiple paper blueprints

The repository includes complete paper and batch templates for all four exams:

- NEET UG: one 180-question paper.
- JEE Main: one 75-question paper with Mathematics, Physics, and Chemistry.
- JEE Advanced: Paper 1 and Paper 2, each with separate section blueprints.
- WBJEE: a Mathematics paper and a Physics/Chemistry paper.

Each paper blueprint follows the corresponding rules in `prompts/instructions`
and links the applicable subject syllabuses. Each batch template requests 100
test sets by default; change `test_count` if you want a smaller run.
For multi-subject blueprints, each subject is generated in a separate AI
request using that subject's `total_questions` and applicable section/type
allocations. The resulting subject Moodle XML files are combined and validated
as one paper before it is committed.

## Chapter-grouped generation

The exam paper templates enable chapter-grouped generation with:

```yaml
chapter_generation:
  enabled: true
  max_questions_per_request: 15
```

The runner splits each subject's Markdown syllabus at its leaf headings, then
asks the configured model for a chapter allocation whose integer counts sum to
the subject total. This allocation is saved in the batch checkpoint and reused
for every test set in that run. To supply a reviewed allocation instead, add a
`chapter_counts` mapping keyed by chapter heading or generated chapter ID.
If the model returns an invalid allocation, the runner requests a correction
with validation feedback, up to three total planning attempts. If all attempts
fail, the error is reported and `--resume` can retry without discarding earlier
valid plans. These additional requests may incur token usage and cost.

Chapters are grouped in syllabus order up to the configured question target.
Each group request receives its chapter text, exact chapter quotas, and a
deterministically allocated share of the subject's section and question-type
counts. The model must prefix every Moodle question name with its chapter ID
(for example, `[ch001-kinematics]`); the runner checks the per-chapter counts
before accepting the group. Valid group responses are saved and reused during
resume/retry. Each checkpoint is tied to its source syllabus, paper spec,
allocation settings, and analysis backend configuration.

The AI-generated chapter distribution is a proposal based on syllabus scope,
not official exam weighting. Review and pin it with `chapter_counts` when a
specific exam blueprint requires fixed chapter weights. Checkpoints created by
older runner versions use a different schema and cannot be resumed by the
chapter-grouped runner; start a fresh run in a new output directory.
Each AI interaction log identifies its purpose, such as planning chapter
allocations for a specific subject or generating a particular paper group.
Before each Gemini request attempt, the pipeline calls the token-counting API
and logs an input estimate. It counts the system text as a separate message
because the Gemini Developer API does not accept a system instruction in its
count request; this estimate can therefore differ from actual usage. Chat
counts include retained history and current images. Agent counts cover only
the visible current input and any system text sent with that interaction;
agent-managed history, tools and internal instructions are excluded.
Remote agent mode reports the pre-count as unavailable because its model is
not explicitly specified. After each response, logs show provider-reported
input, output, total, thinking, cached, and tool-input tokens when available;
missing values are logged as unavailable rather than estimated. Failed
requests may not report usage. Token counting adds a network request and may
use API quota; count failures are logged and do not stop generation. OCR
usage is separate and is not included in these counts.

## Optional reference paper

Pass `--reference-paper` to either `generate-paper` or the path-mirrored
paper-generation script to calibrate the style against a reference PDF or OCR
Markdown (`.md` or `.markdown`):

```powershell
python scripts/run_path_mirrored_paper_generation.py `
  --mode agent --model-name gemini-3.8-flash `
  --spec-root src/academic_content_pipeline/resources/spec_templates `
  --spec neet_ug/neet_ug.template.yaml `
  --reference-paper path/to/reference.md `
  --num-tests 1 --output-root output
```

Without a CLI reference or a batch-template default, generation uses no
reference. `--sample-pdf` is a deprecated alias. PDF references use the configured OCR service; existing OCR
Markdown avoids another PDF OCR request. Markdown images must be local and
either beside the Markdown file or in its sibling `ocr` directory.

For a batch with multiple papers, repeat the option with the paper IDs from the
batch spec to assign a different reference to each paper:

```powershell
python scripts/run_path_mirrored_paper_generation.py `
  --mode agent --model-name gemini-3.8-flash `
  --spec-root src/academic_content_pipeline/resources/spec_templates `
  --spec jee_advanced/batch.template.yaml `
  --reference-paper "paper1=path/to/previous-paper1.pdf" `
  --reference-paper "paper2=path/to/previous-paper2.md" `
  --num-tests 1 --output-root output --resume
```

Only the mapped paper uses each reference. Papers without a mapping or template
default generate without reference guidance. IDs must match the batch paper IDs; duplicates and
unknown IDs are rejected. An unkeyed `--reference-paper path` remains a
single shared reference for all papers, and cannot be combined with keyed
references. For discovery across multiple exams, use `--spec` to target the
batch when supplying paper-specific mappings.

A batch template can instead provide optional per-paper defaults:

```yaml
papers:
  - id: mathematics
    spec: mathematics.template.yaml
    reference_paper: references/mathematics.md
  - id: physics_chemistry
    spec: physics_chemistry.template.yaml
    reference_paper: references/physics_chemistry.pdf
```

Template paths are relative to the batch YAML file, not the current working
directory. A keyed CLI reference overrides that paper's default only; an
unkeyed CLI reference overrides all defaults with one shared reference.
`--no-reference-paper` ignores all template defaults and cannot be combined
with explicit reference options. The wrapper forwards these options and the
selected analysis mode even when references come from the template.
Keep local vault paths in a separate local batch configuration rather than
committing machine-specific paths to shared templates. Individual paper
blueprints do not provide reference defaults.

Reference analysis and AI chapter allocation use a stateless Gemini request,
separate from the question-generation backend. Set
`--reference-analysis-model-name` to choose their model; otherwise they use
`--model-name`. In agent mode, the agent is used for question generation, not
for reference analysis or chapter allocation. PDF OCR remains a separate
Mistral operation.

```powershell
python scripts/run_path_mirrored_paper_generation.py `
  --mode agent --model-name gemini-3.8-flash `
  --reference-analysis-model-name gemini-2.5-flash `
  --spec-root src/academic_content_pipeline/resources/spec_templates `
  --num-tests 1 --output-root output --resume
```

By default, the pipeline analyzes the whole reference in fixed batches of 20 PDF
pages or 20 recognized Markdown questions. Use `--reference-analysis-mode full`
to send all text and diagrams in one request.
Full mode can exceed the model's input limits; errors are not silently retried
in a different mode. When Markdown has no recognized question boundaries it
is analyzed as one unit. Images remain with their corresponding page or
Markdown question where possible. Partial analysis results are saved and
reused after interruption; batch profiles are combined into a style profile.

The resulting subject/chapter profile and up to two selected
examples per generation group guide the model; diagrams are used for analysis,
not attached to the generated questions. Chapter-specific guidance is applied
only when the identified chapter matches a syllabus heading, otherwise
subject-level guidance is used. A style profile is not
a guarantee of question quality or an exact style mix. The spec, syllabus and
marking rules remain authoritative, and the model is instructed to produce
original questions rather than copy reference questions.

The profile is saved in batch state and reused on resume. Checkpoints record
the analysis model and backend, analysis mode, fixed batch size, reference content
hash and Markdown image hashes. Changing the analysis model does not prevent
resume or invalidate an existing profile. Partial analyses resumed with a
different model may combine results from multiple models; the profile records
the model used for each batch. Changing the analysis backend, reference or an
associated image requires a fresh batch. Keep the reference available when
resuming. Reference content and selected images are sent to the configured AI
service (and PDFs to the configured OCR service); ensure you have permission to
use them. Generated prompts and the saved profile may contain short reference
excerpts, so protect those files accordingly.

Older checkpoints with references or AI chapter planning do not record the
analysis backend and cannot safely resume with this version. Use a new output
directory or `--force` to start a fresh batch; `--force` replaces that
language's existing batch outputs. Older checkpoints without reference analysis
or AI chapter planning remain resumable when their other settings match.
Checkpoints created with custom batch sizes require a fresh run.

PDF OCR artifacts and their local OCR cache are isolated under the exam output
directory at `reference/<paper-id>/<reference-content-hash>/` (or `shared` for
a shared reference). Use a separate output directory for each exam; the
path-mirrored script does this automatically. A marker prevents different exams
from sharing the same reference directory. New paper-specific profiles are
stored in the language-specific batch state; the checkpoint records their
hashes and the paper-to-reference mapping. When an older exam-nested reference directory is
needed, its artifacts are copied into the new layout for resume; the originals
are left untouched. Existing shared-reference batch profiles remain in batch
state. Previously created global `output/reference` artifacts are not deleted.

For example, the JEE Advanced batch spec is:

```yaml
exam_name: JEE Advanced
test_count: 100
uniqueness_scope: all_papers
papers:
  - id: paper1
    spec: paper1.template.yaml
  - id: paper2
    spec: paper2.template.yaml
```

The paths in `spec` are resolved relative to the batch YAML file. Each
referenced paper spec defines its own subjects, question counts, sections, and
question types. The supplied templates are under
[`spec_templates/`](../src/academic_content_pipeline/resources/spec_templates).

Run 100 JEE Advanced test sets (200 paper outputs):

```powershell
python -m academic_content_pipeline.unified_pipeline agent generate-paper `
  --spec src/academic_content_pipeline/resources/spec_templates/jee_advanced/batch.template.yaml `
  --output-dir output/jee_advanced
```

Run any of the other exams by substituting the batch spec and output directory:

```powershell
python -m academic_content_pipeline.unified_pipeline agent generate-paper `
  --spec src/academic_content_pipeline/resources/spec_templates/neet_ug/batch.template.yaml `
  --output-dir output/neet_ug

python -m academic_content_pipeline.unified_pipeline agent generate-paper `
  --spec src/academic_content_pipeline/resources/spec_templates/jee_main/batch.template.yaml `
  --output-dir output/jee_main

python -m academic_content_pipeline.unified_pipeline agent generate-paper `
  --spec src/academic_content_pipeline/resources/spec_templates/wbjee/batch.template.yaml `
  --output-dir output/wbjee
```

For path-mirrored generation across multiple batch specs, use the helper script.
It discovers batch specs under `--spec-root` and mirrors each spec's parent
folder beneath `--output-root`; `--num-tests` defaults to 1:

```powershell
python scripts/run_path_mirrored_paper_generation.py `
  --spec-root src/academic_content_pipeline/resources/spec_templates `
  --output-root output/papers `
  --mode agent `
  --model-name gemini-3.8-flash `
  --num-tests 1
```

To run one paper spec (rather than discover batch specs), pass `--spec`:

```powershell
python scripts/run_path_mirrored_paper_generation.py `
  --spec-root src/academic_content_pipeline/resources/spec_templates `
  --spec wbjee/mathematics.template.yaml `
  --output-root output/wbjee-mathematics `
  --mode agent `
  --model-name gemini-3.8-flash `
  --num-tests 1 `
  --resume
```

The script defaults to one concurrent spec run. Use `--parallel-workers` to
raise concurrency, `--dry-run` to preview commands, and `--resume` to reuse
checkpoints; when no checkpoint exists, the runner starts a fresh generation.
It infers `--standards` from the spec's path (for example, `wbjee/` becomes
`wbjee`), matching the path-based behavior of the question-generation runner.
Use `--standards` to explicitly override that inferred value.

For a single paper blueprint, `--num-tests 100` repeats that paper spec:

```powershell
python -m academic_content_pipeline.unified_pipeline agent generate-paper `
  --spec path/to/paper.yaml --num-tests 100 --output-dir output/mock_tests
```

Resume an interrupted run with the same spec, output directory, and compatible
generation settings:

```powershell
python -m academic_content_pipeline.unified_pipeline agent generate-paper `
  --spec path/to/batch.yaml --output-dir output/mock_tests --resume
```

To append more test sets while keeping completed outputs, increase
`--num-tests` and resume. For example, after generating one set, use
`--num-tests 2 --resume` to retain test 001 and generate test 002. The requested
count is the desired total, not the number of additional sets. The spec,
syllabus, analysis backend, and other generation settings must remain unchanged;
lowering the count below the checkpoint's current total is rejected. The
question-generation and analysis models and generation agent name can change;
saved responses, chapter plans, reference profiles, and completed outputs
remain in use. The generation and analysis backends must still match.
If resume settings do not match, the runner logs each differing configuration
field with its saved and requested values to help diagnose the mismatch.

Checkpoint configuration stores repository files using repo-root-relative
POSIX paths, not checkout-specific absolute paths. Source fingerprints retain
content hashes; external files are identified by their content hash and
filename. Existing checkpoints with absolute paths are normalized when they
are resumed, when their referenced repository files can be found in the new
checkout. Keep specs and syllabuses inside the repository with relative
references for portable checkpoints; an external input still needs to be
available at the path configured on the machine running generation.

`--max-paper-attempts` controls how many new generation attempts are made for
each unfinished paper in one invocation. Existing saved AI responses are
reused before another AI request. Use `--force` to start a fresh run in the
same output directory; `--force` and `--resume` cannot be combined.

`uniqueness_scope` accepts:

- `all_papers` (default): exact duplicates are rejected across the whole run.
- `paper_type`: exact duplicates are rejected among outputs with the same paper
  ID, such as Paper 1 versus other Paper 1 outputs.
- `test_set`: exact duplicates are rejected only between papers in the same
  test set.
- `none`: skip cross-paper duplicate checks.

Uniqueness checks use normalized question text and answers, so they catch exact
repeats despite XML escaping differences. They cannot guarantee detection of
paraphrased or semantically equivalent questions; generation prompts also
request novel questions.

The batch runner currently requires Moodle XML output. It validates each
subject's configured question count, then validates the combined paper's XML
and total question count. It does not independently score answers or guarantee
semantic uniqueness.

For subject-specific chapter-bank specs, see
[`chapter-bank-templates.md`](./chapter-bank-templates.md).
