# Academic Content Pipeline Command Reference

Run every command below from the `academic-content-pipeline` repository directory.
Examples use Bash syntax.

```bash
cd /path/to/academic-content-pipeline
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
```

Alternatively, install the dependencies from the requirements file:

```bash
python -m pip install -r requirements.txt
```

Set the API credentials used by the selected pipeline operations:

```bash
export GEMINI_API_KEY="..."
export MISTRAL_API_KEY="..."
export GCS_BUCKET_NAME="..."  # Needed for remote mode unless --bucket-name is supplied.
export MISTRAL_OCR_MODEL="mistral-ocr-latest"  # Optional OCR model override.
```

The unified package command is:

```bash
python -m academic_content_pipeline.unified_pipeline <mode-or-task> [task] [options]
```

The package also installs the shorter entry point:

```bash
academic-content-pipeline <mode-or-task> [task] [options]
```

## Communication modes

The mode determines how the pipeline sends requests to an AI provider.

| Mode | Purpose |
|---|---|
| `context` | Rolling multi-turn chat context; `--memory-span` controls retained turns. |
| `agent` | Managed agent session; `--agent-name` selects the agent. |
| `remote` | Remote sandbox execution with GCS staging; requires `GCS_BUCKET_NAME` or `--bucket-name`. |
| `batch` | Gemini Batch API requests. |

Use mode-first syntax:

```bash
python -m academic_content_pipeline.unified_pipeline agent extract --input-file input/paper.pdf
```

Or task-first syntax:

```bash
python -m academic_content_pipeline.unified_pipeline extract --mode agent --input-file input/paper.pdf
```

Mode-first syntax supports `context`, `agent`, `remote`, and `batch`. Task-first syntax is available for `extract`, `generate-questions`, `generate-paper`, `generate-tutorial`, and `translate-artifacts`.

## Unified CLI commands

### `extract`

Extract questions from one PDF or a directory of PDFs with Mistral OCR.

```bash
python -m academic_content_pipeline.unified_pipeline agent extract \
  --source-root ../question-papers-vault \
  --input-dir ../question-papers-vault/previous-year-question-bank/neet-ug \
  --output-dir ../question-papers-vault-moodle/previous-year-question-bank/neet-ug
```

Specify exactly one input source: `--input-file` or `--input-dir`.

| Option | Purpose | Default |
|---|---|---|
| `--input-file PATH` | Extract one PDF. | None |
| `--input-dir PATH` | Extract every PDF discovered below a directory. | None |
| `--page-range START END` | Limit extraction to an inclusive 1-based page range. | All pages |
| `--replace-pages START END` | Re-extract and replace saved batches covering this range; requires a checkpoint. | Disabled |
| `--no-instruction-page` | Do not treat a front instruction page as an instruction page. | False |
| `--instruction-page INDEX` | Set the 1-based front instruction page index. | `1` |
| `--instruction-page-model-name NAME` | Gemini model used to classify the instruction page; defaults to `--model-name`. | `--model-name` |
| `--resume` | Resume from validated per-batch checkpoints. | False |
| `--verify-online` | Verify extracted answers online. | False |

### `generate-questions`

Generate practice questions from chapter PDF or Markdown sources.

```bash
python -m academic_content_pipeline.unified_pipeline agent generate-questions \
  --input-dir ../chapters/jee_main/mathematics \
  --source-root ../chapters/jee_main \
  --top-category "Chapterwise Question Bank" \
  --spec src/academic_content_pipeline/resources/spec_templates/jee_main/mathematics_chapter_bank.template.yaml \
  --output-dir output/jee_main/mathematics
```

Specify exactly one source: `--input-file` or `--input-dir`.

| Option | Purpose | Default |
|---|---|---|
| `--input-file PATH` | Generate from one PDF or Markdown source. | None |
| `--input-dir PATH` | Generate from PDF/Markdown sources below a directory. | None |
| `--spec PATH` | Optional YAML paper or chapter spec used for formal structure and question allocation. | None |
| `--page-range START END` | Limit chapter PDF processing to an inclusive 1-based page range. | All pages |
| `--num-questions COUNT` | Target number of questions synthesized per source. | `5` |
| `--num-papers COUNT` | Split generated XML questions across this many paper files. | `1` |
| `--separate-by-question-type` | Write exact or adaptively planned question types as separate Moodle XML files. Requires `--spec`, Moodle XML output, and `--num-papers 1`. | False |
| `--difficulty-mix MIX` | Comma-separated difficulty ratios. | `easy:0.2,medium:0.5,hard:0.3` |
| `--exam-duration-minutes MINUTES` | Exam duration for PDF output. | None |
| `--translate-to LANGUAGES` | After generation, create English-master bilingual artifacts for comma-separated non-English languages. | None |

