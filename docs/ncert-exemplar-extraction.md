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
python scripts\run_path_mirrored_extraction.py --source-root ..\question-papers-vault\neet_ug\ncert_exemplar --output-root ..\ncert-exemplar-staging --languages english
python scripts\run_path_mirrored_extraction.py --source-root ..\question-papers-vault\neet_ug\ncert_exemplar --output-root ..\ncert-exemplar-staging --languages english,bengali
python scripts\run_path_mirrored_extraction.py --source-root ..\question-papers-vault\neet_ug\ncert_exemplar --output-root ..\ncert-exemplar-staging --languages english,hindi
```

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
