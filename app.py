"""
TraceBox - Visual Code-to-Logic & Memory Execution Sandbox
==========================================================

Flask backend with three defensive layers around user-submitted Python:

1. Static AST guard      - rejects dangerous syntax before anything runs.
2. Restricted builtins   - the code only sees a tiny whitelist of functions.
3. Resource limits       - 150-step quota, wall-clock timeout, CPU/memory
                           limits, all inside a throw-away child process.

Execution is recorded line-by-line with sys.settrace and returned to the
browser as JSON "frames" that the front end can step through.
"""

import ast
import io
import multiprocessing
import os
import queue
import sys
import traceback
import types

from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
MAX_STEPS = 150            # maximum recorded frames per run
MAX_CODE_LENGTH = 3000     # maximum characters accepted from the client
MAX_OUTPUT_CHARS = 2000    # maximum captured stdout
MAX_REPR_CHARS = 80        # truncation length for a value preview
MAX_COLLECTION_ITEMS = 20  # list/dict items sent to the browser
TIMEOUT_SECONDS = 3        # wall-clock limit for one run
MEMORY_HEADROOM_MB = 256   # extra memory the child may allocate (Linux only)
USER_FILENAME = "<tracebox>"

FORBIDDEN_CALLS = {
    "open", "eval", "exec", "compile", "__import__",
    "input", "breakpoint", "exit", "quit", "getattr",
    "setattr", "delattr", "globals", "locals", "vars",
    "dir", "help", "memoryview", "bytearray",
}

# Attribute names / prefixes that give access to interpreter internals
# (frames, generators, code objects) and could be used to escape.
FORBIDDEN_ATTRS = {"mro", "format_map"}
FORBIDDEN_ATTR_PREFIXES = ("__", "gi_", "cr_", "ag_", "tb_", "f_", "co_", "func_")

BLOCKED_NODES = {
    ast.Import: "import statements are not allowed in TraceBox",
    ast.ImportFrom: "import statements are not allowed in TraceBox",
    ast.ClassDef: "classes are not supported in TraceBox (use functions)",
    ast.AsyncFunctionDef: "async code is not supported",
    ast.AsyncFor: "async code is not supported",
    ast.AsyncWith: "async code is not supported",
    ast.Await: "async code is not supported",
}

SAFE_BUILTINS = {
    "print": print, "range": range, "len": len,
    "int": int, "float": float, "str": str, "bool": bool,
    "list": list, "dict": dict, "set": set, "tuple": tuple,
    "abs": abs, "min": min, "max": max, "sum": sum, "round": round,
    "sorted": sorted, "reversed": reversed, "enumerate": enumerate,
    "zip": zip, "map": map, "filter": filter, "any": any, "all": all,
    "pow": pow, "divmod": divmod, "chr": chr, "ord": ord,
    "isinstance": isinstance, "type": type,
    "Exception": Exception, "ValueError": ValueError,
    "TypeError": TypeError, "IndexError": IndexError,
    "KeyError": KeyError, "ZeroDivisionError": ZeroDivisionError,
    "True": True, "False": False, "None": None,
}


# --------------------------------------------------------------------------
# Layer 1 - Static AST security guard
# --------------------------------------------------------------------------
class SecurityError(Exception):
    """Raised when submitted code contains a forbidden construct."""

    def __init__(self, message, line=None):
        super().__init__(message)
        self.message = message
        self.line = line


def validate_ast(source_code: str) -> None:
    """Parse the code and walk every node; raise before anything executes."""
    tree = ast.parse(source_code, filename=USER_FILENAME)

    for node in ast.walk(tree):
        line = getattr(node, "lineno", None)

        for node_type, message in BLOCKED_NODES.items():
            if isinstance(node, node_type):
                raise SecurityError(message, line)

        if isinstance(node, ast.Name):
            if node.id in FORBIDDEN_CALLS:
                raise SecurityError(f"use of '{node.id}' is not allowed", line)
            if node.id.startswith("__"):
                raise SecurityError("names starting with '__' are not allowed", line)

        elif isinstance(node, ast.Attribute):
            attr = node.attr
            if attr in FORBIDDEN_ATTRS or attr.startswith(FORBIDDEN_ATTR_PREFIXES):
                raise SecurityError(f"access to attribute '{attr}' is not allowed", line)

        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            # Blocks "{0.__class__}".format(x) style escapes.
            if "__" in node.value:
                raise SecurityError("strings containing '__' are not allowed", line)

        elif isinstance(node, ast.ExceptHandler) and node.type is None:
            raise SecurityError("bare 'except:' is not allowed; use 'except Exception:'", line)


# --------------------------------------------------------------------------
# Layer 2 + 3 - Tracing engine (runs inside a child process)
# --------------------------------------------------------------------------
class StepLimitReached(BaseException):
    """BaseException so user code with 'except Exception' cannot swallow it."""


class LimitedBuffer(io.StringIO):
    """stdout replacement that silently stops growing past a limit."""

    def __init__(self, limit):
        super().__init__()
        self.limit = limit

    def write(self, text):
        room = self.limit - self.tell()
        if room > 0:
            return super().write(text[:room])
        return len(text)


def short(value, limit=MAX_REPR_CHARS):
    try:
        text = repr(value)
    except Exception:
        text = "<unrepresentable>"
    return text if len(text) <= limit else text[: limit - 1] + "…"