### `generate-paper` / `generate-papers`

Generate one paper blueprint repeatedly or generate complete test sets from a batch spec.

```bash
python -m academic_content_pipeline.unified_pipeline agent generate-paper \
  --source-root src/academic_content_pipeline/resources/spec_templates \
  --top-category "Practice Papers" \
  --spec src/academic_content_pipeline/resources/spec_templates/neet_ug/batch.template.yaml \
  --output-dir output/neet-ug
```

| Option | Purpose | Default |
|---|---|---|
| `--spec PATH` | YAML/JSON paper or batch spec. | None |
| `--category-year YYYY` | Use an exam/year/language Moodle category; requires XML output. | None |
| `--num-tests COUNT` | Desired total number of test sets. | Spec default or None |
| `--resume` | Resume a compatible batch checkpoint; creates a new run when no checkpoint exists. | False |
| `--max-paper-attempts COUNT` | Maximum new generation attempts for each unfinished paper per invocation. | `3` |
| `--difficulty-mix MIX` | Comma-separated difficulty ratios. | `easy:0.2,medium:0.5,hard:0.3` |
| `--exam-duration-minutes MINUTES` | Exam duration for PDF output. | None |
| `--reference-paper [PAPER_ID=]PATH` | Add a PDF or OCR Markdown reference; repeat for paper-specific batch references. Mutually exclusive with `--sample-pdf` and `--no-reference-paper`. | None |
| `--sample-pdf PATH` | Deprecated alias for `--reference-paper`. | None |
| `--no-reference-paper` | Ignore reference defaults declared in batch templates. Mutually exclusive with reference options. | False |
| `--reference-analysis-mode batched\|full` | Analyze references in fixed batches or as one full request. | `batched` |
| `--reference-analysis-model-name NAME` | Stateless Gemini model for reference analysis and chapter planning; defaults to `--model-name`. | `--model-name` |

`--reference-paper` may be unkeyed for every paper or keyed as `paper1=path/to/file.pdf`. Keyed references cannot be combined with unkeyed references. `--num-tests` is the desired total test-set count; increasing it while resuming appends test sets. `--force` cannot be combined with `--resume`.

Checkpointed batch-paper generation requires `--output-format xml`. Supported `uniqueness_scope` values are `all_papers`, `paper_type`, `test_set`, and `none`.
When using the path-mirrored paper-generation runner, template IDs such as `neet_ug`, `jee_main`, and `jee_advanced` are written under the canonical output folders `neet-ug`, `jee-main`, and `jee-advanced`. Nested spec folders remain unchanged.

### `generate-tutorial`

Generate or re-render an exam-oriented tutorial.

```bash
python -m academic_content_pipeline.unified_pipeline agent generate-tutorial \
  --input-file ../chapters/kinematics.md \
  --output-dir output/tutorials \
  --generate-pdf \
  --pdf-engine html
```

| Option | Purpose | Default |
|---|---|---|
| `--input-file PATH` | Use one PDF or Markdown source. | None |
| `--input-dir PATH` | Process PDF/Markdown or HTML inputs below a directory. | None |
| `--page-range START END` | Limit chapter PDF processing to an inclusive 1-based page range. | All pages |
| `--title TEXT` | Set the tutorial title. | None |
| `--generate-pdf` | Render the generated tutorial to PDF. | False |
| `--render-pdf` | Render existing HTML input to PDF beside the HTML; do not run AI generation. | False |

`--render-pdf` requires `--pdf-engine html`, exactly one HTML input source, and cannot be combined with `--generate-pdf`.

### `translate-artifacts`

Translate reviewed English Moodle XML, HTML, or TeX artifacts without regenerating source content.

```bash
python -m academic_content_pipeline.unified_pipeline agent translate-artifacts \
  --input-dir ./reviewed-english \
  --output-dir ./translated \
  --translate-to bengali,hindi
```

