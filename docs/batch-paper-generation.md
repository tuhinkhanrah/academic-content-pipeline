# Batch paper generation

`generate-paper` can run one paper blueprint repeatedly or combine multiple
blueprints into each test set. Each paper is generated, validated, checked for
exact duplicate questions, and committed separately. A JSON checkpoint and
saved AI responses are kept in the output directory so interrupted runs can be
continued.

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

For a single paper blueprint, `--num-tests 100` repeats that paper spec:

```powershell
python -m academic_content_pipeline.unified_pipeline agent generate-paper `
  --spec path/to/paper.yaml --num-tests 100 --output-dir output/mock_tests
```

Resume an interrupted run with the same spec, output directory, model, and
generation settings:

```powershell
python -m academic_content_pipeline.unified_pipeline agent generate-paper `
  --spec path/to/batch.yaml --output-dir output/mock_tests --resume
```

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
