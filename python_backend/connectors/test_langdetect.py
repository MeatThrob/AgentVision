"""
Language-detection tests (pure stdlib). Asserts the ONE shared detector
(connectors.log_sources.detect_language) recognizes C# and C++ — including BARE
source trees with no build/project file — plus the existing ecosystems.
Run:  python3 python_backend/connectors/test_langdetect.py
"""
from __future__ import annotations
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from connectors import log_sources as ls  # noqa: E402

_fails = 0


def _mk(files: list[str]) -> str:
    d = tempfile.mkdtemp()
    for f in files:
        p = os.path.join(d, f)
        if os.path.dirname(f):
            os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "w").write("// x\n")
    return d


def check(name: str, files: list[str], expect: str):
    global _fails
    got = ls.detect_language(_mk(files))
    ok = got == expect
    if not ok:
        _fails += 1
    print(f"  [{'ok ' if ok else 'FAIL'}] {name:28} -> {got or '(none)':8} (want {expect})")


if __name__ == "__main__":
    print("=" * 66)
    print("language detection — C#/C++ (incl. bare files) + regressions")
    print("=" * 66)
    # C# / .NET
    check("C# .csproj",             ["App.csproj", "Program.cs"],            "dotnet")
    check("C# .sln",                ["App.sln", "Program.cs"],               "dotnet")
    check("C# BARE .cs (no proj)",  ["Program.cs", "Utils.cs"],              "dotnet")
    check("C# Directory.Build.props", ["Directory.Build.props", "A.cs"],     "dotnet")
    check("C# .fsproj",             ["App.fsproj", "Main.fs"],               "dotnet")
    check("F# bare .fs",            ["Main.fs"],                             "dotnet")
    # C / C++
    check("C++ CMakeLists",         ["CMakeLists.txt", "main.cpp"],          "cpp")
    check("C++ Makefile",           ["Makefile", "main.cpp"],                "cpp")
    check("C++ BARE .cpp/.cxx",     ["main.cpp", "engine.cxx"],              "cpp")
    check("C++ header-only .hpp/.h", ["vec.hpp", "mat.h"],                   "cpp")
    check("C bare .c/.h",           ["main.c", "header.h"],                  "cpp")
    check("C++ .vcxproj",           ["App.vcxproj", "main.cpp"],             "cpp")
    check("meson.build",            ["meson.build", "a.cpp"],                "cpp")
    check("vcpkg.json",             ["vcpkg.json", "a.cpp"],                 "cpp")
    # regressions — must still work
    check("python requirements",    ["requirements.txt", "app.py"],          "python")
    check("python bare .py",        ["app.py", "util.py"],                   "python")
    check("node package.json",      ["package.json", "a.js"],                "node")
    check("go.mod",                 ["go.mod", "main.go"],                   "go")
    check("rust Cargo.toml",        ["Cargo.toml", "src/main.rs"],           "rust")
    check("ruby bare .rb",          ["app.rb"],                              "ruby")
    check("php composer",           ["composer.json", "index.php"],          "php")
    # mixed / precedence
    check("py project + Makefile",  ["pyproject.toml", "Makefile", "a.py"],  "python")
    check("bare cpp-dominant",      ["a.cpp", "b.cpp", "c.cpp", "one.py"],   "cpp")
    check("empty dir",              [],                                      "")
    print("=" * 66)
    if _fails:
        print(f"RESULT: {_fails} FAILURE(S)")
        sys.exit(1)
    print("RESULT: all language-detection tests passed")
    sys.exit(0)
