"""Hard rule (shared conventions): users.sim_label ('human'/'bot') exists ONLY for Member C's analytics.
No defence, allocation or UI code may read it. This enforces it for everything Member B ships in
backend/app/defence, and for the SQL it sends to the database.

Checked structurally rather than with a naive grep: identifiers, attribute names, keyword arguments and
every NON-docstring string literal (SQL lives in strings) are searched. Comments and docstrings that merely
explain the rule are allowed. The migrations and the .csv/.txt/.sql data files are searched as raw text
without any allowance, because a migration must not even create such a column."""
import ast
import re
from pathlib import Path

DEFENCE = Path(__file__).resolve().parents[1] / "app" / "defence"
NEEDLE = re.compile(r"sim_?label", re.IGNORECASE)


def python_files():
    return sorted(p for p in DEFENCE.rglob("*.py") if "__pycache__" not in p.parts)


def docstring_nodes(tree: ast.AST) -> set[int]:
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) \
                    and isinstance(body[0].value.value, str):
                ids.add(id(body[0].value))
    return ids


def offences(path: Path, root: Path = DEFENCE) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    docs = docstring_nodes(tree)
    found = []
    for node in ast.walk(tree):
        text = None
        if isinstance(node, ast.Name):
            text = node.id
        elif isinstance(node, ast.Attribute):
            text = node.attr
        elif isinstance(node, ast.keyword) and node.arg:
            text = node.arg
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            text = node.name
        elif isinstance(node, ast.arg):
            text = node.arg
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
            text = node.value
        if text and NEEDLE.search(text):
            found.append(f"{path.relative_to(root)}:{getattr(node, 'lineno', '?')}: {text[:60]!r}")
    return found


def test_the_scan_actually_sees_the_files():
    names = {p.name for p in python_files()}
    assert {"gate.py", "engine.py", "writer.py", "collect.py", "0003_decisions.py", "routes.py"} <= names


def test_no_defence_python_code_references_the_ground_truth_column():
    bad = [o for p in python_files() for o in offences(p)]
    assert bad == [], "defence code must never touch users.sim_label:\n" + "\n".join(bad)


def test_the_scanner_would_catch_a_violation(tmp_path):
    """Positive control: the REAL scanner, pointed at a file that breaks the rule in three different ways (argument, attribute, SQL string)."""
    f = tmp_path / "bad.py"
    f.write_text(
        "def f(row, sim_label=None):\n"
        "    x = row.sim_label\n"
        '    return "SELECT sim_label FROM users"\n'
    )
    assert len(offences(f, tmp_path)) == 3
    ok = tmp_path / "ok.py"
    ok.write_text(
        '"""Explains that sim_label is never read."""\n'
        "# sim_label: comment only\n"
        "x = 1\n"
    )
    assert offences(ok, tmp_path) == []


def test_data_files_and_templates_are_clean_too():
    for p in DEFENCE.rglob("*"):
        if p.is_file() and p.suffix in {".csv", ".txt", ".sql", ".mako", ".ini"}:
            assert not NEEDLE.search(p.read_text(encoding="utf-8")), p


def test_the_decisions_table_has_no_ground_truth_column():
    migration = (DEFENCE / "migrations" / "versions" / "0003_decisions.py").read_text(encoding="utf-8")
    sql = migration[migration.index("CREATE TABLE defence.decisions"):migration.index("CREATE INDEX decisions_event_idx")]
    cols = {ln.split()[0] for ln in sql.splitlines()[1:] if ln.strip() and not ln.strip().startswith(("--", ")"))}
    assert cols == {"id", "event_id", "user_id", "ts", "action", "weight", "score", "signals", "layer", "ip", "device", "reason"}
