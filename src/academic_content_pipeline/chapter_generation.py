"""Utilities for parsing syllabus chapters and splitting exact quotas."""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from typing import Dict, List


def _chapter_id(title: str, index: int) -> str:
    normalized = unicodedata.normalize("NFKD", title)
    slug = re.sub(r"[^a-z0-9]+", "-", normalized.casefold()).strip("-")
    return f"ch{index:03d}-{slug or 'chapter'}"


def parse_syllabus_chapters(path: Path) -> List[Dict[str, str]]:
    """Split Markdown at leaf headings, excluding the document title."""
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    headings = []
    for index, line in enumerate(lines):
        match = re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", line)
        if match:
            headings.append(
                {
                    "line": index,
                    "level": len(match.group(1)),
                    "title": match.group(2).strip(),
                }
            )
    if not headings:
        raise ValueError(f"Syllabus has no Markdown chapter headings: {path}")

    chapters = []
    for heading_index, heading in enumerate(headings):
        if heading_index == 0 and heading["level"] == 1:
            continue
        end_line = len(lines)
        has_child = False
        for later in headings[heading_index + 1 :]:
            if later["level"] <= heading["level"]:
                end_line = later["line"]
                break
            has_child = True
        if has_child:
            continue
        content = "\n".join(lines[heading["line"] : end_line]).strip()
        if not content:
            continue
        chapters.append(
            {
                "id": _chapter_id(heading["title"], len(chapters) + 1),
                "title": heading["title"],
                "content": content,
            }
        )
    if not chapters:
        raise ValueError(f"Syllabus has no non-empty chapter sections: {path}")
    return chapters


def allocate_matrix(row_totals: List[int], column_totals: List[int]) -> List[List[int]]:
    """Round proportional allocations while preserving both exact margins."""
    if any(value < 0 for value in row_totals + column_totals):
        raise ValueError("Allocation totals must be non-negative.")
    if sum(row_totals) != sum(column_totals):
        raise ValueError("Row and column allocation totals must match.")
    total = sum(row_totals)
    if total == 0:
        return [[0 for _ in column_totals] for _ in row_totals]

    matrix = []
    row_deficits = []
    column_deficits = column_totals.copy()
    fractional_edges = []
    for row_index, row_total in enumerate(row_totals):
        values = []
        row_floor_total = 0
        for column_index, column_total in enumerate(column_totals):
            numerator = row_total * column_total
            value, remainder = divmod(numerator, total)
            values.append(value)
            row_floor_total += value
            column_deficits[column_index] -= value
            if remainder:
                fractional_edges.append(
                    (remainder / total, row_index, column_index)
                )
        matrix.append(values)
        row_deficits.append(row_total - row_floor_total)

    fractional_edges.sort(reverse=True)
    row_count = len(row_totals)
    column_count = len(column_totals)
    source = 0
    row_offset = 1
    column_offset = row_offset + row_count
    sink = column_offset + column_count
    graph: List[List[List[int]]] = [[] for _ in range(sink + 1)]

    def add_edge(start: int, end: int, capacity: int) -> List[int]:
        forward = [end, capacity, len(graph[end])]
        backward = [start, 0, len(graph[start])]
        graph[start].append(forward)
        graph[end].append(backward)
        return forward

    for row_index, deficit in enumerate(row_deficits):
        add_edge(source, row_offset + row_index, deficit)
    for _, row_index, column_index in fractional_edges:
        add_edge(row_offset + row_index, column_offset + column_index, 1)
    for column_index, deficit in enumerate(column_deficits):
        add_edge(column_offset + column_index, sink, deficit)

    required_flow = sum(row_deficits)
    flow = 0
    while flow < required_flow:
        parent: List[tuple[int, int] | None] = [None] * len(graph)
        parent[source] = (source, -1)
        queue = [source]
        for node in queue:
            for edge_index, edge in enumerate(graph[node]):
                if edge[1] > 0 and parent[edge[0]] is None:
                    parent[edge[0]] = (node, edge_index)
                    queue.append(edge[0])
                    if edge[0] == sink:
                        break
            if parent[sink] is not None:
                break
        if parent[sink] is None:
            raise ValueError("Could not reconcile integer chapter quota allocations.")
        node = sink
        while node != source:
            parent_entry = parent[node]
            if parent_entry is None:
                raise RuntimeError("Internal allocation flow path is incomplete.")
            previous, edge_index = parent_entry
            edge = graph[previous][edge_index]
            edge[1] -= 1
            graph[node][edge[2]][1] += 1
            node = previous
        flow += 1

    for row_index in range(row_count):
        for edge in graph[row_offset + row_index]:
            node = edge[0]
            if column_offset <= node < sink and edge[1] == 0:
                reverse_edge = graph[node][edge[2]]
                if reverse_edge[1] == 1:
                    matrix[row_index][node - column_offset] += 1
    if any(
        sum(matrix[row_index][column_index] for row_index in range(row_count))
        != column_totals[column_index]
        for column_index in range(column_count)
    ):
        raise ValueError("Chapter quota allocation did not preserve column totals.")
    return matrix


def group_chapters(
    chapters: List[Dict[str, str]],
    counts: Dict[str, int],
    max_questions: int,
) -> List[Dict[str, object]]:
    """Group chapters in syllabus order without exceeding a target request size."""
    if max_questions < 1:
        raise ValueError("max_questions_per_request must be at least 1.")
    groups = []
    current = []
    current_count = 0
    for chapter in chapters:
        count = counts[chapter["id"]]
        if count <= 0:
            continue
        if current and current_count + count > max_questions:
            groups.append({"chapters": current, "question_count": current_count})
            current = []
            current_count = 0
        current.append(chapter)
        current_count += count
    if current:
        groups.append({"chapters": current, "question_count": current_count})
    return groups