Specify exactly one input: `--input-file` or `--input-dir`. The output directory must not be the input directory or one of its descendants.

| Option | Purpose | Default |
|---|---|---|
| `--input-file PATH` | Translate one `.xml`, `.html`, or `.tex` artifact. | None |
| `--input-dir PATH` | Discover supported artifacts below a directory. | None |
| `--translate-to LANGUAGES` | Required comma-separated non-English target languages. | None |
| `--xml-chunk-size COUNT` | Questions sent to one AI request while translating Moodle XML. | `5` |

### `generate-templates`

Generate managed paper, batch, and subject chapter-bank templates from the exam manifest.

```bash
python -m academic_content_pipeline.unified_pipeline generate-templates --force
```

| Option | Purpose | Default |
|---|---|---|
| `--manifest PATH` | Exam manifest that declares papers and instruction files. | Built-in exam manifest |
| `--output-dir PATH` | Directory receiving generated templates. | Built-in spec-template directory |
| `--force` | Replace existing generated files. | False |

Generation refuses to overwrite existing files without `--force`.

### `validate-templates`

Validate the manifest, paper structure, scoring, references, batch specs, and chapter-bank weights.

```bash
python -m academic_content_pipeline.unified_pipeline validate-templates
```

| Option | Purpose | Default |
|---|---|---|
| `--manifest PATH` | Exam manifest to validate. | Built-in exam manifest |
| `--template-dir PATH` | Directory containing paper, batch, and chapter-bank templates. | Built-in spec-template directory |

## Common unified CLI options

These options are available to task subcommands.

| Option | Purpose | Default |
|---|---|---|
| `--languages LANGUAGES` | Comma-separated target languages. | `english` |
| `--standards STANDARD` | Exam standard, for example `neet_ug` or `jee_main`. | `general` |
| `--tags TAGS` | Comma-separated global tags, for example `year:2026,source:allen`. | Empty |
| `--output-dir PATH` | Directory for generated outputs. | `./output` |
| `--output-format xml\|pdf` | Select Moodle XML or PDF output. | `xml` |
| `--pdf-engine html\|tex` | Select HTML/Chrome or XeLaTeX PDF rendering. | `html` |
| `--log-file PATH` | Write pipeline logs to this file. | None |
| `--verbose` | Enable verbose debug logging. | False |
| `--prompt PATH` | Override the main prompt or exam profile Markdown file. | Task-specific built-in prompt |
| `--instruction-file PATH` | Use an exam instruction Markdown file. | None |
| `--xml-rules PATH` | Moodle XML generation rules. | `prompts/core/moodle_xml_rules.md` |
| `--xml-extraction-rules PATH` | Moodle XML extraction rules. | `prompts/core/moodle_xml_extraction_rules.md` |
| `--tags-rules PATH` | Naming and tag rules. | `prompts/core/naming_and_tags_rules.md` |
| `--templates PATH` | Moodle XML templates. | `prompts/core/moodle_xml_templates.md` |
| `--pdf-rules PATH` | Explicit PDF rules override. | None |
| `--pdf-rules-html PATH` | HTML PDF rules. | `prompts/core/pdf_html_rules.md` |
| `--pdf-rules-tex PATH` | TeX PDF rules. | `prompts/core/pdf_tex_rules.md` |
| `--reasoning-rules PATH` | Step-by-step reasoning and solution rules. | `prompts/core/reasoning_rules.md` |
| `--mcq-types PATH` | MCQ type registry YAML. | Built-in catalog |
| `--model-name NAME` | Gemini model name. | `gemini-flash-latest` |
| `--ocr-model NAME`, `--model NAME` | Mistral OCR model name. | `MISTRAL_OCR_MODEL` or `mistral-ocr-latest` |
| `--agent-name NAME` | Managed agent identifier. | `antigravity-preview-09-2026` |
| `--memory-span COUNT` | Retained turns in `context` mode. | `3` |
| `--rate-limit-delay SECONDS` | Delay between page OCR requests. | `6.0` |
| `--retry-limit COUNT` | Maximum retry attempts per page/request after API or quota errors. | `5` |
| `--batch-size COUNT` | Maximum pages per extraction request; `0` submits the whole document in one batch. | `5` |
| `--force` | Force regeneration of existing or cached outputs. | False |
| `--disable-ocr-cache` | Disable local OCR caching and force a fresh OCR run. | False |
| `--bucket-name NAME` | GCS bucket for remote staging. | `GCS_BUCKET_NAME` or None |
| `--watermark-text TEXT` | Add text over generated PDF pages. | Empty |
| `--header-text TEXT` | Add repeating text to the header of HTML-rendered PDFs. | Empty |
| `--footer-text TEXT` | Add repeating text to the footer of HTML-rendered PDFs. | Empty |

