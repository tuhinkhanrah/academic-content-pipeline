# NCERT Exemplar extraction

PDFs under a directory named `ncert_exemplar` automatically use the NCERT
Exemplar extraction rules. No separate mode is required. Other extraction jobs
retain their existing behavior.

The rules extract original MCQs, including multi-correct MCQs, and matching
questions. VSA, SA, LA and other ineligible questions are excluded rather than
converted. Matching uses native Moodle matching where pairs are unambiguous,
or MCQ combinations when necessary; assertion-reason questions retain their
source choices. Extracted XML batches are rejected if they contain another
question type, malformed matching items, invalid answer fractions, inconsistent
single-correct grading, or missing referenced image files. The previous section
heading is provided as context when a section continues across pages.

Run the mirrored extractor from the project directory, using a **new staging
directory** instead of the current question bank. Run it once per language set:

```powershell
python scripts\run_path_mirrored_extraction.py --source-root ..\question-papers-vault\neet_ug\ncert_exemplar --top-category "NCERT Exemplar Question Bank" --output-root ..\ncert-exemplar-staging --languages english
python scripts\run_path_mirrored_extraction.py --source-root ..\question-papers-vault\neet_ug\ncert_exemplar --top-category "NCERT Exemplar Question Bank" --output-root ..\ncert-exemplar-staging --languages english,bengali
python scripts\run_path_mirrored_extraction.py --source-root ..\question-papers-vault\neet_ug\ncert_exemplar --top-category "NCERT Exemplar Question Bank" --output-root ..\ncert-exemplar-staging --languages english,hindi
```

Moodle XML categories use `$module$/top/<top-category>/<source-relative-path-without-extension>/<languages>`.
Language names are lowercase and joined with underscores, in the order supplied.
Enable **Get category from file** when importing. The source root itself is not
included in the category. Changing it or the top category prevents resuming a
checkpoint created with a different category.
Older checkpoints without a category cannot be resumed with these settings
unless migrated using the backfill below. Internal batch checkpoints are not
import files.
If extraction settings do not match on resume or page replacement, the error
log lists each compared field with its saved and requested values. For example,
`configuration.category_path` differs if the source root changes. Settings
ignored by the checkpoint compatibility check are not reported as differences.

To resume checkpoints whose categories were built relative to the vault root,
keep that root and select just NCERT Exemplar with `--subtree`:

```powershell
python scripts\run_path_mirrored_extraction.py `
  --source-root ..\question-papers-vault `
  --subtree neet_ug\ncert_exemplar `
  --output-root ..\question-papers-vault-moodle `
  --top-category "NCERT Exemplar Question Bank" `
  --mode agent --model-name gemini-3.6-flash --languages english `
  --resume --dry-run
```

Remove `--dry-run` after reviewing the selected files. The subtree must be an
existing directory inside the source root. It only limits discovery: categories
and output paths remain relative to the source root. Without `--subtree`, the
entire root is scanned. The option also limits `--repair` to the selected
directory, while preserving paths relative to the original output root.

After a successful Moodle XML extraction, the pipeline rebuilds
`combined_moodle.xml` in every output directory containing question XML and
its ancestors up to the output root. Each combined file contains categorized
questions from that directory and its descendants, including all available
language combinations. The mirrored runner combines once after all its jobs
finish; `--dry-run` does not combine. The same applies to Moodle XML question
and paper generation. PDF output is unaffected.

The combiner preserves source category markers and ignores OCR XML, category-only
XML, hidden checkpoint folders and existing combined imports. It does not update older
combined files with other names; those may be stale. Do not import both a
combined file and its individual source files into the same question bank
unless duplicates are intended. Existing `combined_moodle.xml` files not
created by this combiner are never overwritten.

All Moodle question XML under the output root must have one leading category.
The combiner refuses malformed or uncategorized XML and incomplete or changed
matching extraction and batch checkpoints. This applies to the **whole output
root**, even with `--subtree`; incomplete adaptive question-type checkpoints
also block combination. Backfill unrelated legacy XML first or use a
separate output root. Generation may already have succeeded when combination
fails. Resolve the reported error and rerun; completed checkpoint hashes remain
protected. Individual combined files are replaced atomically, but a failure
while writing multiple levels can leave some levels updated and others stale.

