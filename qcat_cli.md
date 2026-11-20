# `qcat_cli.php` — Moodle 5.3.x Question Category CLI Reference

`qcat_cli.php` is a custom command-line utility for inspecting, exporting, and purging Moodle 5.3.x question bank category trees (`question_categories`) and their associated `question_bank_entries` in a MariaDB-backed Moodle container (`moodle_app`).

It defaults to **read-only / dry-run** behavior on destructive actions and traverses category trees **post-order** (deepest child categories first, root category last) to respect parent/child relationships and maintain database integrity.

---

## 1. Prerequisites & Setup

* **File Location:** Place `qcat_cli.php` in your Moodle application root directory alongside `config.php` (e.g., `/var/www/html/public/qcat_cli.php` or inside your Docker bind-mounted source directory).
* **Execution Context:** Execute via `docker exec` as the `www-data` user:
  ```bash
  docker exec -u www-data moodle_app php qcat_cli.php [options]
  ```
* **Moodle 5.3.x Data Model:** Category entry counts, relocations, and deletions operate against `{question_bank_entries}` (`questioncategoryid`), `{question_versions}`, and `{question}` rather than legacy pre-4.x `question.category` fields.

---

## 2. CLI Options & Flags

| Flag | Short | Values / Type | Default | Description |
| :--- | :--- | :--- | :--- | :--- |
| `--action` | `-a` | `list` \| `purge` | *(required)* | Operation mode: inspect/export a tree (`list`) or delete a tree (`purge`). |
| `--categoryid` | `-c` | `<int>` | `0` *(required)* | Root `question_categories.id` to inspect or purge. |
| `--format` | `-f` | `text` \| `json` \| `csv` | `text` | Output format when using `--action=list`. |
| `--moveto` | `-m` | `<int>` | `0` | Destination holding category ID to relocate `question_bank_entries` into before deleting categories. |
| `--delete-questions` | `-d` | boolean flag | `false` | Delete unreferenced `question_bank_entries`, versions, questions, and type option rows in the target tree. |
| `--execute` | `-e` | boolean flag | `false` | Commits changes to MariaDB. Without `--execute`, `purge` runs in **DRY RUN** mode. |
| `--help` | `-h` | boolean flag | `false` | Displays CLI usage instructions and exits. |

> **Mutual Exclusion Rule:** `--moveto` and `--delete-questions` cannot be combined in the same command. Supplying both will cause the script to exit immediately with an error.

---

## 3. Commands & Examples

### A. View Help
Displays all available flags and usage syntax:
```bash
docker exec -u www-data moodle_app php qcat_cli.php --help
```

### B. List Category Tree (Indented Text)
Prints a visual hierarchy of the root category and all nested subcategories along with `question_bank_entries` counts:
```bash
docker exec -u www-data moodle_app php qcat_cli.php --action=list --categoryid=2467
```

### C. Export Category Tree (CSV or JSON)
Exports clean structured data (`id,parent,depth,path,name,entries`) to your host machine for auditing or archiving prior to deletion:
```bash
# Export to CSV
docker exec -u www-data moodle_app php qcat_cli.php --action=list --categoryid=2467 --format=csv > tree_2467.csv

# Export to JSON
docker exec -u www-data moodle_app php qcat_cli.php --action=list --categoryid=2467 --format=json > tree_2467.json
```

### D. Dry Run Purge on an Empty Category Tree
Simulates deletion and lists every category targeted in post-order without modifying MariaDB:
```bash
docker exec -u www-data moodle_app php qcat_cli.php --action=purge --categoryid=2865
```

### E. Execute Purge on an Empty Category Tree
Permanently deletes the empty category tree inside a MariaDB transaction and purges Moodle caches:
```bash
docker exec -u www-data moodle_app php qcat_cli.php --action=purge --categoryid=2865 --execute
docker exec -u www-data moodle_app php admin/cli/purge_caches.php
```