Moodle XML tasks also use:

| Option | Purpose | Default |
|---|---|---|
| `--top-category TEXT` | Optional additional top-level Moodle question-bank category. | None |
| `--source-root PATH` | Source root used to derive source-relative Moodle category paths. Required for Moodle XML extraction/generation/paper tasks. | None |

For Moodle XML, categories follow:
`$module$/top/[<top-category>/]<source-relative-path-without-extension>/<lowercase-language-combination>`.
Keep `--source-root` fixed at the vault root when selecting a nested input or `--subtree`; otherwise the relative category path changes. Omitting `--top-category` does not remove existing XML categories or migrate checkpoints. Resume requires the saved category to match: use the original option for an unmigrated checkpoint or migrate it first.

## Path-mirrored scripts

Mirrored scripts preserve source paths below `--source-root` or `--spec-root` and apply inferred metadata.

### Extraction

```bash
python scripts/run_path_mirrored_extraction.py \
  --source-root ../question-papers-vault \
  --subtree ncert-exemplar-question-bank \
  --output-root ../question-papers-vault-moodle \
  --languages english,bengali,hindi \
  --resume
```

This uses the reorganized vault path as the output layout. Existing XMLs without
checkpoints cannot be resumed; checkpoints must match the requested category and
other resume settings.

| Option | Purpose | Default |
|---|---|---|
| `--source-root PATH` | Required root containing source PDFs. | None |
| `--subtree PATH` | Existing directory inside `--source-root`; limits discovery and repair but not category/output-root mirroring. | Whole source root |
| `--top-category TEXT` | Top-level Moodle category. | None |
| `--output-root PATH` | Required output root. | None |
| `--mode context\|agent\|remote\|batch\|sequential` | AI or OCR communication mode. | `context` |
| `--languages LANGUAGES` | Comma-separated target languages. | `english` |
| `--model-name NAME` | Gemini model override. | Unified pipeline default |
| `--instruction-page-model-name NAME` | Gemini model for instruction-page classification. | `--model-name` |
| `--model NAME`, `--ocr-model NAME` | Mistral OCR model. | `MISTRAL_OCR_MODEL` or `mistral-ocr-latest` |
| `--batch-size COUNT` | Maximum OCR pages per extraction request; `0` uses one batch. | `5` |
| `--parallel-workers COUNT` | Maximum PDFs processed concurrently. | `1` |
| `--bucket-name NAME` | GCS bucket for remote mode. | `GCS_BUCKET_NAME` |
| `--instruction-file PATH` | Optional extraction instruction file. | None |
| `--dry-run` | Print commands without executing extraction. | False |
| `--force` | Re-run extraction when target XML exists. | False |
| `--resume` | Resume per-PDF batch checkpoints. | False |
| `--repair` | Rebuild completed XML and checkpoints offline from saved responses. | False |
| `--repair-backup-root PATH` | Required new separate directory for pre-repair backups. | None |
| `--replace-pages START END` | Re-extract and replace saved batches for an inclusive page range. | None |
| `--disable-ocr-cache` | Disable local OCR cache. | False |
| `--verbose` | Print per-file metadata. | False |

`--repair` cannot be combined with `--resume`, `--force`, or `--replace-pages`. The extraction script does not rebuild combined Moodle XML; this is done after a successful run by the shared combiner.

### OCR-only extraction

```bash
python scripts/run_ocr_only_path_mirrored_extraction.py \
  --source-root ../question-papers-vault \
  --output-root ../ocr-output
```

| Option | Purpose | Default |
|---|---|---|
| `--source-root PATH` | Required root containing source PDFs. | None |
| `--output-root PATH` | Required directory for mirrored OCR output. | None |
| `--mode sequential\|batch` | Use direct sequential OCR or Mistral OCR batch jobs. | `sequential` |
| `--ocr-model NAME`, `--model NAME` | Mistral OCR model. | `MISTRAL_OCR_MODEL` or `mistral-ocr-latest` |
| `--force` | Overwrite an existing OCR XML output. | False |
| `--disable-ocr-cache` | Disable the OCR cache. | False |
| `--dry-run` | Print planned OCR work. | False |
| `--verbose` | Print processing progress. | False |

