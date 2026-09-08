"""
Vercel serverless function backing the "Run a live scan" button.

No GitHub token required: public repos can be fetched over plain HTTPS via
codeload.github.com, the same mechanism `pip install git+https://...` uses.

Python analysis is stdlib-only. JS/TS analysis needs `esprima` (see
api/requirements.txt) — the one non-stdlib dependency in this function,
added deliberately and for the first time to support JS/TS repos.

This is a trimmed, sandboxed copy of the same gap-finding logic proven in
scanner/ast_analysis.py and scanner/js_analysis.py — kept in sync by hand
for now; once this is stable, worth extracting into a shared package both
sides import.
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

import esprima

MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024  # 25MB — has to fit a serverless timeout
DOWNLOAD_TIMEOUT_SECONDS = 8
MAX_RESULTS = 8
MIN_COMPLEXITY = 2

# --- Rate limiting ---
# Vercel serverless functions don't share memory across instances, so this
# is best-effort, not bulletproof: it stops a single script hammering the
# endpoint from one warm instance, and caps worst-case cost per instance.
# A determined attacker spreading requests across many cold starts could
# still get through — closing that gap for real needs a shared store
# (e.g. Upstash Redis's free tier), which is the honest next step if this
# ever shows signs of actual abuse rather than normal demo traffic.
COOLDOWN_SECONDS = 15          # per-IP: one scan every 15s
MAX_REQUESTS_PER_WINDOW = 20   # per-instance: hard cap regardless of IP
WINDOW_SECONDS = 300           # 5-minute rolling window for the hard cap

_last_request_by_ip: dict[str, float] = {}
_request_timestamps: list[float] = []

GITHUB_REF_RE = re.compile(
    r"^(?:https?://github\.com/)?(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+?)(?:\.git)?/?$"
)
SKIP_DIRS = {"venv", ".venv", "env", "node_modules", "__pycache__", "build", "dist", "migrations", "vendor", ".next", "coverage"}
TEST_FILE_MARKERS = ("test_", "_test", "tests", "conftest")

JS_EXTENSIONS = (".js", ".jsx", ".ts", ".tsx")
JS_TEST_MARKERS = (".test.", ".spec.", "__tests__")
JS_BRANCH_TYPES = {
    "IfStatement", "ForStatement", "ForInStatement", "ForOfStatement",
    "WhileStatement", "DoWhileStatement", "TryStatement", "SwitchCase",
    "LogicalExpression",
}


class ScanError(Exception):
    pass


class RateLimitError(Exception):
    def __init__(self, message: str, retry_after: int):
        super().__init__(message)
        self.retry_after = retry_after


def check_rate_limit(ip: str, *, _now=None) -> None:
    """Raises RateLimitError if this request should be rejected. Called
    before any expensive work (download, parse) happens."""
    import time
    now = _now() if _now else time.time()

    # Per-IP cooldown
    last = _last_request_by_ip.get(ip)
    if last is not None and (now - last) < COOLDOWN_SECONDS:
        retry_after = int(COOLDOWN_SECONDS - (now - last)) + 1
        raise RateLimitError(f"Please wait {retry_after}s between scans.", retry_after)

    # Global hard cap per warm instance, rolling window
    global _request_timestamps
    _request_timestamps = [t for t in _request_timestamps if now - t < WINDOW_SECONDS]
    if len(_request_timestamps) >= MAX_REQUESTS_PER_WINDOW:
        raise RateLimitError(
            "This demo is getting heavy traffic right now — please try again shortly.",
            WINDOW_SECONDS,
        )

    _last_request_by_ip[ip] = now
    _request_timestamps.append(now)


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


def iter_sources(tar_bytes: bytes, extensions: tuple[str, ...]):
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:gz") as tar:
        for member in tar.getmembers():
            if not member.isfile() or not member.name.endswith(extensions):
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


def is_js_test_file(path: str) -> bool:
    name = path.rsplit("/", 1)[-1].lower()
    return any(m in name for m in JS_TEST_MARKERS) or "__tests__" in path.split("/")


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


# --- JS/TS analysis (mirrors scanner/js_analysis.py; see that file for the
# fuller honest-scope-limit explanation — real TypeScript syntax like
# interfaces/generics can't be parsed by esprima and is silently skipped,
# same "skip, don't crash" behavior as a Python SyntaxError) ---

def js_try_parse(source: str):
    for parser in (esprima.parseModule, esprima.parseScript):
        try:
            return parser(source, options={"loc": True}, tolerant=True)
        except Exception:
            continue
    return None


def js_walk_all(node):
    if node is None:
        return
    if isinstance(node, list):
        for item in node:
            yield from js_walk_all(item)
        return
    if not hasattr(node, "type"):
        return
    yield node
    for key, value in vars(node).items():
        if key in ("loc", "range"):
            continue
        yield from js_walk_all(value)


def js_complexity_of(node) -> int:
    return sum(1 for n in js_walk_all(node) if n.type in JS_BRANCH_TYPES)


def js_is_trivial_body(func_node) -> bool:
    if getattr(func_node, "expression", False):
        return js_complexity_of(func_node.body) == 0
    stmts = getattr(func_node.body, "body", [])
    if not stmts:
        return True
    if len(stmts) == 1 and stmts[0].type == "ReturnStatement":
        arg = getattr(stmts[0], "argument", None)
        if arg is None:
            return True
        return getattr(arg, "type", None) == "Literal"
    return False


def js_collect_test_identifiers(test_sources: list[str]) -> set[str]:
    names: set[str] = set()
    for source in test_sources:
        tree = js_try_parse(source)
        if tree is None:
            continue
        for node in js_walk_all(tree):
            if node.type == "Identifier":
                names.add(node.name)
            elif node.type == "Property":
                key = getattr(node, "key", None)
                if key is not None and getattr(key, "type", None) == "Identifier":
                    names.add(key.name)
    return names


def js_collect_functions(node, class_stack: list[str], results: list[dict], file_path: str):
    if node is None:
        return
    if isinstance(node, list):
        for item in node:
            js_collect_functions(item, class_stack, results, file_path)
        return
    if not hasattr(node, "type"):
        return

    t = node.type
    pushed = False

    def register(func_node, name):
        cx = js_complexity_of(func_node.body)
        if cx < MIN_COMPLEXITY or js_is_trivial_body(func_node):
            return
        qualname = ".".join(class_stack + [name])
        length = max(func_node.loc.end.line - func_node.loc.start.line, 1)
        score = round(cx * 2 + (cx / length) * 10, 2)
        results.append({
            "function": qualname,
            "file": file_path,
            "lines": f"{func_node.loc.start.line}-{func_node.loc.end.line}",
            "complexity": cx,
            "score": score,
            "reason": f"'{qualname}' has real branching logic (complexity {cx}) "
                      f"and no reference found in any test file",
            "language": "javascript",
            "_name": name,  # used for the tested-identifier check, stripped before response
        })

    if t == "FunctionDeclaration" and node.id and not node.id.name.startswith("_"):
        register(node, node.id.name)
    elif t == "VariableDeclarator":
        init = getattr(node, "init", None)
        idnode = getattr(node, "id", None)
        if (init is not None and getattr(init, "type", None) in ("FunctionExpression", "ArrowFunctionExpression")
                and idnode is not None and getattr(idnode, "type", None) == "Identifier"
                and not idnode.name.startswith("_")):
            register(init, idnode.name)
    elif t == "MethodDefinition":
        key = getattr(node, "key", None)
        name = getattr(key, "name", None) if key is not None else None
        value = getattr(node, "value", None)
        if (name and not name.startswith("_") and name != "constructor"
                and value is not None and getattr(value, "type", None) == "FunctionExpression"):
            register(value, name)
    elif t == "ClassDeclaration":
        cname = getattr(getattr(node, "id", None), "name", None)
        if cname:
            class_stack.append(cname)
            pushed = True

    for key, value in vars(node).items():
        if key in ("loc", "range"):
            continue
        js_collect_functions(value, class_stack, results, file_path)

    if pushed:
        class_stack.pop()


def find_js_gaps(js_sources: list[tuple[str, str]]) -> list[dict]:
    test_sources = [s for p, s in js_sources if is_js_test_file(p)]
    main_sources = [(p, s) for p, s in js_sources if not is_js_test_file(p)]
    tested_names = js_collect_test_identifiers(test_sources)

    all_gaps: list[dict] = []
    for path, source in main_sources:
        tree = js_try_parse(source)
        if tree is None:
            continue  # unparseable — likely real TS syntax; skip, don't crash
        results: list[dict] = []
        js_collect_functions(tree, [], results, path)
        for gap in results:
            if gap.pop("_name") not in tested_names:
                all_gaps.append(gap)
    return all_gaps


def find_python_gaps(py_sources: list[tuple[str, str]]) -> list[dict]:
    test_sources = [s for p, s in py_sources if is_test_file(p)]
    main_sources = [(p, s) for p, s in py_sources if not is_test_file(p)]

    tested_names: set[str] = set()
    for source in test_sources:
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
            continue  # e.g. Python 2 syntax, or a genuine syntax error — skip, don't crash

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
                                "language": "python",
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

    return gaps


def run_scan(ref: str) -> dict:
    owner, repo = parse_ref(ref)
    tar_bytes = fetch_tarball(owner, repo)

    py_sources = list(iter_sources(tar_bytes, (".py",)))
    js_sources = list(iter_sources(tar_bytes, JS_EXTENSIONS))

    if not py_sources and not js_sources:
        return {"repo": f"{owner}/{repo}", "mode": "demo (read-only)", "gap_count": 0, "gaps": [],
                "note": "No Python or JavaScript/TypeScript source files found in this repo."}

    gaps: list[dict] = []
    languages_scanned = []
    if py_sources:
        gaps.extend(find_python_gaps(py_sources))
        languages_scanned.append("python")
    if js_sources:
        gaps.extend(find_js_gaps(js_sources))
        languages_scanned.append("javascript/typescript")

    gaps.sort(key=lambda g: g["score"], reverse=True)
    top = gaps[:MAX_RESULTS]

    result = {"repo": f"{owner}/{repo}", "mode": "demo (read-only)", "gap_count": len(top), "gaps": top}
    if not top:
        result["note"] = (
            f"Scanned {' and '.join(languages_scanned)} source, found no untested gaps "
            f"above the complexity threshold — may mean it's already well-tested, or "
            f"(for TypeScript) uses type syntax this scanner can't parse yet."
        )
    return result


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
            client_ip = (
                self.headers.get("x-forwarded-for", "").split(",")[0].strip()
                or self.client_address[0]
            )
            check_rate_limit(client_ip)

            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            payload = json.loads(raw or b"{}")
            ref = (payload.get("repo") or "").strip()
            if not ref:
                self._send_json(400, {"error": "missing 'repo' field, e.g. {\"repo\": \"owner/repo\"}"})
                return
            result = run_scan(ref)
            self._send_json(200, result)
        except RateLimitError as exc:
            self._send_json(429, {"error": str(exc), "retry_after": exc.retry_after})
        except ScanError as exc:
            self._send_json(400, {"error": str(exc)})
        except Exception as exc:  # last-resort guard so the function never 500s silently
            self._send_json(500, {"error": f"unexpected error: {exc}"})
