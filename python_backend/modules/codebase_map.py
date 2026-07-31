"""
Module 9: Codebase Map Generator
Builds an ASCII file tree and import dependency graph for the project.
"""
import ast
import os
from pathlib import Path
from shared.schema.snapshot_schema import CodebaseInfo as CodebaseMap


def build_map(project_root: str = ".",
              max_depth: int = 4,
              extensions: tuple = (".py", ".ts", ".tsx", ".js", ".swift")) -> CodebaseMap:
    root = Path(project_root).resolve()
    cmap = CodebaseMap()
    cmap.root = str(root)

    # File tree
    lines: list[str] = []
    total_files = 0
    total_lines = 0

    for dirpath, dirnames, filenames in os.walk(root):
        # Skip hidden and common noise dirs
        dirnames[:] = [
            d for d in sorted(dirnames)
            if not d.startswith(".") and d not in
            {"__pycache__", "node_modules", ".git", "venv", ".venv",
             "dist", "build", ".build", "DerivedData"}
        ]
        depth = len(Path(dirpath).relative_to(root).parts)
        if depth >= max_depth:
            dirnames.clear()
            continue
        indent = "  " * depth
        folder = Path(dirpath).name
        if depth > 0:
            lines.append(f"{indent}{folder}/")
        for fname in sorted(filenames):
            fpath = Path(dirpath) / fname
            if fpath.suffix in extensions:
                total_files += 1
                try:
                    lc = len(fpath.read_text(errors="replace").splitlines())
                    total_lines += lc
                    lines.append(f"{'  ' * (depth+1)}{fname}  ({lc}L)")
                except Exception:
                    lines.append(f"{'  ' * (depth+1)}{fname}")

    cmap.file_tree   = "\n".join(lines)
    cmap.total_files = total_files
    cmap.total_lines = total_lines

    # Python dependency edges
    cmap.dependency_edges = _build_python_deps(root)

    return cmap


def _build_python_deps(root: Path) -> list[str]:
    """
    Parse import statements from all .py files.
    Returns list of "source.py -> target.py" strings.
    """
    edges: list[str] = []
    py_names = {p.stem for p in root.rglob("*.py")}

    for py_file in root.rglob("*.py"):
        if any(part.startswith(".") for part in py_file.parts):
            continue
        try:
            tree = ast.parse(py_file.read_text(errors="replace"))
        except SyntaxError:
            continue

        source = py_file.relative_to(root)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [alias.name.split(".")[0] for alias in node.names]
                elif node.module:
                    names = [node.module.split(".")[0]]
                for name in names:
                    if name in py_names and name != py_file.stem:
                        edge = f"{source} -> {name}.py"
                        if edge not in edges:
                            edges.append(edge)

    return edges[:50]  # cap for readability


def map_to_overlay_lines(cmap: CodebaseMap) -> list[str]:
    return [
        f"Project: {Path(cmap.root).name}",
        f"  {cmap.total_files} files  {cmap.total_lines:,} lines",
    ]