### Chapter question generation

```bash
python scripts/run_path_mirrored_question_generation.py \
  --source-root ../chapters \
  --top-category "Chapterwise Question Bank" \
  --output-root ../chapter-moodle \
  --mode agent \
  --num-questions 20
```

For the competitive-exam chapter bank, keep `--source-root ../competitive-exams-chapters`
fixed, even with `--subtree`, and use `--top-category chapterwise-question-bank`.
There is no `--resume` flag: matching outputs are reused by default. Adaptive
checkpoints record each completed XML's category and SHA-256. A category or
checksum mismatch must be resolved before reuse; `--force` regenerates instead.
Reuse also requires the original spec and language settings. The migration
does not replace the saved spec fingerprint with that of a newer template.

To migrate legacy adaptive outputs without generating questions, stop all
writers and first run a read-only check:

```bash
python scripts/migrate_chapter_question_categories.py \
  --source-root ../competitive-exams-chapters \
  --output-root ../competitive-exams-chapters-moodle
```

Add `--backup-root ../chapterwise-category-migration-backup` to apply. Use a
new directory outside both repositories. The migrator stages individual and
combined XMLs, backs up originals, and journals the changes before publishing.
If publishing is interrupted, rerun with the same backup directory. OCR XML
and other non-Moodle assets are not changed.

| Option | Purpose | Default |
|---|---|---|
| `--source-root PATH` | Required root containing PDF/Markdown sources. | None |
| `--subtree PATH` | Existing directory inside `--source-root`; limits discovery. | Whole source root |
| `--top-category TEXT` | Top-level Moodle category. | None |
| `--output-root PATH` | Required root for mirrored question outputs. | None |
| `--mode context\|agent\|remote\|batch\|sequential` | AI communication mode. | `context` |
| `--languages LANGUAGES` | Comma-separated target languages. | `english` |
| `--standards STANDARD` | Override the exam standard inferred from the source path. | Inferred |
| `--model-name NAME` | Gemini model override. | Unified pipeline default |
| `--ocr-model NAME` | Mistral OCR model for PDF inputs. | `MISTRAL_OCR_MODEL` or `mistral-ocr-latest` |
| `--output-format xml\|pdf` | Select Moodle XML or PDF output. | `xml` |
| `--pdf-engine html\|tex` | Select PDF rendering engine. | `html` |
| `--num-questions COUNT` | Questions generated per source file. | `5` |
| `--num-papers COUNT` | Split XML across this many paper files. | `1` |
| `--difficulty-mix MIX` | Difficulty ratios. | `easy:0.2,medium:0.5,hard:0.3` |
| `--page-range START END` | Optional 1-based PDF page range. | All pages |
| `--exam-duration-minutes MINUTES` | Exam duration for PDF output. | None |
| `--spec PATH` | Explicit YAML/JSON spec; otherwise select a matching subject template. | Inferred |
| `--instruction-file PATH` | Optional instruction profile. | None |
| `--separate-by-question-type` | Write each exact/adaptive question type to separate XML. | False |
| `--translate-to LANGUAGES` | Create non-English English-master bilingual outputs. | None |
| `--parallel-workers COUNT` | Maximum concurrent source files. | `1` |
| `--bucket-name NAME` | GCS bucket for remote mode. | `GCS_BUCKET_NAME` |
| `--watermark-text TEXT` | PDF watermark. | Empty |
| `--header-text TEXT` | Repeating HTML PDF header. | Empty |
| `--footer-text TEXT` | Repeating HTML PDF footer. | Empty |
| `--dry-run` | Print commands without executing. | False |
| `--force` | Generate even when matching output exists. | False |
| `--disable-ocr-cache` | Force fresh Mistral OCR. | False |
| `--verbose` | Print inferred per-file metadata. | False |

### Paper generation

Run these commands from the `academic-content-pipeline` repository. The runner
discovers batch specs recursively under `--spec-root`, starts one Moodle XML
generation job per spec, and mirrors each spec's parent folder below
`--output-root`. Without `--spec` or `--subtree`, the bundled templates select
NEET UG, JEE Main, JEE Advanced, and WBJEE.

