"""Build snapshot-consistent context and validate every reported source citation."""

import json

from .model import ModelClient, ModelError
from .analysis import syntax_findings
from .context import plan_batches



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


def check_snapshot(snapshot, previous, config, custom_rules, resume=None, on_progress=None, should_continue=None, previous_coverage=None, required_paths=()):
    coverage = {"checked": [], "uncovered": sorted(snapshot.files), "excluded": snapshot.excluded, "warnings": snapshot.warnings}
    completed = []
    findings = syntax_findings(snapshot.files)
    progress = {"completed": 0, "total": 0, "current_file": ""}
    def report(status, error=None):
        result = {"status": status, "findings": list(findings), "coverage": dict(coverage), "git": snapshot.git, "model": config["model"],
                  "model_version": config.get("version", 0), "batches": list(completed), "progress": dict(progress)}
        if error:
            result["error"] = error
        return result
    try:
        batches, coverage = plan_batches(snapshot, previous, config, custom_rules, previous_coverage, required_paths)
        progress["total"] = len(batches)
        saved = {batch["id"]: batch for batch in (resume or {}).get("batches", [])}
        done_by_path = {}
        required_by_path = {}
        for batch in batches:
            required_by_path[batch["path"]] = required_by_path.get(batch["path"], 0) + 1
        skipped_paths = {item["path"] for item in coverage["skipped_ranges"]}
        seen = {json.dumps(item, sort_keys=True, ensure_ascii=False) for item in findings}
        for batch in batches:
            if should_continue and not should_continue():
                return report("superseded", "检查对象已变化")
            progress["current_file"] = batch["path"]
            if on_progress:
                on_progress(report("running"))
            issues = saved[batch["id"]]["findings"] if batch["id"] in saved else ModelClient().check(config, batch["context"])
            validate_findings(issues, snapshot.files)
            for issue in issues:
                for citation in [issue, *issue["related"]]:
                    bounds = batch["context"]["file_ranges"].get(citation["path"])
                    if bounds is None or not bounds[0] <= citation["line"] <= bounds[1]:
                        raise ModelError("模型证据引用了本批次未读取的行段")
                identity = json.dumps(issue, sort_keys=True, ensure_ascii=False)
                if identity not in seen:
                    findings.append(issue)
                    seen.add(identity)
            completed.append({key: batch[key] for key in ("id", "path", "start_line", "end_line", "context_chars") } | {"findings": issues})
            done_by_path[batch["path"]] = done_by_path.get(batch["path"], 0) + 1
            coverage["ranges"] = [{key: item[key] for key in ("path", "start_line", "end_line")} for item in completed]
            coverage["checked"] = sorted(path for path, count in done_by_path.items() if count == required_by_path[path] and path not in skipped_paths)
            coverage["uncovered"] = sorted(set(required_by_path) - set(coverage["checked"]) | skipped_paths)
            coverage["context_chars"] = max(coverage["context_chars"], batch["context_chars"])
            progress["completed"] = len(completed)
            if on_progress:
                on_progress(report("running"))
        status = "partial" if coverage["uncovered"] or coverage["warnings"] else "completed"
        return report(status)
    except ModelError as error:
        return report("failed", str(error))