Do not run other writers against the output tree during combination. A
`.moodle-combiner.lock` prevents simultaneous combinations at the same root.
If an interrupted process leaves that lock behind, verify no combination is
running before removing that specific lock file and retrying.

Extraction prompt snapshots include the standard and language set in their
filenames, for example `keep312_neet_ug_english_bengali_pages_1_5_prompt.md`.
For simultaneous language runs, use a separate `--output-root` for each run:
OCR caches, images and temporary files remain shared within an output root and
are not safe for concurrent extraction of the same PDF. After the runs finish,
collect the language XML files into a common staging tree for the checker.

The extractor normally skips existing outputs. Existing XML and combined imports
are not changed by updating these rules; regenerate into staging, review, and
rebuild combined imports separately. New checkpoints do not record the NCERT
prompt hash; hashes in older checkpoints are ignored. A resumed extraction may
therefore combine batches or saved responses created under different prompt
versions; review them or start a fresh extraction if the changes affect
question inclusion or content. Existing batches still must pass the current
structural validation. Do not use `--force` against the current question bank
unless replacing those files is intentional.

## Add categories to existing XML

The offline backfill script can write categorized **copies** to a new directory
or update the input in place with checkpoint-aware backups. Run from the
project directory and preview the mappings first:

```powershell
python scripts\backfill_moodle_categories.py `
  --source-root ..\question-papers-vault `
  --input-root ..\question-papers-vault-moodle `
  --subtree neet_ug\ncert_exemplar\class11\biology `
  --output-root ..\question-papers-vault-categorized `
  --top-category "Chapterwise Question Bank" `
  --languages english `
  --languages english,bengali `
  --languages english,hindi `
  --dry-run
```

Remove `--dry-run` to write the copies. Remove `--subtree` to process the
whole mirrored input root, which must correspond to the same source root.
Repeat `--languages` for every language combination in the input; unknown or
ambiguous matches and malformed XML stop the run before writing. Legacy Moodle
files named exactly like their source need an explicit `--legacy-languages`
combination. OCR XML, hidden checkpoints, combined files, category-only files,
and XML containing multiple category markers are skipped. Existing single category markers are
replaced with the calculated path. Checkpoints are not copied, so the new tree
is for reviewing and importing, not resuming generation.

To update in place, stop all extraction, generation, repair, and other writers
for the input and source trees. Back up the entire bank independently first;
the script's backups contain only files it processes. Use the same input and
output root and a **new, separate** backup root:

```powershell
python scripts\backfill_moodle_categories.py `
  --source-root ..\question-papers-vault `
  --input-root ..\question-papers-vault-moodle `
  --output-root ..\question-papers-vault-moodle `
  --backup-root ..\question-papers-vault-category-backup `
  --subtree neet_ug\ncert_exemplar\class11\biology `
  --top-category "Chapterwise Question Bank" `
  --languages english `
  --languages english,bengali `
  --languages english,hindi `
  --dry-run
