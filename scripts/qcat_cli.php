<?php
define('CLI_SCRIPT', true);
require(__DIR__ . '/config.php');
require_once($CFG->libdir . '/clilib.php');

list($options, $unrecognized) = cli_get_params([
    'action'           => '',
    'categoryid'       => 0,
    'format'           => 'text',
    'moveto'           => 0,
    'delete-questions' => false,
    'execute'          => false,
    'help'             => false,
], [
    'a' => 'action',
    'c' => 'categoryid',
    'f' => 'format',
    'm' => 'moveto',
    'd' => 'delete-questions',
    'e' => 'execute',
    'h' => 'help',
]);

if ($options['help'] || empty($options['action']) || empty($options['categoryid'])) {
    echo <<<USAGE
Moodle 5.3.x Question Category CLI Tool

Usage:
  php qcat_cli.php --action=list  --categoryid=<ID> [--format=text|json|csv]
  php qcat_cli.php --action=purge --categoryid=<ID> [--moveto=<ID> | --delete-questions] [--execute]

Actions:
  list                Print or export category tree and question_bank_entries counts
  purge               Purge a category tree (DRY RUN by default; pass --execute to apply)

Options:
  -a, --action            list | purge
  -c, --categoryid        Root category ID to inspect or purge
  -f, --format            Output format for 'list': text (default) | json | csv
  -m, --moveto            Destination category ID to move question_bank_entries before deleting
  -d, --delete-questions  Delete unused question_bank_entries in targeted categories (aborts if referenced)
  -e, --execute           Apply changes to MariaDB (without this, runs in DRY RUN mode)
  -h, --help              Show this help message

USAGE;
    exit(0);
}

$action = strtolower(trim($options['action']));
$rootcatid = (int)$options['categoryid'];
$format = strtolower(trim($options['format']));
$movetocatid = (int)$options['moveto'];
$deletequestions = !empty($options['delete-questions']);
$is_execute = !empty($options['execute']);

global $DB;

if (!in_array($action, ['list', 'purge'], true)) {
    cli_error("Invalid action '{$action}'. Use 'list' or 'purge'.");
}

if (!in_array($format, ['text', 'json', 'csv'], true)) {
    cli_error("Invalid format '{$format}'. Use 'text', 'json', or 'csv'.");
}

if ($movetocatid > 0 && $deletequestions) {
    cli_error("Cannot use --moveto and --delete-questions together. Choose one strategy.");
}

if (!$rootcategory = $DB->get_record('question_categories', ['id' => $rootcatid])) {
    cli_error("Category ID {$rootcatid} does not exist.");
}

$contextid = (int)$rootcategory->contextid;
$allcats = $DB->get_records('question_categories', ['contextid' => $contextid], 'sortorder ASC, id ASC');

$childrenmap = [];
foreach ($allcats as $cat) {
    $childrenmap[(int)$cat->parent][] = (int)$cat->id;
}

function get_descendants_postorder(int $parentid, array $childrenmap): array {
    $ids = [];
    if (!empty($childrenmap[$parentid])) {
        foreach ($childrenmap[$parentid] as $childid) {
            $ids = array_merge($ids, get_descendants_postorder($childid, $childrenmap));
            $ids[] = $childid;
        }
    }
    return $ids;
}

// Post-order: deepest children first, root category last.
$postorder_ids = array_merge(get_descendants_postorder($rootcatid, $childrenmap), [$rootcatid]);

