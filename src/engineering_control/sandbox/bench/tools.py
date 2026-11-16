"""The tools of the workbench: a map of the code, finding the files a piece of work is
about, and reading them.

This file runs inside the sandbox, with nothing but Python's own library and git. It is
deterministic: no model takes part, and the same repository and the same question give
the same answer.

The map (``index``) is read from the code itself. For every tracked file it holds what
the file defines and which other files of the repository it imports; from that follow
who imports a file and which tests cover it. Python is read with Python's own parser.
JavaScript, TypeScript and Go are read by their import and declaration lines, which is
exact for imports and names and knows nothing of what a function calls. The map is kept
on the run's volume and only the files that changed are read again.

``find`` scores files against the words of a question: a word in a file's path or in a
name it defines counts most, a word in its text less, and a word that is everywhere
counts for nothing. The best files are joined by their neighbours on the map: what they
import, what imports them, and the tests that cover them. Every file comes with why.

``read`` hands back files as text, whole or up to a number of lines, and says which
were cut and which were left out. A file that looks like it holds a secret, and a file
outside the repository, is never read.

``apply`` carries out edits that were written elsewhere: replace a text that is in a
file exactly once, write a file whole, delete a file. All of them are applied or none
is, and an edit that does not fit says why in words the one who wrote it can act on.
"""

from __future__ import annotations

import ast
import json
import math
import os
import re
import shlex
import subprocess
import sys
import time
from typing import Any

REPO = os.environ.get("BENCH_REPO", "/workspace/repo")
INDEX = os.environ.get("BENCH_INDEX", "/workspace/.cache/code-index.json")
HISTORY = os.environ.get("BENCH_HISTORY", "/workspace/.cache/history.txt")
VERSION = 2
# A commit that changed more files than this says nothing of what belongs together.
MAX_TOGETHER = 30
MAX_PARSED = 400_000
MAX_DEFS = 200
SKIPPED_DIRS = ("node_modules/", "vendor/", "dist/", "build/", ".git/", "third_party/")
LANGUAGES = {
    ".py": "python",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".go": "go",
}
JS_ENDINGS = (".ts", ".tsx", ".mts", ".js", ".jsx", ".mjs", ".cjs", ".cts", ".json")
JS_IMPORT = re.compile(
    r"""(?:import|export)\s[^'"`;]*?from\s*['"]([^'"\n]+)['"]"""
    r"""|import\s*['"]([^'"\n]+)['"]"""
    r"""|(?:require|import)\s*\(\s*['"]([^'"\n]+)['"]\s*\)"""
)
JS_DEF = re.compile(
    r"^[ \t]*(?:export\s+)?(?:default\s+)?(?:declare\s+)?(?:abstract\s+)?(?:async\s+)?"
    r"(?:function\*?|class|const|let|var|interface|type|enum)\s+([A-Za-z_$][\w$]*)",
    re.M,
)
GO_IMPORTS = re.compile(r"^import\s*\(([^)]*)\)", re.M | re.S)
GO_IMPORT = re.compile(r'^import\s+(?:[\w.]+\s+)?"([^"]+)"', re.M)
GO_DEF = re.compile(r"^func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)|^type\s+([A-Za-z_]\w*)", re.M)
TEST_NAME = re.compile(r"(^test_.*\.py$)|(_test\.(py|go)$)|(\.(test|spec)\.[a-z]+$)")
TEST_DIRS = {"tests", "test", "__tests__", "e2e", "spec", "specs"}
SECRET_NAME = re.compile(
    r"(^|/)(\.env(\..*)?|.*\.pem|.*\.key|.*\.p12|.*\.pfx|id_[rd]sa|id_ed25519|"
    r"\.npmrc|\.netrc|\.pypirc|credentials(\.json)?|secrets?\.(json|ya?ml|toml))$",
    re.I,
)
STOP = frozenset(
    """the and for with that this from have has not are was were will can could should
    would when then than into out over under about after before where which while what
    who how why does did done use used using make made get set add new all any some one
    two also only more most very just but its their there here our your you they them
    been being each other such per via onto upon between within without through issue bug fix
    fixed fails failing failed error errors expected actual steps please need needs
    want wants like see must may might instead still already again page file files code
    test tests true false null none undefined return function class const let var""".split()
)