### F. Purge a Non-Empty Tree by Relocating Questions (`--moveto`)
Recommended approach when questions exist in the subtree and must be preserved in a holding category (e.g., ID `999` within the same context):
```bash
# Step 1: Dry run to verify counts and destination context
docker exec -u www-data moodle_app php qcat_cli.php --action=purge --categoryid=2467 --moveto=999

# Step 2: Execute live relocation and category deletion
docker exec -u www-data moodle_app php qcat_cli.php --action=purge --categoryid=2467 --moveto=999 --execute

# Step 3: Clear Moodle caches
docker exec -u www-data moodle_app php admin/cli/purge_caches.php
```

### G. Purge a Non-Empty Tree by Deleting Unreferenced Questions (`--delete-questions`)
Permanently deletes unused questions and categories. Only proceeds if **zero** targeted entries are referenced in `{question_references}`:
```bash
# Step 1: Dry run to verify reference checks
docker exec -u www-data moodle_app php qcat_cli.php --action=purge --categoryid=2467 --delete-questions

# Step 2: Execute live deletion
docker exec -u www-data moodle_app php qcat_cli.php --action=purge --categoryid=2467 --delete-questions --execute

# Step 3: Clear Moodle caches
docker exec -u www-data moodle_app php admin/cli/purge_caches.php
```

---

## 4. Consequences by Mode & Option

| Command / Option Combination | Database Impact | Question Bank Impact | Reversibility |
| :--- | :--- | :--- | :--- |
| `--action=list` (`text` \| `json` \| `csv`) | **None (Read-only)** | No changes. | N/A |
| `--action=purge` *(without `--execute`)* | **None (Dry Run)** | Simulates traversal and counts; writes nothing. | N/A |
| `--action=purge --execute` *(empty tree)* | Deletes rows in `question_categories` bottom-up. | Removes empty subcategories and root category. | Irreversible without DB backup. |
| `--action=purge --moveto=<ID> --execute` | Updates `question_bank_entries.questioncategoryid` to `<ID>`, then deletes emptied `question_categories` rows. | All questions and versions remain intact inside holding category `<ID>`. | Categories deleted; questions preserved. |
| `--action=purge --delete-questions --execute` | Deletes rows in `question_versions`, `question_bank_entries`, `question`, `question_answers`, `question_hints`, `qtype_*_options`, and `question_categories`. | Permanently destroys unreferenced questions and all categories in the tree. | **Permanently irreversible** without DB backup. |

---

## 5. Warnings & Built-In Guardrails

> **CRITICAL WARNING — Irreversible Data Loss:** Passing `--execute` with `--delete-questions` permanently deletes question records and category hierarchies directly from MariaDB. Always run a **dry run** first, export a tree snapshot (`--format=csv` or `json`), and take a MariaDB backup (`question_categories`, `question_bank_entries`, `question_versions`, `question`) before running `--execute` on large trees.

### Built-In Safety Guardrails
1. **Dry-Run Default:** Destructive database operations never execute unless `--execute` (`-e`) is explicitly passed.
2. **Last Category in Context Protection:** Aborts immediately if purging the target tree would leave `0` categories in that `contextid` (Moodle requires at least one category per context).
3. **Destination Validation (`--moveto`):** Aborts if the target holding category does not exist, belongs to a different `contextid`, or resides inside the subtree being deleted.
4. **Quiz / Reference Protection (`--delete-questions`):** Scans `{question_references}` before deleting. If any entry in the subtree is linked to a quiz or activity, the script aborts and instructs you to use `--moveto` instead.
5. **Post-Order Verification:** Deletes leaf categories first and checks before every single `DELETE` that `0` child categories and `0` `question_bank_entries` remain under that ID.
6. **MariaDB Transaction Rollback:** Wraps live execution in `$DB->start_delegated_transaction()`, automatically rolling back all uncommitted changes on any thrown exception (requires transactional `InnoDB` tables).
7. **Cache Synchronization:** Because `$DB` operations bypass web UI event hooks, always run `php admin/cli/purge_caches.php` after `--execute` so Moodle's UI reflects the updated category tree immediately.