Preview all discovered jobs without generating papers:

```bash
python scripts/run_path_mirrored_paper_generation.py \
  --spec-root src/academic_content_pipeline/resources/spec_templates \
  --output-root ../exam-practice-papers \
  --mode agent \
  --num-tests 1 \
  --dry-run
```

Remove `--dry-run` to run the jobs. To select one exam's batch spec, keep the
same spec root and use `--subtree`:

```bash
python scripts/run_path_mirrored_paper_generation.py \
  --spec-root src/academic_content_pipeline/resources/spec_templates \
  --subtree neet_ug \
  --output-root ../exam-practice-papers \
  --mode agent \
  --languages english \
  --year 2027 \
  --num-tests 4 \
  --resume
```

Alternatively, use `--spec neet_ug/batch.template.yaml` instead of `--subtree`
to select a single spec. The two options cannot be combined. If `--spec-root`
is omitted with `--spec`, the spec's parent becomes the root, so its output is
written directly under `--output-root` rather than an exam subfolder.

With the bundled spec root, output directories use canonical exam names and a
language subfolder, for example:

```text
exam-practice-papers/
  neet-ug/2027/english/neet_ug_english_test_001_full_paper.xml
  jee-main/2027/english/
  jee-advanced/2027/english/
  wbjee/2027/english/
```

Language combinations such as `english,bengali` use their own
`english_bengali` folder and checkpoint. With `--year 2027`, the year is
inserted after the exam folder and the category is
`$module$/top/neet-ug/2027/english/test_001/full_paper` for the first NEET
paper (or includes `--top-category` if supplied). Each batch output has its own
`test_NNN/<paper_id>` category; for example, JEE Advanced uses separate
`test_001/paper1` and `test_001/paper2` categories.
The runner preserves nested spec folders and does not add a year automatically
when `--year` is omitted. Multiple discovered
specs cannot target the same output directory. Successful jobs rebuild
hierarchical combined Moodle XML after all jobs finish.

`--resume` checks the saved configuration and output hashes and resumes a
compatible checkpoint; without a checkpoint it starts a new run if the
corresponding output files do not already exist. Keep the
spec root, output root, language, category and other checkpoint settings
consistent with the existing run. `--num-tests` is the desired total: increase
it to append test sets, not to repeat the existing count. `--force` starts a
fresh run and cannot be combined with `--resume`. Changing or omitting a
previously used `--top-category` does not migrate an existing checkpoint.
If a generated group contains malformed XML, the batch retains that response
for diagnosis and automatically retries the group within the
`--max-paper-attempts` limit. Other saved groups are reused; if the limit is
reached, resume the checkpoint to retry without starting over.
Without `--year`, Moodle categories follow the spec-relative path, not the
output folder slug: the NEET batch example uses
`$module$/top/neet_ug/batch.template/english/test_001/full_paper` when
`--top-category` is omitted.
Checkpoints created or migrated without a year category will not resume with
`--year` unless their categories and hashes are migrated; use a separate output
directory or migrate them safely before resuming.
Checkpoints using the older shared category scheme cannot resume with the new
per-paper scheme; use a new output directory or `--force` for a fresh run.

