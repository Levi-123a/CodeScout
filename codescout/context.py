"""Plan complete line-range batches, with bounded related snippets per request."""

import difflib
import hashlib
import json
from pathlib import PurePosixPath

from .analysis import symbols
from .model import ModelError, input_chars

RULES_FILENAME = "CODESCOUT_RULES.md"


def changes(snapshot, previous):
    previous = previous or {}
    changed = sorted(path for path, text in snapshot.files.items() if previous.get(path) != text)
    missing = set(previous) - set(snapshot.files)
    removed = sorted(path for path in missing if any(path == excluded or excluded.endswith("/") and path.startswith(excluded) for excluded in snapshot.excluded))
    return changed, sorted(missing - set(removed)), removed


def _related(files, focus, text):
    terms = symbols(focus, text)
    scored = []
    for path, content in files.items():
        if path in {focus, RULES_FILENAME}:
            continue
        lines = content.splitlines(True)
        hits = [(index, sum(term in line for term in terms)) for index, line in enumerate(lines)]
        best = max(hits, key=lambda pair: pair[1], default=(0, 0))
        is_doc = PurePosixPath(path).suffix.lower() in {".md", ".rst", ".txt"}
        # Unchanged docs remain a fallback when source has no named symbols.
        score = best[1] * 10 + int(is_doc != (PurePosixPath(focus).suffix.lower() in {".md", ".rst", ".txt"}))
        scored.append((-score, path, max(0, best[0] - 5)))
    return sorted(scored)[:2]


def plan_batches(snapshot, previous, config, custom_rules, previous_coverage=None, required_paths=()):
    budget = min(config.get("context_chars", 60000), 12000)
    changed, deleted, removed = changes(snapshot, previous)
    files = snapshot.files
    targets = [path for path in files if path != RULES_FILENAME]
    if previous and previous_coverage is not None and changed and RULES_FILENAME not in changed:
        previously_checked = set(previous_coverage.get("checked", [])) | set(previous_coverage.get("reused", []))
        relevant = set(changed) | set(required_paths) | (set(targets) - previously_checked)
        for path in changed:
            relevant.update(item[1] for item in _related(files, path, files[path]))
        targets = [path for path in targets if path in relevant]
    reused = sorted(set(files) - set(targets) - {RULES_FILENAME})
    targets.sort(key=lambda path: (PurePosixPath(path).suffix.lower() in {".md", ".rst", ".txt"}, path))
    batches, skipped = [], []
    for path in targets:
        lines = files[path].splitlines(True) or [""]
        offset = 0
        while offset < len(lines):
            context = {"target": "worktree", "custom_rules": custom_rules, "focus": path, "changed": [path] if path in changed else [], "deleted": deleted[:50],
                       "files": {}, "file_ranges": {}, "diff": ""}
            if input_chars(config, context) >= budget:
                raise ModelError("约定超过单次上下文预算，请缩短约定或增加预算")
            end = offset
            while end < min(offset + 120, len(lines)):
                context["files"][path] = "".join(lines[offset:end + 1])
                context["file_ranges"][path] = [offset + 1, end + 1]
                if input_chars(config, context) > budget:
                    break
                end += 1
            if end == offset:
                skipped.append({"path": path, "start_line": offset + 1, "end_line": offset + 1, "reason": "单行超过请求预算"})
                offset += 1
                continue
            context["files"][path] = "".join(lines[offset:end])
            context["file_ranges"][path] = [offset + 1, end]
            for _, related, start in _related(files, path, files[path]):
                related_lines = files[related].splitlines(True) or [""]
                stop = min(start + 30, len(related_lines))
                while stop > start:
                    context["files"][related] = "".join(related_lines[start:stop])
                    context["file_ranges"][related] = [start + 1, stop]
                    if input_chars(config, context) <= budget:
                        break
                    stop -= 1
                if stop == start:
                    context["files"].pop(related, None)
                    context["file_ranges"].pop(related, None)
            if previous and path in changed:
                before_lines = previous.get(path, "").splitlines(True)
                diff = "".join(difflib.unified_diff(before_lines[offset:end], lines[offset:end], fromfile="before/" + path, tofile="after/" + path))
                context["diff"] = diff[:2000]
                while context["diff"] and input_chars(config, context) > budget:
                    context["diff"] = context["diff"][:-100]
            batch_id = hashlib.sha256(json.dumps(context, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            batches.append({"id": batch_id, "path": path, "start_line": offset + 1, "end_line": end, "context": context, "context_chars": input_chars(config, context)})
            offset = end
    coverage = {"checked": [], "reused": reused, "uncovered": sorted(set(targets)), "excluded": snapshot.excluded, "warnings": list(snapshot.warnings), "changed": changed,
                "deleted": deleted, "removed_from_scope": removed, "ranges": [], "skipped_ranges": skipped, "budget_chars": budget, "context_chars": 0}
    return batches, coverage
