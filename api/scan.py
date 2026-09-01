"""
Vercel serverless function backing the "Run a live scan" button.

No GitHub token required: public repos can be fetched over plain HTTPS via
codeload.github.com, the same mechanism `pip install git+https://...` uses.
Self-contained (stdlib only) since each Vercel Python function ships its own
dependencies independently.

This is a trimmed, sandboxed copy of the same gap-finding logic proven in
scanner/ast_analysis.py — kept in sync by hand for now; once this is stable,
worth extracting into a shared package both sides import.
"""
from __future__ import annotations

import ast
import io
import json
import re
import tarfile
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler

MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024  # 25MB — has to fit a serverless timeout
DOWNLOAD_TIMEOUT_SECONDS = 8
MAX_RESULTS = 8
MIN_COMPLEXITY = 2

GITHUB_REF_RE = re.compile(
    r"^(?:https?://github\.com/)?(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+?)(?:\.git)?/?$"
)
SKIP_DIRS = {"venv", ".venv", "env", "node_modules", "__pycache__", "build", "dist", "migrations", "vendor"}
TEST_FILE_MARKERS = ("test_", "_test", "tests", "conftest")


class ScanError(Exception):
    pass


def parse_ref(ref: str) -> tuple[str, str]:
    match = GITHUB_REF_RE.match(ref.strip())
    if not match:
        raise ScanError(f"'{ref}' doesn't look like a GitHub repo reference")
    return match.group("owner"), match.group("repo")


def fetch_tarball(owner: str, repo: str) -> bytes:
    for branch in ("main", "master"):
        url = f"https://codeload.github.com/{owner}/{repo}/tar.gz/refs/heads/{branch}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "prism-scan-demo"})
            with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT_SECONDS) as resp:
                data = resp.read(MAX_DOWNLOAD_BYTES + 1)
                if len(data) > MAX_DOWNLOAD_BYTES:
                    raise ScanError(f"{owner}/{repo} is too large for a live demo scan")
                return data
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                continue  # try the other default branch name
            raise ScanError(f"could not fetch {owner}/{repo}: HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise ScanError(f"could not reach GitHub: {exc.reason}") from exc
    raise ScanError(f"'{owner}/{repo}' not found (checked main and master branches)")


def iter_python_sources(tar_bytes: bytes):
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:gz") as tar:
        for member in tar.getmembers():
            if not member.isfile() or not member.name.endswith(".py"):
                continue
            parts = member.name.split("/")
            if any(p in SKIP_DIRS for p in parts):
                continue
            f = tar.extractfile(member)
            if f is None:
                continue
            try:
                source = f.read().decode("utf-8", errors="ignore")
            except Exception:
                continue
            # Strip the tarball's top-level "<repo>-<branch>/" prefix for display.
            display_path = "/".join(parts[1:]) if len(parts) > 1 else parts[0]
            yield display_path, source


def is_test_file(path: str) -> bool:
    name = path.rsplit("/", 1)[-1].lower()
    return any(m in name for m in TEST_FILE_MARKERS) or "tests" in path.lower().split("/")


def complexity_of(node: ast.AST) -> int:
    branch_types = (ast.If, ast.For, ast.While, ast.Try, ast.BoolOp, ast.With)
    return sum(1 for child in ast.walk(node) if isinstance(child, branch_types))


def is_trivial_body(node) -> bool:
    body = node.body
    if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant):
        body = body[1:]
    if not body:
        return True
    if len(body) == 1 and isinstance(body[0], (ast.Pass, ast.Return)):
        if isinstance(body[0], ast.Return) and body[0].value is not None:
            return not isinstance(body[0].value, ast.Constant)
        return True
    return False


def run_scan(ref: str) -> dict:
    owner, repo = parse_ref(ref)
    tar_bytes = fetch_tarball(owner, repo)

    sources = list(iter_python_sources(tar_bytes))
    test_sources = [(p, s) for p, s in sources if is_test_file(p)]
    main_sources = [(p, s) for p, s in sources if not is_test_file(p)]

    if not main_sources:
        return {"repo": f"{owner}/{repo}", "mode": "demo (read-only)", "gap_count": 0, "gaps": [],
                "note": "No Python source files found in this repo."}

    tested_names: set[str] = set()
    for _, source in test_sources:
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                tested_names.add(node.id)
            elif isinstance(node, ast.Attribute):
                tested_names.add(node.attr)

    gaps = []
    for path, source in main_sources:
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue

        stack: list[str] = []

        def visit(node, stack=stack, path=path):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if not child.name.startswith("_"):
                        cx = complexity_of(child)
                        if cx >= MIN_COMPLEXITY and not is_trivial_body(child) and child.name not in tested_names:
                            qualname = ".".join(stack + [child.name])
                            length = max(getattr(child, "end_lineno", child.lineno) - child.lineno, 1)
                            score = round(cx * 2 + (cx / length) * 10, 2)
                            gaps.append({
                                "function": qualname,
                                "file": path,
                                "lines": f"{child.lineno}-{getattr(child, 'end_lineno', child.lineno)}",
                                "complexity": cx,
                                "score": score,
                                "reason": f"'{qualname}' has real branching logic (complexity {cx}) "
                                          f"and no reference found in any test file",
                            })
                    stack.append(child.name)
                    visit(child, stack, path)
                    stack.pop()
                elif isinstance(child, ast.ClassDef):
                    stack.append(child.name)
                    visit(child, stack, path)
                    stack.pop()
                else:
                    visit(child, stack, path)

        visit(tree)

    gaps.sort(key=lambda g: g["score"], reverse=True)
    top = gaps[:MAX_RESULTS]

    return {"repo": f"{owner}/{repo}", "mode": "demo (read-only)", "gap_count": len(top), "gaps": top}


class handler(BaseHTTPRequestHandler):
    def _send_json(self, status: int, payload: dict):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            payload = json.loads(raw or b"{}")
            ref = (payload.get("repo") or "").strip()
            if not ref:
                self._send_json(400, {"error": "missing 'repo' field, e.g. {\"repo\": \"owner/repo\"}"})
                return
            result = run_scan(ref)
            self._send_json(200, result)
        except ScanError as exc:
            self._send_json(400, {"error": str(exc)})
        except Exception as exc:  # last-resort guard so the function never 500s silently
            self._send_json(500, {"error": f"unexpected error: {exc}"})