| Option | Purpose | Default |
|---|---|---|
| `--spec-root PATH`, `--source-root PATH` | Root containing paper/batch specs. Required for discovery. | None |
| `--spec PATH` | Generate one spec instead of discovering batch specs. Mutually exclusive with `--subtree`. | None |
| `--subtree PATH` | Discover batch specs only below this directory. Mutually exclusive with `--spec`. | Whole spec root |
| `--output-root PATH` | Required output root; spec folders are mirrored below it. | None |
| `--mode context\|agent\|remote\|batch` | AI communication mode. | `context` |
| `--model-name NAME` | Gemini model override. | Unified pipeline default |
| `--languages LANGUAGES` | Comma-separated target languages. | `english` |
| `--top-category TEXT` | Optional additional top-level Moodle category. | None |
| `--year YYYY` | Add a four-digit year to output paths and Moodle categories. | None |
| `--standards STANDARD` | Override the standard inferred from the spec path. | Inferred |
| `--tags TAGS` | Comma-separated global tags. | Empty |
| `--difficulty-mix MIX` | Difficulty ratios. | `easy:0.2,medium:0.5,hard:0.3` |
| `--num-tests COUNT` | Test sets per spec. | `1` |
| `--max-paper-attempts COUNT` | Maximum generation attempts per unfinished paper. | `3` |
| `--reference-paper [PAPER_ID=]PATH` | PDF/OCR Markdown reference; repeat for paper-specific mappings. | None |
| `--sample-pdf PATH` | Deprecated reference alias. Mutually exclusive with other reference options. | None |
| `--no-reference-paper` | Ignore template reference defaults. Mutually exclusive with reference options. | False |
| `--reference-analysis-mode batched\|full` | Reference analysis mode. | `batched` |
| `--reference-analysis-model-name NAME` | Model for reference analysis and chapter planning. | `--model-name` |
| `--exam-duration-minutes MINUTES` | PDF exam duration (CLI compatibility). | None |
| `--parallel-workers COUNT` | Maximum concurrent spec runs. | `1` |
| `--resume` | Resume existing checkpoints or start a new run. | False |
| `--force` | Start fresh in an existing output directory. Cannot be combined with `--resume`. | False |
| `--dry-run` | Print commands without running them. | False |

### Artifact translation

```bash
python scripts/run_path_mirrored_artifact_translation.py \
  --source-root ./reviewed-english \
  --output-root ./translated \
  --translate-to bengali,hindi
```

| Option | Purpose | Default |
|---|---|---|
| `--source-root PATH` | Required root containing reviewed `.xml`, `.html`, or `.tex` artifacts. | None |
| `--output-root PATH` | Mirrored translation root; required unless `--in-place` is used. | None |
| `--in-place` | Write translations beside English source files. | False |
| `--mode context\|agent\|remote\|batch\|sequential` | AI communication mode. | `context` |
| `--translate-to LANGUAGES` | Required comma-separated target languages. | None |
| `--model-name NAME` | Gemini model override. | Unified pipeline default |
| `--bucket-name NAME` | GCS bucket for remote mode. | `GCS_BUCKET_NAME` |
| `--xml-chunk-size COUNT` | Moodle XML questions sent per AI request. | `5` |
| `--parallel-workers COUNT` | Maximum concurrent source artifacts. | `1` |
| `--dry-run` | Print commands without executing. | False |
| `--force` | Regenerate existing translations. | False |

### Tutorial generation

```bash
python scripts/run_path_mirrored_tutorial.py \
  --source-root ../chapters \
  --output-root output/tutorials \
  --mode agent
```

| Option | Purpose | Default |
|---|---|---|
| `--source-root PATH` | Required root containing PDF/Markdown sources, or HTML files for `--render-pdf`. | None |
| `--output-root PATH` | Mirrored tutorial output root. | None |
| `--mode context\|agent\|remote\|batch\|sequential` | AI communication mode. | `context` |
| `--languages LANGUAGES` | Comma-separated target languages. | `english` |
| `--model-name NAME` | Gemini model override. | Unified pipeline default |
| `--ocr-model NAME` | Mistral OCR model for PDF inputs. | `MISTRAL_OCR_MODEL` or `mistral-ocr-latest` |
| `--pdf-engine html\|tex` | PDF rendering engine. | `html` |
| `--page-range START END` | Optional 1-based PDF page range. | All pages |
| `--parallel-workers COUNT` | Maximum concurrent source files. | `1` |
| `--bucket-name NAME` | GCS bucket for remote mode. | `GCS_BUCKET_NAME` |
| `--watermark-text TEXT` | PDF watermark. | Empty |
| `--header-text TEXT` | Repeating HTML PDF header. | Empty |
| `--footer-text TEXT` | Repeating HTML PDF footer. | Empty |
| `--dry-run` | Print commands without running. | False |
| `--force` | Regenerate matching tutorial output. | False |
| `--generate-pdf` | Render generated tutorials to PDF. | False |
| `--render-pdf` | Re-render existing HTML to PDFs beside it without AI generation. | False |
| `--disable-ocr-cache` | Force fresh Mistral OCR. | False |
| `--verbose` | Print inferred per-file metadata. | False |

## Offline category backfill

Write categorized copies or update existing Moodle XML in place.