def git(*args: str) -> str:
    done = subprocess.run(  # noqa: S603 - git, with arguments this file wrote
        ["git", *args],  # noqa: S607
        cwd=REPO,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return done.stdout.decode("utf-8", errors="replace")


# --- the map ---------------------------------------------------------------------------------


def tracked() -> dict[str, str]:
    """Every tracked file with the name git has for its content. Links and other
    repositories inside this one are left out."""
    found: dict[str, str] = {}
    for entry in git("ls-files", "-s", "-z").split("\0"):
        head, _, path = entry.partition("\t")
        parts = head.split()
        if len(parts) < 2 or not path or parts[0] in ("120000", "160000"):
            continue
        found[path] = parts[1]
    return found


def is_test(path: str) -> bool:
    parts = path.split("/")
    return bool(TEST_NAME.search(parts[-1])) or bool(TEST_DIRS & set(parts[:-1]))


def _python(text: str) -> tuple[list[str], list[str]]:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return [], []
    defs: list[str] = []
    raw: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            defs.append(node.name)
        elif isinstance(node, ast.ClassDef):
            defs.append(node.name)
            defs += [
                inner.name
                for inner in node.body
                if isinstance(inner, ast.FunctionDef | ast.AsyncFunctionDef)
                and not inner.name.startswith("__")
            ]
    for part in ast.walk(tree):
        if isinstance(part, ast.Import):
            raw += [alias.name for alias in part.names]
        elif isinstance(part, ast.ImportFrom):
            base = "." * part.level + (part.module or "")
            raw.append(base)
            # ``from package import module`` names a file as often as a name in one.
            joint = base if base.endswith(".") or not base else base + "."
            raw += [joint + alias.name for alias in part.names if alias.name != "*"]
    return defs, raw


def _script(text: str) -> tuple[list[str], list[str]]:
    raw = [next(part for part in found if part) for found in JS_IMPORT.findall(text)]
    return JS_DEF.findall(text), raw


def _go(text: str) -> tuple[list[str], list[str]]:
    raw = GO_IMPORT.findall(text)
    for block in GO_IMPORTS.findall(text):
        raw += re.findall(r'"([^"]+)"', block)
    return [first or second for first, second in GO_DEF.findall(text)], raw


def facts(path: str, language: str) -> dict[str, Any]:
    """What one file defines and what it names as imported, as it says so itself."""
    try:
        with open(os.path.join(REPO, path), "rb") as handle:
            data = handle.read(MAX_PARSED + 1)
    except OSError:
        return {"lines": 0, "defs": [], "raw": []}
    if b"\0" in data[:8000]:
        return {"lines": 0, "defs": [], "raw": [], "binary": True}
    text = data[:MAX_PARSED].decode("utf-8", errors="replace")
    reader = {"python": _python, "go": _go}.get(language, _script)
    defs, raw = reader(text) if language and len(data) <= MAX_PARSED else ([], [])
    unique_defs = list(dict.fromkeys(defs))[:MAX_DEFS]
    return {"lines": text.count("\n") + 1, "defs": unique_defs, "raw": list(dict.fromkeys(raw))}


class Resolver:
    """Which file of the repository an import names."""

    def __init__(self, files: dict[str, dict[str, Any]]) -> None:
        self.files = files
        self.modules: dict[str, list[str]] = {}
        self.packages: dict[str, tuple[str, dict[str, Any]]] = {}
        self.go_modules: dict[str, str] = {}
        self.go_dirs: dict[str, list[str]] = {}
        for path, entry in files.items():
            if entry["lang"] == "python":
                parts = path[:-3].split("/")
                if parts[-1] == "__init__":
                    parts = parts[:-1]
                for start in range(len(parts)):
                    self.modules.setdefault(".".join(parts[start:]), []).append(path)
            elif entry["lang"] == "go" and not path.endswith("_test.go"):
                self.go_dirs.setdefault(os.path.dirname(path), []).append(path)
            name = os.path.basename(path)
            if name == "package.json":
                manifest = _json(path)
                if isinstance(manifest.get("name"), str):
                    self.packages[manifest["name"]] = (os.path.dirname(path), manifest)
            elif name == "go.mod":
                found = re.search(r"^module\s+(\S+)", _text(path), re.M)
                if found:
                    self.go_modules[found.group(1)] = os.path.dirname(path)

    def _first(self, candidates: list[str]) -> str | None:
        for candidate in candidates:
            normal = os.path.normpath(candidate)
            if normal in self.files:
                return normal
        return None

    def _script_file(self, base: str) -> str | None:
        stem, ending = os.path.splitext(base)
        swapped = [stem + other for other in JS_ENDINGS] if ending in JS_ENDINGS else []
        return self._first(
            [
                base,
                *(base + ending for ending in JS_ENDINGS),
                *swapped,
                *(os.path.join(base, "index" + ending) for ending in JS_ENDINGS),
            ]
        )

    def script(self, path: str, spec: str) -> str | None:
        if spec.startswith("."):
            return self._script_file(os.path.join(os.path.dirname(path), spec))
        name = max(
            (n for n in self.packages if spec == n or spec.startswith(n + "/")), key=len, default=""
        )
        if not name:
            return None
        folder, manifest = self.packages[name]
        rest = spec[len(name) :].lstrip("/")
        if rest:
            for root in (folder, os.path.join(folder, "src")):
                found = self._script_file(os.path.join(root, rest))
                if found:
                    return found
            return None
        entries = [manifest.get(key) for key in ("source", "module", "main", "types")]
        exports = manifest.get("exports")
        dot = exports.get(".") if isinstance(exports, dict) else exports
        entries += list(dot.values()) if isinstance(dot, dict) else [dot]
        for entry in [e for e in entries if isinstance(e, str)]:
            for candidate in (entry, re.sub(r"(^|/)(dist|build|lib)/", r"\1src/", entry)):
                found = self._script_file(os.path.join(folder, candidate))
                if found:
                    return found
        return self._script_file(os.path.join(folder, "src", "index")) or self._script_file(
            os.path.join(folder, "index")
        )

    def python(self, path: str, spec: str) -> str | None:
        if spec.startswith("."):
            level = len(spec) - len(spec.lstrip("."))
            folder = os.path.dirname(path)
            for _up in range(level - 1):
                folder = os.path.dirname(folder)
            target = (
                os.path.join(folder, *spec.lstrip(".").split(".")) if spec.strip(".") else folder
            )
            return self._first([target + ".py", os.path.join(target, "__init__.py")])
        options = [option for option in self.modules.get(spec, []) if option != path]
        if not options:
            return None
        here = path.split("/")

        def shared(option: str) -> int:
            count = 0
            for mine, theirs in zip(here, option.split("/"), strict=False):
                if mine != theirs:
                    break
                count += 1
            return count

        best = max(options, key=lambda option: (shared(option), -len(option)))
        # A single word that matches a file somewhere else is a library, not that file.
        return best if "." in spec or shared(best) > 0 else None

    def go(self, spec: str) -> list[str]:
        module = max(
            (m for m in self.go_modules if spec == m or spec.startswith(m + "/")),
            key=len,
            default="",
        )
        if not module:
            return []
        folder = os.path.normpath(
            os.path.join(self.go_modules[module], spec[len(module) :].lstrip("/"))
        )
        return self.go_dirs.get("" if folder == "." else folder, [])[:30]

    def resolve(self, path: str, entry: dict[str, Any]) -> list[str]:
        found: list[str] = []
        for spec in entry.get("raw", []):
            if entry["lang"] == "python":
                target = self.python(path, spec)
                found += [target] if target else []
            elif entry["lang"] == "go":
                found += self.go(spec)
            elif entry["lang"]:
                target = self.script(path, spec)
                found += [target] if target else []
        return [target for target in dict.fromkeys(found) if target != path]


def _text(path: str) -> str:
    try:
        with open(os.path.join(REPO, path), encoding="utf-8", errors="replace") as handle:
            return handle.read(MAX_PARSED)
    except OSError:
        return ""


def _json(path: str) -> dict[str, Any]:
    try:
        found = json.loads(_text(path))
    except ValueError:
        return {}
    return found if isinstance(found, dict) else {}


def index() -> dict[str, Any]:
    """The map of the repository as it stands, reading again only what changed."""
    started = time.monotonic()
    try:
        with open(INDEX, encoding="utf-8") as handle:
            old = json.load(handle)
        before = old["files"] if old.get("version") == VERSION else {}
    except (OSError, ValueError, KeyError):
        before = {}
    files: dict[str, dict[str, Any]] = {}
    reused = 0
    for path, blob in tracked().items():
        if path.startswith(SKIPPED_DIRS) or any(f"/{part}" in f"/{path}" for part in SKIPPED_DIRS):
            continue
        language = LANGUAGES.get(os.path.splitext(path)[1].lower(), "")
        known = before.get(path)
        if isinstance(known, dict) and known.get("hash") == blob:
            entry = {key: known[key] for key in ("lines", "defs", "raw") if key in known}
            reused += 1
        else:
            entry = facts(path, language)
        files[path] = {**entry, "hash": blob, "lang": language, "test": is_test(path)}
    resolver = Resolver(files)
    edges = 0
    for path, entry in files.items():
        entry["imports"] = resolver.resolve(path, entry)
        edges += len(entry["imports"])
    document = {
        "version": VERSION,
        "files": files,
        "summary": {
            "files": len(files),
            "code": sum(1 for entry in files.values() if entry["lang"]),
            "tests": sum(1 for entry in files.values() if entry["test"] and entry["lang"]),
            "imports": edges,
            "reused": reused,
            "seconds": round(time.monotonic() - started, 2),
        },
    }
    os.makedirs(os.path.dirname(INDEX), exist_ok=True)
    staged = INDEX + ".new"
    with open(staged, "w", encoding="utf-8") as handle:
        json.dump(document, handle, separators=(",", ":"))
    os.replace(staged, INDEX)
    return document


# --- what changes together -------------------------------------------------------------------


def together(files: dict[str, Any]) -> tuple[dict[str, int], dict[str, dict[str, int]], int]:
    """From the names of what the last commits changed, as the clone left them: in how
    many commits each file changed, in how many each pair changed together, and how many
    commits were read. Without that record, nothing."""
    changed: dict[str, int] = {}
    pairs: dict[str, dict[str, int]] = {}
    try:
        with open(HISTORY, encoding="utf-8", errors="replace") as handle:
            blocks = handle.read().split("\x01")
    except OSError:
        return changed, pairs, 0
    commits = 0
    for block in blocks:
        lines = [line.strip() for line in block.splitlines()]
        if not lines or not lines[0]:
            continue
        commits += 1
        names = sorted({line for line in lines[1:] if line in files})
        for name in names:
            changed[name] = changed.get(name, 0) + 1
        if not 2 <= len(names) <= MAX_TOGETHER:
            continue
        for one in names:
            for other in names:
                if one != other:
                    row = pairs.setdefault(one, {})
                    row[other] = row.get(other, 0) + 1
    return changed, pairs, commits


# --- finding ---------------------------------------------------------------------------------


def terms_of(text: str, limit: int = 40) -> list[str]:
    """The words of a question worth looking for: names as they are written, and the
    words a name is made of."""
    found: list[str] = []
    for word in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", text):
        pieces = re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|[0-9]+", word.replace("_", " "))
        found.append(word)
        found += [piece for piece in pieces if len(piece) >= 3] if len(pieces) > 1 else []
    kept = dict.fromkeys(word.lower() for word in found)
    return [word for word in kept if word not in STOP and not word.isdigit()][:limit]


def named_paths(text: str, files: dict[str, Any]) -> list[str]:
    """Files the question names itself, by their path or by the end of it."""
    found: list[str] = []
    for token in re.findall(r"[\w@.-]+(?:/[\w@.-]+)+|[\w-]+\.[A-Za-z]{1,5}\b", text):
        token = token.strip("./")
        if token in files:
            found.append(token)
        elif "/" in token or "." in token:
            ends = [path for path in files if path.endswith("/" + token)]
            found += ends if len(ends) <= 3 else []
    return list(dict.fromkeys(found))


def find(question: dict[str, Any]) -> dict[str, Any]:
    document = index()
    files: dict[str, dict[str, Any]] = document["files"]
    text = str(question.get("text") or "")
    limit = max(1, min(int(question.get("limit") or 20), 80))
    code = {path for path, entry in files.items() if entry["lang"]}
    total = max(1, len(code))
    scores: dict[str, float] = {}
    why: dict[str, list[str]] = {}

    def add(path: str, amount: float, reason: str) -> None:
        scores[path] = scores.get(path, 0.0) + amount
        reasons = why.setdefault(path, [])
        if reason not in reasons and len(reasons) < 6:
            reasons.append(reason)

    for path in named_paths(text, files):
        add(path, 50.0, "the question names it")
    used: list[dict[str, Any]] = []
    for term in terms_of(text):
        counts: dict[str, int] = {}
        for line in git("grep", "-I", "-i", "-c", "-F", "-e", term, "--", ".").splitlines():
            where, _, number = line.rpartition(":")
            if where in files and number.isdigit():
                counts[where] = int(number)
        in_code = sum(1 for path in counts if path in code)
        # A word that is in a fifth of the code says nothing about where to look.
        common = in_code > max(40, total // 5)
        weight = math.log(1 + total / max(1, in_code))
        used.append({"term": term, "files": len(counts), "common": common})
        for path, entry in files.items():
            if not entry["lang"]:
                continue
            lower = path.lower()
            if term in lower:
                add(path, 2.0 * max(weight, 1.0), f"its path has {term}")
            names = [name.lower() for name in entry.get("defs", [])]
            if term in names:
                add(path, 3.0 * max(weight, 1.0), f"it defines {term}")
            elif not common and any(term in name for name in names):
                add(path, 1.5 * weight, f"a name in it has {term}")
        if common:
            continue
        for where, times in counts.items():
            if where in code:
                add(where, weight * min(times, 5) / 5, f"it mentions {term} ({times})")
    importers: dict[str, list[str]] = {}
    for path, entry in files.items():
        for target in entry.get("imports", []):
            importers.setdefault(target, []).append(path)
    # Tests are found through what they test: by themselves they rank below the code.
    ranked = sorted(scores, key=lambda p: (-scores[p] * (0.6 if files[p]["test"] else 1.0), p))
    seeds = ranked[: max(1, math.ceil(limit * 0.6))]
    chosen: list[str] = list(seeds)

    def join(path: str, reason: str) -> None:
        if path not in files:
            return
        if path not in chosen:
            if len(chosen) >= limit:
                return
            chosen.append(path)
        # How it hangs together with the best files is said also of a file that was
        # found by its own words.
        reasons = why.setdefault(path, [])
        if reason not in reasons:
            reasons.insert(0, reason)

    if question.get("neighbours", True):
        for seed in seeds:
            around = importers.get(seed, [])
            for test in sorted(
                (p for p in around if files[p]["test"]), key=lambda p: -scores.get(p, 0)
            )[:2]:
                join(test, f"it tests {seed}")
            mine = [p for p in files[seed].get("imports", []) if not files[p]["test"]]
            for target in sorted(mine, key=lambda p: -scores.get(p, 0))[:3]:
                join(target, f"{seed} imports it")
            users = sorted(
                (p for p in around if not files[p]["test"]), key=lambda p: -scores.get(p, 0)
            )
            for user in users[:2]:
                join(user, f"it imports {seed}")
        # What changed in the same commits as the best files, at least twice and in a
        # quarter of their commits: what the code does not say hangs together.
        changed, pairs, commits = together(files)
        for seed in seeds:
            seen = changed.get(seed, 0)
            partners = [
                (times, other)
                for other, times in pairs.get(seed, {}).items()
                if times >= 2 and times * 4 >= seen
            ]
            for times, other in sorted(partners, key=lambda pair: (-pair[0], pair[1]))[:3]:
                join(other, f"it changed together with {seed} in {times} of {seen} commits")
    else:
        commits = 0
    for path in ranked:
        if len(chosen) >= limit:
            break
        if path not in chosen:
            chosen.append(path)
    return {
        "files": [
            {
                "path": path,
                "score": round(scores.get(path, 0.0), 2),
                "lines": files[path].get("lines", 0),
                "test": files[path]["test"],
                "why": why.get(path, []),
            }
            for path in chosen
        ],
        "terms": used,
        "indexed": document["summary"],
        # How many commits said which files change together; none when the clone read none.
        "history": commits,
    }


# --- the tests of a change --------------------------------------------------------------------

PY_ROOTS = ("pyproject.toml", "pytest.ini", "setup.cfg", "tox.ini", "setup.py")
JS_TESTS = (".mjs", ".js", ".cjs", ".ts", ".tsx", ".mts", ".cts", ".jsx")
JS_CONFIGS = ("ts", "mts", "js", "mjs", "cjs")
TESTED = re.compile(r"^test_(.+)\.py$|^(.+)_test\.(?:py|go)$|^(.+)\.(?:test|spec)\.[a-z]+$")
SAID = 6000


def changed_since(base: str) -> list[str]:
    """What differs from the commit the run started at, committed or not, and what is new."""
    names = set(git("ls-files", "--others", "--exclude-standard").splitlines())
    if base:
        names |= set(git("diff", "--name-only", "--no-renames", base, "--").splitlines())
    return sorted(name for name in names if name)


def tested_name(path: str) -> str:
    """What a test file is named after: price for price.test.ts, test_price.py, price_test.go."""
    match = TESTED.match(os.path.basename(path))
    return next((group for group in match.groups() if group), "") if match else ""


def _family(language: str) -> str:
    """JavaScript and TypeScript test each other; every other language tests itself."""
    return "script" if language in ("javascript", "typescript") else language


def _near(one: str, other: str) -> int:
    """How many leading folders two paths share."""
    shared = 0
    for mine, theirs in zip(one.split("/")[:-1], other.split("/")[:-1], strict=False):
        if mine != theirs:
            break
        shared += 1
    return shared


def related_tests(changed: list[str], files: dict[str, Any], limit: int) -> list[dict[str, str]]:
    """The tests that cover what was changed, the surest first: a test that was changed
    itself, a test that imports a changed file, a test named after one, a Go test of its
    package, and a test that imports what imports it."""
    importers: dict[str, list[str]] = {}
    for path, entry in files.items():
        for target in entry.get("imports", []):
            importers.setdefault(target, []).append(path)
    tests = [path for path, entry in files.items() if entry["test"] and entry["lang"]]
    found: dict[str, str] = {}

    def add(path: str, why: str) -> None:
        if path in files and files[path]["test"] and files[path]["lang"] and path not in found:
            found[path] = why

    for path in changed:
        add(path, "it was changed")
    code = [path for path in changed if not (path in files and files[path]["test"])]
    for path in code:
        for user in sorted(importers.get(path, [])):
            add(user, f"it imports {path}")
    named: dict[str, list[str]] = {}
    for test in tests:
        name = tested_name(test)
        if name:
            named.setdefault(name, []).append(test)
    for path in code:
        stem = os.path.basename(path).split(".")[0]
        # A test in the language of the file, the nearest first: price.py is not tested
        # by a price_test.go elsewhere in the repository.
        language = _family(LANGUAGES.get(os.path.splitext(path)[1].lower(), ""))
        same = [test for test in named.get(stem, []) if _family(files[test]["lang"]) == language]
        for test in sorted(same, key=lambda t: (-_near(t, path), t))[:3]:
            add(test, f"it is named after {path}")
        if path.endswith(".go"):
            folder = os.path.dirname(path)
            for test in sorted(t for t in tests if os.path.dirname(t) == folder):
                add(test, f"it is in the package of {path}")
    for path in code:
        for user in sorted(importers.get(path, [])):
            if files[user]["test"]:
                continue
            for test in sorted(importers.get(user, [])):
                add(test, f"it imports {user}, which imports {path}")
    return [{"path": path, "why": why} for path, why in list(found.items())[:limit]]


def _above(folder: str) -> list[str]:
    """A folder of the repository and every folder above it, the repository's own last."""
    parts = [part for part in folder.split("/") if part]
    return ["/".join(parts[:count]) for count in range(len(parts), -1, -1)]


def _there(*parts: str) -> bool:
    return os.path.exists(os.path.join(REPO, *[part for part in parts if part]))


def _tool(name: str, folder: str) -> str | None:
    """A program a package manager installed, in the folder's own packages or above."""
    for where in _above(folder):
        if _there(where, "node_modules", ".bin", name):
            return (
                os.path.join(REPO, where, "node_modules", ".bin", name)
                if where
                else (os.path.join(REPO, "node_modules", ".bin", name))
            )
    return None


def runner_of(test: str) -> tuple[str, str, list[str], str] | str:
    """How one test file is run: the runner's name, the folder it runs in, the command
    before the files, and the file as the runner is given it. Or why it cannot be run."""
    folder = os.path.dirname(test)
    if test.endswith(".py"):
        root = next((w for w in _above(folder) if any(_there(w, n) for n in PY_ROOTS)), "")
        python = next(
            (
                os.path.join(REPO, where, ".venv", "bin", "python")
                if where
                else (os.path.join(REPO, ".venv", "bin", "python"))
                for where in dict.fromkeys([root, ""])
                if _there(where, ".venv", "bin", "python")
            ),
            "python3",
        )
        argv = [python, "-m", "pytest", "-q", "-p", "no:cacheprovider"]
        return "pytest", root, argv, os.path.relpath(test, root or ".")
    if test.endswith("_test.go"):
        module = next((w for w in _above(folder) if _there(w, "go.mod")), None)
        if module is None:
            return "there is no go.mod above it"
        package = os.path.relpath(folder or ".", module or ".")
        return "go test", module, ["go", "test"], "." if package == "." else f"./{package}"
    if test.endswith(JS_TESTS):
        for where in _above(folder):
            if not _there(where, "package.json"):
                continue
            manifest = _json(os.path.join(where, "package.json") if where else "package.json")
            wanted: set[str] = set()
            for part in ("dependencies", "devDependencies"):
                listed = manifest.get(part)
                if isinstance(listed, dict):
                    wanted |= set(listed)
            for name, before in (("vitest", ["run"]), ("jest", [])):
                configured = any(_there(where, f"{name}.config.{end}") for end in JS_CONFIGS)
                if name not in wanted and not configured:
                    continue
                program = _tool(name, where)
                if program is None:
                    return f"{name} is not installed for {where or 'the repository'}"
                return name, where, [program, *before], os.path.relpath(test, where or ".")
        if test.endswith((".mjs", ".js", ".cjs")):
            return "node --test", "", ["node", "--test"], test
        return "no package.json above it names vitest or jest"
    return "no runner is known for this kind of file"


def _run(argv: list[str], folder: str, seconds: float) -> dict[str, Any]:
    started = time.monotonic()
    timed_out = False
    try:
        process = subprocess.Popen(  # noqa: S603 - the repository's own test runner
            argv,
            cwd=os.path.join(REPO, folder) if folder else REPO,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as error:
        return {"exit_code": 127, "seconds": 0.0, "timed_out": False, "said": str(error)[:SAID]}
    try:
        output, _ = process.communicate(timeout=max(1.0, seconds))
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(process.pid, 9)
        except OSError:
            process.kill()
        output, _ = process.communicate()
    return {
        "exit_code": process.returncode,
        "seconds": round(time.monotonic() - started, 1),
        "timed_out": timed_out,
        "said": output.decode("utf-8", errors="replace")[-SAID:],
    }


def _own(commands: list[dict[str, Any]], found: list[str], deadline: float) -> list[dict[str, Any]]:
    """Run the related tests with the commands a flow names for them, in order, up to the
    first that fails. ``{files}`` in a command stands for the test files; a command in a
    folder is given the ones in that folder, and is left out when there are none."""
    ran: list[dict[str, Any]] = []
    for command in commands:
        folder = str(command.get("cwd") or "").strip("/")
        folder = "" if folder == "." else folder
        argv = [str(part) for part in command.get("argv") or []]
        wants = any("{files}" in part for part in argv)
        inside = [
            os.path.relpath(test, folder) if folder else test
            for test in found
            if not folder or test.startswith(folder + "/")
        ]
        if not argv or (wants and not inside):
            continue
        filled: list[str] = []
        for part in argv:
            if part == "{files}":
                filled += inside
            else:
                filled.append(part.replace("{files}", shlex.join(inside)))
        line = shlex.join(argv)
        group: dict[str, Any] = {
            "runner": "the flow's own command",
            "folder": folder,
            "files": len(inside) if wants else 0,
            "line": (f"[{folder}] {line}" if folder else line)[:600],
        }
        left = deadline - time.monotonic()
        if left < 5:
            ran.append({**group, "exit_code": None, "seconds": 0.0, "timed_out": True, "said": ""})
            break
        allowed = min(left, float(command.get("timeout") or left))
        ran.append({**group, **_run(filled, folder, allowed)})
        if ran[-1]["exit_code"] != 0:
            break
    return ran


def tests(question: dict[str, Any]) -> dict[str, Any]:
    """Find the tests that cover what was changed and run them: with the commands the
    flow names for it, or each kind by the repository's own runner."""
    files: dict[str, dict[str, Any]] = index()["files"]
    named = [str(path) for path in question.get("paths") or []]
    changed = named or changed_since(str(question.get("base") or ""))
    limit = max(1, min(int(question.get("limit") or 20), 60))
    related = related_tests(changed, files, limit)
    deadline = time.monotonic() + max(5, int(question.get("seconds") or 600))
    own = [command for command in question.get("commands") or [] if isinstance(command, dict)]
    if own:
        ran_own = _own(own, [entry["path"] for entry in related], deadline) if related else []
        return {
            "changed": changed[:200],
            "tests": related,
            "ran": ran_own,
            "unrunnable": [],
            "passed": all(run["exit_code"] == 0 for run in ran_own) if ran_own else None,
        }
    groups: dict[tuple[str, str, tuple[str, ...]], list[str]] = {}
    unrunnable: list[dict[str, str]] = []
    for entry in related:
        how = runner_of(entry["path"])
        if isinstance(how, str):
            unrunnable.append({"path": entry["path"], "why": how})
            continue
        runner, folder, argv, given = how
        listed = groups.setdefault((runner, folder, tuple(argv)), [])
        if given not in listed:
            listed.append(given)
    ran: list[dict[str, Any]] = []
    for (name, where, command), listed in groups.items():
        left = deadline - time.monotonic()
        line = " ".join([os.path.basename(command[0]), *command[1:], *listed])
        if where:
            line = f"[{where}] {line}"
        group: dict[str, Any] = {
            "runner": name,
            "folder": where,
            "files": len(listed),
            "line": line[:600],
        }
        if left < 5:
            ran.append({**group, "exit_code": None, "seconds": 0.0, "timed_out": True, "said": ""})
            continue
        ran.append({**group, **_run([*command, *listed], where, left)})
    return {
        "changed": changed[:200],
        "tests": related,
        "ran": ran,
        "unrunnable": unrunnable,
        "passed": all(run["exit_code"] == 0 for run in ran) if ran else None,
    }


# --- reading ---------------------------------------------------------------------------------


def read(question: dict[str, Any]) -> dict[str, Any]:
    lines = max(20, min(int(question.get("lines") or 600), 5000))
    room = max(1000, min(int(question.get("characters") or 200_000), 600_000))
    known = tracked()
    root = os.path.realpath(REPO)
    given: list[dict[str, Any]] = []
    left: list[dict[str, str]] = []
    for wanted in list(dict.fromkeys(str(path) for path in question.get("paths") or []))[:200]:
        path = os.path.normpath(wanted.strip().lstrip("/"))
        full = os.path.realpath(os.path.join(root, path))
        if (
            wanted.strip().startswith("/")
            or path.startswith("..")
            or not full.startswith(root + os.sep)
        ):
            left.append({"path": wanted, "why": "it is not in the repository"})
        elif SECRET_NAME.search(path):
            left.append({"path": wanted, "why": "its name is one a secret is kept under"})
        elif path not in known and not os.path.isfile(full):
            left.append({"path": wanted, "why": "there is no such file"})
        elif room <= 0:
            left.append(
                {"path": wanted, "why": "no room left: read fewer files, or fewer lines of each"}
            )
        else:
            try:
                with open(full, "rb") as handle:
                    data = handle.read(2_000_000)
            except OSError:
                left.append({"path": wanted, "why": "it cannot be read"})
                continue
            if b"\0" in data[:8000]:
                left.append({"path": wanted, "why": "it is not text"})
                continue
            every = data.decode("utf-8", errors="replace").splitlines()
            text = "\n".join(every[:lines])[:room]
            room -= len(text)
            shown = text.count("\n") + 1 if text else 0
            given.append(
                {
                    "path": path,
                    "text": text,
                    "lines": len(every),
                    "shown": shown,
                    # Said, never silent: a file that is not whole says how much is there.
                    "cut": shown < len(every),
                }
            )
    return {"files": given, "left_out": left}


# --- changing --------------------------------------------------------------------------------


def _syntax(path: str, text: str) -> str | None:
    """Why the file would no longer be what its name says it is, where that can be
    told without the project's own tools."""
    ending = os.path.splitext(path)[1].lower()
    if ending == ".py":
        try:
            compile(text, path, "exec", dont_inherit=True)
        except SyntaxError as error:
            return f"it would not be Python any more (line {error.lineno}: {error.msg})"
        except ValueError as error:
            return f"it would not be Python any more ({error})"
    elif ending == ".json":
        try:
            json.loads(text)
        except ValueError as error:
            return f"it would not be JSON any more ({error})"
    return None


def apply(question: dict[str, Any]) -> dict[str, Any]:
    """Apply edits to the checkout: all of them, or none.

    An edit replaces a text that is in a file exactly once, writes a file whole, or
    deletes one. Every edit is tried on what the edits before it left; only when all
    of them fit, and every file is still what its name says, is anything written.
    """
    edits = [edit for edit in question.get("edits") or [] if isinstance(edit, dict)]
    protected = tuple(str(name) for name in question.get("protected") or [])
    root = os.path.realpath(REPO)
    staged: dict[str, str | None] = {}
    refused: list[dict[str, Any]] = []

    def current(path: str) -> str | None:
        if path in staged:
            return staged[path]
        try:
            with open(os.path.join(root, path), "rb") as handle:
                data = handle.read(2_000_001)
        except OSError:
            return None
        if len(data) > 2_000_000 or b"\0" in data[:8000]:
            raise ValueError("it is not a text file that can be edited here")
        return data.decode("utf-8", errors="surrogateescape")

    for number, edit in enumerate(edits[:400], start=1):
        wanted = str(edit.get("path") or "")
        action = str(edit.get("action") or "")
        path = os.path.normpath(wanted.strip().lstrip("/")) if wanted.strip() else ""
        find, replace = str(edit.get("find") or ""), str(edit.get("replace") or "")

        def refuse(
            why: str, number: int = number, wanted: str = wanted, action: str = action
        ) -> None:
            refused.append({"edit": number, "path": wanted, "action": action, "why": why})

        full = os.path.realpath(os.path.join(root, path)) if path else ""
        outside = wanted.strip().startswith("/") or path.startswith("..")
        if not path or outside or not full.startswith(root + os.sep):
            refuse("it is not a file of the repository")
            continue
        if path == ".git" or path.startswith(".git/") or path.startswith(".run/"):
            refuse("it is not a file of the repository's own")
            continue
        if any(
            path == name or (name.endswith("/") and path.startswith(name)) for name in protected
        ):
            refuse("it is a file that may not be changed here")
            continue
        if SECRET_NAME.search(path):
            refuse("its name is one a secret is kept under")
            continue
        if os.path.islink(os.path.join(root, path)) or os.path.isdir(full):
            refuse("it is not a plain file")
            continue
        try:
            before = current(path)
        except ValueError as error:
            refuse(str(error))
            continue
        if action == "write":
            staged[path] = replace
        elif action == "delete":
            if before is None:
                refuse("there is no such file to delete")
            else:
                staged[path] = None
        elif action == "replace":
            if before is None:
                refuse("there is no such file: write it whole instead")
            elif not find:
                refuse("it does not say which text to replace")
            elif before.count(find) == 1:
                staged[path] = before.replace(find, replace, 1)
            elif before.count(find) > 1:
                refuse(
                    f"the text to replace is in the file {before.count(find)} times: give "
                    "more of the lines around it, so that it is there once"
                )
            elif " ".join(find.split()) in " ".join(before.split()):
                refuse(
                    "the text to replace is in the file with other spacing or line breaks: "
                    "copy it exactly as the file has it"
                )
            else:
                refuse("the text to replace is not in the file as it is now")
        else:
            refuse("it says neither replace, write nor delete")
    for path, text in staged.items():
        wrong = _syntax(path, text) if text is not None else None
        if wrong:
            refused.append({"edit": 0, "path": path, "action": "", "why": wrong})
    if refused or not staged:
        # Nothing is written unless everything fits.
        return {"applied": [], "refused": refused, "edits": len(edits)}
    applied: list[dict[str, str]] = []
    now: list[dict[str, Any]] = []
    room = 300_000
    for path, text in staged.items():
        full = os.path.join(root, path)
        if text is None:
            os.remove(full)
            applied.append({"path": path, "action": "deleted"})
            continue
        existed = os.path.exists(full)
        os.makedirs(os.path.dirname(full) or root, exist_ok=True)
        with open(full, "wb") as handle:
            handle.write(text.encode("utf-8", errors="surrogateescape"))
        applied.append({"path": path, "action": "changed" if existed else "added"})
        # What the file holds now, for whoever has it in hand: what was read before the
        # change is no longer what is there.
        every = text.encode("utf-8", errors="replace").decode("utf-8").splitlines()
        shown = "\n".join(every)[: max(0, room)]
        room -= len(shown)
        kept = shown.count("\n") + 1 if shown else 0
        now.append(
            {
                "path": path,
                "text": shown,
                "lines": len(every),
                "shown": kept,
                "cut": kept < len(every),
            }
        )
    return {"applied": applied, "refused": [], "edits": len(edits), "files": now}


def main(arguments: list[str]) -> int:
    if len(arguments) != 3 or arguments[0] not in ("find", "read", "index", "apply", "tests"):
        print(
            "usage: tools.py find|read|index|apply|tests <question.json> <answer.json>",
            file=sys.stderr,
        )
        return 2
    with open(arguments[1], encoding="utf-8") as handle:
        question = json.load(handle)
    if arguments[0] == "find":
        answer = find(question)
    elif arguments[0] == "read":
        answer = read(question)
    elif arguments[0] == "apply":
        answer = apply(question)
    elif arguments[0] == "tests":
        answer = tests(question)
    else:
        answer = {"indexed": index()["summary"]}
    os.makedirs(os.path.dirname(arguments[2]), exist_ok=True)
    with open(arguments[2], "w", encoding="utf-8") as handle:
        json.dump(answer, handle, separators=(",", ":"))
    said = {key: value for key, value in answer.items() if key not in ("files", "ran")}
    print(json.dumps({**said, "files": len(answer.get("files", []))})[:4000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