// Fetch question_bank_entries counts per category in chunks.
$qcounts = [];
foreach (array_chunk($postorder_ids, 300) as $chunk) {
    $placeholders = implode(',', array_fill(0, count($chunk), '?'));
    $rows = $DB->get_records_sql("
        SELECT questioncategoryid AS catid, COUNT(id) AS cnt
        FROM {question_bank_entries}
        WHERE questioncategoryid IN ($placeholders)
        GROUP BY questioncategoryid
    ", $chunk);
    foreach ($rows as $row) {
        $qcounts[(int)$row->catid] = (int)$row->cnt;
    }
}

$total_entries = 0;
foreach ($postorder_ids as $cid) {
    $total_entries += $qcounts[$cid] ?? 0;
}

// -------------------------------------------------------------------------
// 1. LIST ACTION (TEXT / JSON / CSV)
// -------------------------------------------------------------------------
if ($action === 'list') {
    $rows = [];
    $collect_rows = function(int $catid, int $depth, string $path) use (&$collect_rows, &$rows, $allcats, $childrenmap, $qcounts): void {
        if (!isset($allcats[$catid])) {
            return;
        }
        $cat = $allcats[$catid];
        $currentpath = $path === '' ? $cat->name : ($path . ' / ' . $cat->name);
        $rows[] = [
            'id'      => (int)$cat->id,
            'parent'  => (int)$cat->parent,
            'depth'   => $depth,
            'path'    => $currentpath,
            'name'    => (string)$cat->name,
            'entries' => (int)($qcounts[$catid] ?? 0),
        ];
        if (!empty($childrenmap[$catid])) {
            foreach ($childrenmap[$catid] as $childid) {
                $collect_rows($childid, $depth + 1, $currentpath);
            }
        }
    };

    $collect_rows($rootcatid, 0, '');

    if ($format === 'json') {
        $payload = [
            'contextid'       => $contextid,
            'root_categoryid' => $rootcatid,
            'total_categories'=> count($rows),
            'total_entries'   => $total_entries,
            'categories'      => $rows,
        ];
        echo json_encode($payload, JSON_PRETTY_PRINT | JSON_UNESCAPED_UNICODE) . "\n";
        exit(0);
    }

    if ($format === 'csv') {
        $out = fopen('php://output', 'w');
        fputcsv($out, ['id', 'parent', 'depth', 'path', 'name', 'entries']);
        foreach ($rows as $r) {
            fputcsv($out, [$r['id'], $r['parent'], $r['depth'], $r['path'], $r['name'], $r['entries']]);
        }
        fclose($out);
        exit(0);
    }

    echo "==================================================\n";
    echo "CONTEXT ID: {$contextid} | ROOT CATEGORY ID: {$rootcatid}\n";
    echo "TOTAL CATEGORIES IN TREE: " . count($rows) . " | TOTAL ENTRIES: {$total_entries}\n";
    echo "==================================================\n";
    foreach ($rows as $r) {
        $indent = str_repeat('  ', $r['depth']);
        $prefix = $r['depth'] > 0 ? '└─ ' : '';
        echo "{$indent}{$prefix}[ID: {$r['id']}] {$r['name']} ({$r['entries']} entries)\n";
    }
    echo "==================================================\n";
    exit(0);
}

// -------------------------------------------------------------------------
// 2 & 3. PURGE ACTION (DRY RUN DEFAULT, --execute TO APPLY)
// -------------------------------------------------------------------------
$remaining_context_cats = count($allcats) - count($postorder_ids);
if ($remaining_context_cats <= 0) {
    cli_error("Refusing to purge: this would leave 0 question categories in context {$contextid}.");
}

if ($movetocatid > 0) {
    if (in_array($movetocatid, $postorder_ids, true)) {
        cli_error("--moveto category {$movetocatid} is inside the target deletion tree.");
    }
    $targetcat = $DB->get_record('question_categories', ['id' => $movetocatid]);
    if (!$targetcat) {
        cli_error("--moveto category {$movetocatid} does not exist.");
    }
    if ((int)$targetcat->contextid !== $contextid) {
        cli_error("--moveto category {$movetocatid} (context {$targetcat->contextid}) does not match context {$contextid}.");
    }
}

$entry_ids = [];
foreach (array_chunk($postorder_ids, 300) as $chunk) {
    $placeholders = implode(',', array_fill(0, count($chunk), '?'));
    $found = $DB->get_fieldset_sql("
        SELECT id
        FROM {question_bank_entries}
        WHERE questioncategoryid IN ($placeholders)
    ", $chunk);
    foreach ($found as $eid) {
        $entry_ids[] = (int)$eid;
    }
}

$referenced_entry_ids = [];
if (!empty($entry_ids)) {
    foreach (array_chunk($entry_ids, 300) as $chunk) {
        $placeholders = implode(',', array_fill(0, count($chunk), '?'));
        $qrefs = $DB->get_fieldset_sql("
            SELECT DISTINCT questionbankentryid
            FROM {question_references}
            WHERE questionbankentryid IN ($placeholders)
        ", $chunk);
        foreach ($qrefs as $qid) {
            $referenced_entry_ids[(int)$qid] = true;
        }
    }
}

if ($total_entries > 0 && $movetocatid === 0 && !$deletequestions) {
    cli_error(
        "Targeted tree contains {$total_entries} question_bank_entries. " .
        "Specify --moveto=<HOLDING_CAT_ID> to move them, or --delete-questions to delete unreferenced entries."
    );
}

if ($deletequestions && !empty($referenced_entry_ids)) {
    cli_error(
        "Refusing to delete questions: " . count($referenced_entry_ids) .
        " entries are referenced in question_references (e.g. quizzes). Use --moveto=<HOLDING_CAT_ID> instead."
    );
}

mtrace("==================================================");
mtrace("MODE: " . ($is_execute ? "LIVE EXECUTION (--execute)" : "DRY RUN (no changes made)"));
mtrace("==================================================");
mtrace("Root Category ID: {$rootcatid} ({$rootcategory->name})");
mtrace("Context ID: {$contextid} (Remaining categories after purge: {$remaining_context_cats})");
mtrace("Categories to delete (children first, root last): " . count($postorder_ids));
mtrace("Question bank entries found: " . count($entry_ids) . " (Referenced: " . count($referenced_entry_ids) . ")");

if ($total_entries > 0) {
    if ($movetocatid > 0) {
        mtrace("Strategy: MOVE entries -> Category ID {$movetocatid}");
    } else if ($deletequestions) {
        mtrace("Strategy: DELETE unreferenced entries, versions, question records & type rows");
    }
}
mtrace("--------------------------------------------------");
foreach ($postorder_ids as $cid) {
    $cname = $allcats[$cid]->name ?? '(unknown)';
    $cnt = $qcounts[$cid] ?? 0;
    mtrace("  [ID: {$cid}] {$cname} | Entries: {$cnt}");
}
mtrace("==================================================");

if (!$is_execute) {
    mtrace("DRY RUN COMPLETE. Pass --execute to apply these changes.");
    exit(0);
}

mtrace("Starting MariaDB transaction...");
$transaction = $DB->start_delegated_transaction();

try {
    if (!empty($entry_ids) && $movetocatid > 0) {
        foreach (array_chunk($postorder_ids, 300) as $chunk) {
            $placeholders = implode(',', array_fill(0, count($chunk), '?'));
            $params = array_merge([$movetocatid], $chunk);
            $DB->execute("
                UPDATE {question_bank_entries}
                SET questioncategoryid = ?
                WHERE questioncategoryid IN ($placeholders)
            ", $params);
        }
        mtrace("Moved " . count($entry_ids) . " entries to category {$movetocatid}.");
    } else if (!empty($entry_ids) && $deletequestions) {
        foreach (array_chunk($entry_ids, 200) as $echunk) {
            $eplaceholders = implode(',', array_fill(0, count($echunk), '?'));

            $qids = $DB->get_fieldset_sql("
                SELECT DISTINCT questionid
                FROM {question_versions}
                WHERE questionbankentryid IN ($eplaceholders)
            ", $echunk);

            $DB->delete_records_select('question_versions', "questionbankentryid IN ($eplaceholders)", $echunk);
            $DB->delete_records_select('question_bank_entries', "id IN ($eplaceholders)", $echunk);

            if (!empty($qids)) {
                foreach (array_chunk($qids, 200) as $qchunk) {
                    $qplaceholders = implode(',', array_fill(0, count($qchunk), '?'));
                    if ($DB->get_manager()->table_exists('question_hints')) {
                        $DB->delete_records_select('question_hints', "questionid IN ($qplaceholders)", $qchunk);
                    }
                    if ($DB->get_manager()->table_exists('question_answers')) {
                        $DB->delete_records_select('question_answers', "question IN ($qplaceholders)", $qchunk);
                    }
                    foreach (['qtype_multichoice_options', 'qtype_numerical_options', 'qtype_shortanswer_options', 'qtype_match_options', 'qtype_essay_options', 'qtype_ordering_options'] as $optable) {
                        if ($DB->get_manager()->table_exists($optable) && $DB->get_manager()->field_exists($optable, 'questionid')) {
                            $DB->delete_records_select($optable, "questionid IN ($qplaceholders)", $qchunk);
                        }
                    }
                    $DB->delete_records_select('question', "id IN ($qplaceholders)", $qchunk);
                }
            }
        }
        mtrace("Deleted " . count($entry_ids) . " unreferenced question bank entries.");
    }

    foreach ($postorder_ids as $cid) {
        if (!$DB->record_exists('question_categories', ['id' => $cid])) {
            continue;
        }
        if ($DB->record_exists('question_categories', ['parent' => $cid])) {
            throw new \moodle_exception("Abort: Category {$cid} still has child categories.");
        }
        if ($DB->record_exists('question_bank_entries', ['questioncategoryid' => $cid])) {
            throw new \moodle_exception("Abort: Category {$cid} still has question_bank_entries.");
        }
        $DB->delete_records('question_categories', ['id' => $cid]);
        mtrace("Deleted category ID: {$cid}");
    }

    $transaction->allow_commit();
    mtrace("PURGE COMPLETED SUCCESSFULLY.");
} catch (Throwable $e) {
    $transaction->rollback($e);
    mtrace("ERROR (rolled back): " . $e->getMessage());
    exit(1);
}