```bash
python scripts/backfill_moodle_categories.py \
  --source-root ../question-papers-vault \
  --input-root ../question-papers-vault-moodle \
  --subtree neet_ug/ncert_exemplar/class11/biology \
  --output-root ../question-papers-vault-categorized \
  --top-category "Chapterwise Question Bank" \
  --languages english \
  --languages english,bengali \
  --languages english,hindi \
  --dry-run
```

Remove `--dry-run` after reviewing the preview.

| Option | Purpose | Default |
|---|---|---|
| `--source-root PATH` | Required root containing original PDFs or chapter files. | None |
| `--input-root PATH` | Required root containing existing Moodle XML. | None |
| `--output-root PATH` | New copy directory, or the input root for in-place updates. | None |
| `--backup-root PATH` | Required new separate backup directory for in-place updates. | None |
| `--top-category TEXT` | Required top-level Moodle category. | None |
| `--subtree PATH` | Process only this directory relative to `--input-root`. | Whole input root |
| `--languages LANGUAGES` | Repeat for every language combination to match. | Required |
| `--legacy-languages LANGUAGES` | Language combination for XML named exactly like its source. | None |
| `--spec PATH` | Original paper/batch spec for an existing paper-output tree. | None |
| `--dry-run` | Preview mappings without writing files. | False |

In-place updates require a new separate backup root and a stopped set of pipeline writers. The script validates checkpoint output hashes before writing and protects resume integrity.

## NCERT Exemplar validation

```bash
python scripts/check_ncert_exemplar_xml.py \
  --root ../ncert-exemplar-staging
```

| Option | Purpose | Default |
|---|---|---|
| `--root PATH` | Required root containing extracted language XML files. | None |

The checker reports missing language files, unsupported question types, malformed matching items, invalid grading, missing referenced images, and unequal question counts.

## Offline NCERT recovery

Rebuild XML from saved checkpoint responses without AI or OCR requests:

```bash
python scripts/rebuild_ncert_exemplar_xml.py \
  --source-root ../question-papers-vault-moodle/neet_ug/ncert_exemplar \
  --output-root ../ncert-exemplar-recovered
```

| Option | Purpose | Default |
|---|---|---|
| `--source-root PATH` | Existing bank or backup containing checkpoints and saved responses. | None |
| `--output-root PATH` | Required new, separate staging directory. | None |

The output directory must not already exist. Rebuilt files contain no checkpoints and cannot be resumed.

## Remote cleanup

Delete Google AI Studio batch jobs, managed environments, or both:

```bash
python scripts/cleanup.py batches
python scripts/cleanup.py environments
python scripts/cleanup.py all
```

| Argument/Option | Purpose | Default |
|---|---|---|
| `batches` | Delete Google AI Studio batch jobs. | Required positional value |
| `environments` | Delete managed agent environments. | Required positional value |
| `all` | Delete both resource types. | Required positional value |
| `--api-key KEY` | Google AI Studio API key. | `GEMINI_API_KEY` or secure prompt |

## Generated templates and Moodle integration

- Subject chapter-bank templates are under `src/academic_content_pipeline/resources/spec_templates/<exam>/`.
- The exam manifest is `src/academic_content_pipeline/resources/spec_templates/exam_template_manifest.yaml`.
- Prompt rules are under `prompts/core/`, `prompts/extractor/`, `prompts/generator/`, `prompts/instructions/`, and `prompts/translator/`.
- Successful Moodle XML extraction, question generation, and paper generation rebuilds hierarchical combined XML files named after their containing folders (for example, `2013_combined_moodle.xml`).
- Do not import individual source XML and an ancestor combined XML together unless duplicate questions are intentional.
- Enable **Get category from file** in Moodle for categorized XML imports.
- Run language combinations into separate staging trees or separate output roots because OCR caches, images, temporary files, and checkpoints are isolated per output root.

## Safe operation notes

- Keep specs and syllabuses inside the repository using relative paths for portable checkpoints.
- Resume settings must match category, source, spec, format, backend, and checkpoint-related configuration.
- `--force` replaces existing batch outputs for the selected language and should be used only when replacement is intentional.
- Stop all extraction, generation, repair, backfill, and other writers before category backfill or repair operations.
- Back up hidden checkpoint folders and OCR assets before recovery or repair.
- PDF OCR artifacts, saved prompts, profiles, and reference excerpts may contain sensitive or licensed source material; protect output directories accordingly.
