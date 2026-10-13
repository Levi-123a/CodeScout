"""Deterministic Python diagnostics and symbols for context selection."""

import ast
import re
from pathlib import PurePosixPath


def symbols(path, text):
    names = set(re.findall(r"[A-Za-z_]\w{2,}", PurePosixPath(path).stem))
    if path.endswith(".py"):
        try:
            tree = ast.parse(text)
            names.update(node.name for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)))
            names.update(node.id for node in ast.walk(tree) if isinstance(node, ast.Name) and len(node.id) > 2)
        except SyntaxError:
            pass
    return names - {"self", "True", "False", "None", "print", "str", "int", "list", "dict"}


def syntax_findings(files):
    findings = []
    for path, text in files.items():
        if not path.endswith(".py"):
            continue
        try:
            ast.parse(text, filename=path)
        except SyntaxError as error:
            lines = text.splitlines()
            line = min(max(error.lineno or 1, 1), len(lines))
            if not lines or not lines[line - 1].strip():
                continue
            findings.append({"path": path, "line": line, "title": "Python 语法错误", "category": "quality", "severity": "high",
                             "reason": error.msg, "evidence": lines[line - 1], "rule": "代码应符合 Python 语法", "suggestion": "修正该行语法后重新检查",
                             "confidence": "high", "related": [], "checker": "python-ast"})
    return findings
