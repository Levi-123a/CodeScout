"""Build snapshot-consistent context and validate every reported source citation."""

import difflib
import json
from pathlib import PurePosixPath

from .model import ModelClient, ModelError


def _size(context):
    return len(json.dumps(context, ensure_ascii=False))


def build_context(snapshot, previous, budget, custom_rules):
    previous = previous or {}
    changed = sorted(name for name, text in snapshot.files.items() if previous.get(name) != text)
    missing = set(previous) - set(snapshot.files)
    removed_from_scope = sorted(name for name in missing if any(name == excluded or excluded.endswith("/") and name.startswith(excluded) for excluded in snapshot.excluded))
    deleted = sorted(missing - set(removed_from_scope))
    rules = sorted(name for name in snapshot.files if PurePosixPath(name).name in {"AGENTS.md", "CLAUDE.md"})
    docs = sorted(name for name in snapshot.files if name.lower().endswith((".md", ".rst", ".txt")))
    priority = list(dict.fromkeys(rules + changed + docs + sorted(snapshot.files)))
    context = {"target": "worktree", "custom_rules": custom_rules, "rules": [], "changed": changed, "deleted": deleted, "diff": "", "files": {}}
    # The model sees complete selected files. No silent per-file truncation.
    uncovered = []
    if _size(context) > budget:
        raise ModelError("约定与变化列表超过上下文预算，请增加预算或缩小项目范围")
    for name in priority:
        context["files"][name] = snapshot.files[name]
        if name in rules:
            context["rules"].append({"path": name, "scope": str(PurePosixPath(name).parent)})
        if _size(context) > budget:
            del context["files"][name]
            if name in rules:
                context["rules"].pop()
            uncovered.append(name)
    if any(name in uncovered for name in rules):
        raise ModelError("有效约定文件超出上下文预算，未执行检查；请增加预算")
    diffs = []
    for name in sorted(set(changed) | set(deleted)):
        if name not in context["files"] and name not in deleted:
            continue
        diff = "".join(difflib.unified_diff(previous.get(name, "").splitlines(True), snapshot.files.get(name, "").splitlines(True), fromfile="before/" + name, tofile="after/" + name))
        context["diff"] = "".join(diffs) + diff
        if _size(context) <= budget:
            diffs.append(diff)
    context["diff"] = "".join(diffs)
    coverage = {"checked": sorted(context["files"]), "uncovered": sorted(uncovered), "excluded": snapshot.excluded,
                "warnings": snapshot.warnings, "changed": changed, "deleted": deleted, "removed_from_scope": removed_from_scope, "context_chars": _size(context), "budget_chars": budget,
                "rules": context["rules"], "diff_included": bool(context["diff"])}
    return context, coverage


def _validate_location(location, files):
    if not isinstance(location, dict):
        raise ModelError("模型问题位置格式无效")
    path, line, evidence = location.get("path"), location.get("line"), location.get("evidence")
    if not isinstance(path, str) or path not in files or not isinstance(line, int) or isinstance(line, bool):
        raise ModelError("模型问题引用了未检查的文件或无效行号")
    lines = files[path].splitlines()
    if line < 1 or line > len(lines) or not isinstance(evidence, str) or not evidence.strip() or evidence not in lines[line - 1]:
        raise ModelError("模型问题的行号或证据与源码不一致，检查未完成")


def validate_findings(findings, files):
    for item in findings:
        _validate_location(item, files)
        for key in ("title", "reason", "rule", "suggestion"):
            if not isinstance(item.get(key), str) or not item[key].strip() or len(item[key]) > 10000:
                raise ModelError("模型问题缺少说明、约定或修复建议")
        if item.get("category") not in {"convention", "quality", "comment", "documentation"} or item.get("severity") not in {"low", "medium", "high"} or item.get("confidence") not in {"low", "medium", "high"}:
            raise ModelError("模型问题分类或置信度无效")
        if not isinstance(item.get("related"), list) or len(item["related"]) > 20:
            raise ModelError("模型关联证据格式无效")
        for location in item["related"]:
            _validate_location(location, files)
        if item["category"] == "documentation" and not item["related"]:
            raise ModelError("文档一致性问题缺少关联证据")
    return findings


def check_snapshot(snapshot, previous, config, custom_rules):
    coverage = {"checked": [], "uncovered": sorted(snapshot.files), "excluded": snapshot.excluded, "warnings": snapshot.warnings}
    try:
        context, coverage = build_context(snapshot, previous, config.get("context_chars", 60000), custom_rules)
        if not context["files"] and snapshot.files:
            raise ModelError("上下文预算无法容纳任何完整文件，检查未完成")
        findings = ModelClient().check(config, context) if context["files"] else []
        findings = validate_findings(findings, context["files"])
        status = "partial" if coverage["uncovered"] or snapshot.warnings else "completed"
        return {"status": status, "findings": findings, "coverage": coverage, "git": snapshot.git, "model": config["model"], "model_version": config.get("version", 0)}
    except ModelError as error:
        return {"status": "failed", "findings": [], "coverage": coverage, "error": str(error), "git": snapshot.git}
