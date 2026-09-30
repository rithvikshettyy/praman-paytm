"""PRD-PAYTM C6: nothing we only drafted or approved is ever called filed or submitted."""

import ast
import re
from pathlib import Path

import yaml

from app import config

BANNED = re.compile(r"\b(filed|submitted)\b", re.IGNORECASE)
APP_DIR = Path(__file__).resolve().parent.parent / "app"


def _strings(tree: ast.AST):
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                docstrings.add(id(first.value))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            yield node


def test_no_string_in_the_backend_says_filed_or_submitted():
    hits = []
    for path in APP_DIR.rglob("*.py"):
        for node in _strings(ast.parse(path.read_text(encoding="utf-8"))):
            if BANNED.search(node.value):
                hits.append(f"{path.name}:{node.lineno}: {node.value[:60]!r}")
    assert hits == []


def _text_values(node):
    if isinstance(node, dict):
        for key, value in node.items():
            if key in {"message", "message_pending", "letter", "label"} and isinstance(value, str):
                yield value
            else:
                yield from _text_values(value)
    elif isinstance(node, list):
        for item in node:
            yield from _text_values(item)


def test_no_user_facing_text_in_data_says_filed_or_submitted():
    for path in config.DATA_DIR.rglob("*.yaml"):
        for text in _text_values(yaml.safe_load(path.read_text(encoding="utf-8"))):
            assert not BANNED.search(text), (path.name, text)