```

After reviewing the preview, remove `--dry-run`. The backup root must not exist
and cannot overlap either source or input root. The script validates matching
completed extraction and batch checkpoints and their output hashes before any
writes; extraction also validates the source PDF hash. It updates category
settings and output hashes without disabling resume integrity checks. For batch
checkpoints, supply the original **batch** spec with `--spec`; every output in
that checkpoint must be selected and match its recorded hash. Checkpoint
languages must match the filename mapping. Incomplete, unsupported, missing,
changed, or conflicting checkpoint outputs stop preflight rather than being
silently adopted. XML without a checkpoint is backed up and updated, but still
cannot be resumed as a checkpointed run.

Files are committed one checkpoint group at a time, not as one transaction
across the entire tree. If interrupted, a checkpoint being updated is marked
`category_backfill_pending` and resume is blocked. Stop all jobs, preserve the
current files and backup, and restore only the affected group's XML files from
the corresponding paths under the backup root; restore its checkpoint JSON
**last**. Extraction checkpoints list `output_file`; batch checkpoints list
each `items[].output_file`. Earlier successfully committed groups should not
be restored automatically. If interruption occurred before a checkpoint was
marked pending, that group has not been changed. Individual files without
checkpoints are replaced atomically. Use a new backup root for any rerun.
The script does not lock other processes, so stopping concurrent writers is
essential. After migration, resume with the same top category, source root,
languages, and remaining generation settings as the checkpoint.

The same script handles mirrored chapter-question outputs, including separate
question types and split papers. For generated exam papers, pass `--spec` with
the original batch or paper spec under `--source-root`, and restrict
`--input-root` or `--subtree` to outputs generated from that spec. The script
uses the explicitly supplied spec for every matched paper; it cannot infer
which spec produced a paper.

After generating all three languages, check the XML structure and question counts:

```powershell
python scripts\check_ncert_exemplar_xml.py --root ..\ncert-exemplar-staging
```

The checker reports missing language files, unsupported question types, malformed matching items, invalid grading,
missing referenced images, and unequal question counts across languages. Equal
counts do **not** prove that the same questions are present in every language,
that every eligible question was extracted, or that excluded questions are
absent. The checker also cannot verify answers, translations or diagrams.
Review these against the source before replacing existing files or rebuilding
and importing the combined Moodle XMLs.

## Offline recovery from saved responses

Back up the complete bank, including hidden checkpoint folders and OCR assets.
With the pipeline stopped, rebuild into a **new, separate** directory:

```powershell
python scripts\rebuild_ncert_exemplar_xml.py --source-root ..\question-papers-vault-moodle\neet_ug\ncert_exemplar --output-root ..\ncert-exemplar-recovered
```

The script sends no AI or OCR requests. It uses saved responses in checkpoint
batch order, not the current chapter XML or saved batch XML; it validates
question structure, checkpoint counts, and referenced images. It reports
missing or invalid data and exits nonzero rather than writing a partial chapter.
The output directory must not already exist. Rebuilt XMLs do not include
checkpoints and cannot be resumed with `--resume`.

Recovery is not an exact restoration: responses may contain questions removed
after extraction, including VSA, SA or LA, or miss later manual corrections.
The structural validator cannot detect those differences. Review every result
against the source and retained backups before replacing any existing files.

## Repair completed extraction checkpoints

The mirrored extractor can also repair the existing XML and batch checkpoints
in place from saved responses, without AI or OCR requests. Stop all extraction
jobs and back up the entire bank first. Run a preflight for one language set:

```powershell
python scripts\run_path_mirrored_extraction.py --source-root ..\question-papers-vault\neet_ug\ncert_exemplar --output-root ..\question-papers-vault-moodle\neet_ug\ncert_exemplar --languages english --repair --repair-backup-root ..\ncert-exemplar-repair-backup-english --dry-run
```

If preflight passes, rerun the same command without `--dry-run`. Use a new,
separate `--repair-backup-root` for each language set, such as
`english,bengali` and `english,hindi`. The repair operates only on the chosen
language set. PDFs with no matching output or checkpoint are skipped; existing
outputs without checkpoints, incomplete checkpoints, changed source PDFs,
missing responses or images, and invalid data cause preflight to fail before
any files change. A missing chapter XML is recreated if its complete checkpoint
and responses are available. `--repair` cannot be combined with `--resume`,
`--force` or `--replace-pages`.

Repair replaces the current chapter XML and its batch XMLs, and updates the
checkpoint hash. It saves the previous versions of these files and the
checkpoint manifest under the backup root, preserving their paths relative to
the output root. A `*.repair-info.json` file records whether the chapter XML
existed before repair. Those backups contain the *pre-repair* files, which may
already be damaged; they are not a substitute for a full bank backup. Do not
run extraction or another repair concurrently. Chapters are committed
individually: if a later chapter fails, earlier repairs remain in place.

An interrupted repair leaves the affected manifest marked `repair_pending`,
which blocks `--resume`. Stop all jobs and preserve all files. Restore only that
chapter's XML and batch XMLs from the displayed backup root, then restore its
manifest **last**, so `--resume` remains blocked during restoration. Do not copy
the entire backup over already repaired chapters or overwrite saved responses.
If the backup's repair-info file says `output_existed` is false, remove only the
newly created chapter XML before restoring the manifest.
Restoration returns the chapter to its pre-repair state, which may itself need
repair. After a successful
repair, `--resume` works when the source PDF and extraction settings match the
checkpoint; it does not re-extract a completed chapter. Saved responses may
still restore excluded or manually removed questions, so review the recovered
content before using it.