def describe(value):
    """Turn a Python value into a small JSON-safe structure."""
    if isinstance(value, types.FunctionType):
        return {"type": "function", "repr": f"def {value.__name__}(...)"}

    info = {"type": type(value).__name__, "repr": short(value)}
    if isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
        info["items"] = [short(x, 30) for x in items[:MAX_COLLECTION_ITEMS]]
        info["size"] = len(items)
    elif isinstance(value, dict):
        entries = list(value.items())
        info["entries"] = [[short(k, 30), short(v, 30)] for k, v in entries[:MAX_COLLECTION_ITEMS]]
        info["size"] = len(entries)
    return info


def execute_trace(code: str) -> dict:
    """Run `code` under sys.settrace and return the recorded frames."""
    steps = []
    out = LimitedBuffer(MAX_OUTPUT_CHARS)

    def call_depth(frame):
        depth = 0
        while frame is not None:
            if frame.f_code.co_filename == USER_FILENAME:
                depth += 1
            frame = frame.f_back
        return depth

    def record(frame, event, return_value=None):
        if len(steps) >= MAX_STEPS:
            raise StepLimitReached()
        is_module = frame.f_code.co_name == "<module>"
        variables = {
            name: describe(value)
            for name, value in list(frame.f_locals.items())
            if not name.startswith("__")
        }
        step = {
            "index": len(steps),
            "line": frame.f_lineno,
            "event": event,
            "func": "global" if is_module else frame.f_code.co_name,
            "depth": call_depth(frame),
            "variables": variables,
            "stdout": out.getvalue(),
        }
        if event == "return":
            step["return_value"] = describe(return_value)
        steps.append(step)

    def tracer(frame, event, arg):
        if frame.f_code.co_filename != USER_FILENAME:
            return None
        name = frame.f_code.co_name
        if name.startswith("<") and name != "<module>":
            return None  # skip lambdas / comprehensions
        if event == "line":
            record(frame, "line")
        elif event == "return":
            record(frame, "end" if name == "<module>" else "return", arg)
        return tracer

    env = {"__builtins__": SAFE_BUILTINS, "__name__": "__main__"}
    error = None
    truncated = False
    real_stdout = sys.stdout
    sys.stdout = out
    sys.settrace(tracer)
    try:
        exec(compile(code, USER_FILENAME, "exec"), env)
    except StepLimitReached:
        truncated = True
    except BaseException as exc:  # includes SystemExit, RecursionError, ...
        line = None
        for frame_summary in traceback.extract_tb(exc.__traceback__):
            if frame_summary.filename == USER_FILENAME:
                line = frame_summary.lineno
        error = {"type": type(exc).__name__, "message": str(exc), "line": line}
    finally:
        sys.settrace(None)
        sys.stdout = real_stdout

    return {
        "ok": True,
        "steps": steps,
        "truncated": truncated,
        "error": error,
        "message": (
            f"Execution stopped after {MAX_STEPS} steps (possible infinite loop)."
            if truncated else None
        ),
    }


def _apply_limits():
    """Best-effort CPU and memory caps for the child process (Linux only)."""
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_CPU, (TIMEOUT_SECONDS + 2, TIMEOUT_SECONDS + 2))
        with open("/proc/self/statm") as fh:
            pages = int(fh.read().split()[0])
        current = pages * resource.getpagesize()
        limit = current + MEMORY_HEADROOM_MB * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    except Exception:
        pass


def _worker(code, result_queue):
    _apply_limits()
    try:
        result_queue.put(execute_trace(code))
    except BaseException as exc:  # last-resort safety net
        result_queue.put({
            "ok": False, "steps": [], "truncated": False,
            "error": {"type": type(exc).__name__, "message": str(exc), "line": None},
            "message": None,
        })


def trace_in_sandbox(code: str) -> dict:
    """Run execute_trace in a child process that is killed on timeout."""
    methods = multiprocessing.get_all_start_methods()
    ctx = multiprocessing.get_context("fork" if "fork" in methods else "spawn")
    result_queue = ctx.Queue()
    proc = ctx.Process(target=_worker, args=(code, result_queue), daemon=True)
    proc.start()
    try:
        return result_queue.get(timeout=TIMEOUT_SECONDS)
    except queue.Empty:
        return {
            "ok": False, "steps": [], "truncated": False, "message": None,
            "error": {
                "type": "TimeoutError",
                "message": f"Program ran longer than {TIMEOUT_SECONDS} seconds and was stopped.",
                "line": None,
            },
        }
    finally:
        if proc.is_alive():
            proc.kill()
        proc.join(1)


# --------------------------------------------------------------------------
# HTTP routes
# --------------------------------------------------------------------------
def error_response(kind, message, line=None, status=400):
    body = {
        "ok": False, "steps": [], "truncated": False, "message": None,
        "error": {"type": kind, "message": message, "line": line},
    }
    return jsonify(body), status


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/trace", methods=["POST"])
def trace():
    payload = request.get_json(silent=True) or {}
    code = payload.get("code")

    if not isinstance(code, str) or not code.strip():
        return error_response("ValueError", "Please enter some Python code.")
    if len(code) > MAX_CODE_LENGTH:
        return error_response(
            "ValueError", f"Code is too long (limit {MAX_CODE_LENGTH} characters)."
        )

    try:
        validate_ast(code)
    except SyntaxError as exc:
        return error_response("SyntaxError", exc.msg, exc.lineno)
    except SecurityError as exc:
        return error_response("SecurityError", exc.message, exc.line)

    return jsonify(trace_in_sandbox(code))


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=os.environ.get("TRACEBOX_DEBUG") == "1")
