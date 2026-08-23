"""Stdlib entrypoint executed inside the sandbox subprocess.

Invoked as ``python -m StAgent.code.runner``. The
parent process streams a payload on stdin describing the
script to run, the user-supplied globals, and limits. Two encodings:

* **V2 framed** (preferred): ``StAgent_RUNNER_V2`` line, JSON metadata line
  (all keys except ``source``), a decimal UTF-8 byte length line, then raw
  UTF-8 source bytes. Built by :func:`build_runner_stdin`.
* **Legacy**: a single JSON object (``source`` embedded as a JSON string).

This module:

1. Installs a SIGALRM timer as a last-resort hard stop.
2. ``chdir``'s into the provided workspace directory.
3. Executes the source with full builtins plus the requested globals.
4. Emits a single NDJSON-terminated response on stdout containing
   ``status``, ``stdout``, ``stderr``, ``result`` (if JSON-serialisable),
   ``produced_files`` (paths relative to the workspace), and timing.
"""

from __future__ import annotations

import io
import json
import math
import mimetypes
import os
import signal
import sys
import time
import traceback
from contextlib import redirect_stderr, redirect_stdout, suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from collections.abc import Callable

_STDOUT_LIMIT = 1 << 20  # 1 MB
_RESULT_STR_LIMIT = 64 * 1024  # 64 KB textual result


def _on_alarm(signum: int, frame: Any) -> None:
    raise TimeoutError("wall-clock timeout inside sandboxed code")


def _safe_json(obj: Any) -> Any:
    """Return ``obj`` if JSON-serialisable, else its ``repr``."""
    try:
        json.dumps(obj)
        return obj
    except (TypeError, ValueError):
        return repr(obj)[:_RESULT_STR_LIMIT]


def _guess_mime(path: Path) -> str | None:
    mime, _ = mimetypes.guess_type(str(path))
    return mime


def _file_fingerprint(path: Path) -> tuple[int, int] | None:
    """Return ``(size, mtime_ns)`` for *path*, or ``None`` if unreadable."""
    try:
        st = path.stat()
        return (int(st.st_size), int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9))))
    except OSError:
        return None


def _scan_files(base: Path) -> dict[Path, tuple[int, int]]:
    """Return ``path → (size, mtime_ns)`` for files under *base* (best-effort)."""
    out: dict[Path, tuple[int, int]] = {}
    if not base.exists():
        return out
    for path in base.rglob("*"):
        try:
            if not path.is_file():
                continue
        except OSError:
            continue
        fp = _file_fingerprint(path)
        if fp is not None:
            out[path] = fp
    return out


def _listing(
    base: Path,
    before: dict[Path, tuple[int, int]],
    *,
    extra_roots: list[Path] | None = None,
    extra_before: dict[Path, dict[Path, tuple[int, int]]] | None = None,
) -> list[dict[str, Any]]:
    """Diff workspace + extra roots against their pre-execution snapshots.

    Emits both **created** files and **modified** files (size/mtime changed).
    Unchanged paths that already existed before the run are omitted.

    Files inside *base* are reported with **relative** paths (legacy behaviour).
    Files inside *extra_roots* are reported with **absolute** paths so the
    parent controller can ingest them via ``register_external_file``.
    """
    produced: list[dict[str, Any]] = []
    seen: set[Path] = set()

    def _emit(path: Path, *, root: Path | None, change: str) -> None:
        if path in seen:
            return
        seen.add(path)
        try:
            size = path.stat().st_size
        except OSError:
            return
        mime = _guess_mime(path)
        if root is not None and root == base:
            try:
                location = str(path.relative_to(root))
            except ValueError:
                location = str(path)
        else:
            location = str(path)
        # ``file_path`` is the canonical produced-file key (tool-parameter
        # contract). ``path`` is kept as a legacy alias for older readers.
        entry: dict[str, Any] = {
            "file_path": location,
            "path": location,
            "bytes": size,
            "change": change,
        }
        if mime:
            entry["mime"] = mime
        produced.append(entry)

    def _diff_tree(
        root: Path,
        snapshot: dict[Path, tuple[int, int]],
        *,
        location_root: Path | None,
    ) -> None:
        if not root.exists():
            return
        for path in root.rglob("*"):
            try:
                if not path.is_file():
                    continue
            except OSError:
                continue
            fp = _file_fingerprint(path)
            if fp is None:
                continue
            prior = snapshot.get(path)
            if prior is None:
                _emit(path, root=location_root, change="created")
            elif prior != fp:
                _emit(path, root=location_root, change="modified")

    _diff_tree(base, before, location_root=base)

    for root in extra_roots or []:
        if not root.exists():
            continue
        snapshot = (extra_before or {}).get(root, {})
        for path in root.rglob("*"):
            try:
                if not path.is_file():
                    continue
            except OSError:
                continue
            try:
                resolved = path.resolve()
            except OSError:
                resolved = path
            try:
                if resolved.is_relative_to(base):
                    continue
            except (AttributeError, ValueError):
                pass
            fp = _file_fingerprint(path)
            if fp is None:
                continue
            prior = snapshot.get(path)
            if prior is None:
                _emit(path, root=root, change="created")
            elif prior != fp:
                _emit(path, root=root, change="modified")

    return produced


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit], True


_VALID_IMPORT_TIERS = frozenset({"stdlib", "extended", "unrestricted"})
_STDLIB_IMPORT_ROOTS = frozenset(getattr(sys, "stdlib_module_names", ())) | {
    "__future__",
    "builtins",
}
_EXTENDED_IMPORT_ROOTS = frozenset(
    {
        "bs4",
        "cv2",
        "docx",
        "httpx",
        "jinja2",
        "lxml",
        "matplotlib",
        "numpy",
        "openpyxl",
        "pandas",
        "PIL",
        "plotly",
        "pptx",
        "requests",
        "scipy",
        "seaborn",
        "sklearn",
        "statsmodels",
        "xlsxwriter",
        "yaml",
    }
)


def _normalise_import_root(raw: object) -> str | None:
    root = str(raw or "").strip().split(".", 1)[0]
    if not root or not root.isidentifier():
        return None
    return root


def _allowed_import_roots(
    import_tier: str,
    extra_import_roots: list[str] | tuple[str, ...],
) -> frozenset[str] | None:
    tier = (import_tier or "unrestricted").strip().lower()
    if tier not in _VALID_IMPORT_TIERS:
        raise ValueError(
            "import_tier must be one of: stdlib, extended, unrestricted"
        )
    if tier == "unrestricted":
        return None

    allowed = set(_STDLIB_IMPORT_ROOTS)
    if tier == "extended":
        allowed.update(_EXTENDED_IMPORT_ROOTS)
    for raw in extra_import_roots:
        root = _normalise_import_root(raw)
        if root:
            allowed.add(root)
    return frozenset(allowed)


def _guarded_import(allowed_roots: frozenset[str]) -> Callable[..., Any]:
    import builtins

    real_import = builtins.__import__

    def _import(
        name: str,
        globals: dict[str, Any] | None = None,
        locals: dict[str, Any] | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> Any:
        root = (name or "").split(".", 1)[0]
        if level == 0 and root and root not in allowed_roots:
            raise ImportError(
                f"Import '{root}' is not allowed by the active import_tier"
            )
        return real_import(name, globals, locals, fromlist, level)

    return _import


def _sandbox_builtins(
    *,
    workspace_path: Path,
    import_tier: str = "unrestricted",
    extra_import_roots: list[str] | tuple[str, ...] = (),
) -> dict[str, Any]:
    """Full builtins for user ``exec`` — no import restrictions."""
    import builtins

    out = dict(builtins.__dict__)
    out["__name__"] = "__sandbox__"
    allowed_roots = _allowed_import_roots(import_tier, extra_import_roots)
    if allowed_roots is not None:
        out["__import__"] = _guarded_import(allowed_roots)
    return out


def _positive_int(raw: object) -> int:
    if not isinstance(raw, (str, bytes, bytearray, int, float)):
        return 0
    try:
        value = int(raw or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, value)


def _positive_seconds(raw: object) -> int:
    if not isinstance(raw, (str, bytes, bytearray, int, float)):
        return 0
    try:
        value = float(raw or 0)
    except (TypeError, ValueError):
        return 0
    if value <= 0:
        return 0
    return max(1, int(math.ceil(value)))


def _apply_resource_limits(payload: dict[str, Any]) -> dict[str, Any]:
    """Apply POSIX resource limits inside the child process, best effort."""
    metadata: dict[str, Any] = {
        "supported": False,
        "applied": {},
        "warnings": [],
    }
    try:
        import resource
    except Exception as exc:  # noqa: BLE001
        metadata["warnings"].append(f"resource module unavailable: {exc}")
        return metadata

    metadata["supported"] = True
    applied: dict[str, int] = {}
    warnings: list[str] = []
    resource_attrs = resource.__dict__
    getrlimit = resource_attrs.get("getrlimit")
    setrlimit = resource_attrs.get("setrlimit")
    rlim_infinity = resource_attrs.get("RLIM_INFINITY")
    if not callable(getrlimit) or not callable(setrlimit) or rlim_infinity is None:
        metadata["warnings"].append("resource module lacks limit APIs")
        return metadata

    def _set(label: str, resource_name: str, requested: int) -> None:
        res_id = resource_attrs.get(resource_name)
        if requested <= 0 or res_id is None:
            return
        try:
            current_soft, current_hard = getrlimit(res_id)
            target = requested
            if current_hard != rlim_infinity:
                target = min(target, int(current_hard))
            if current_soft != rlim_infinity:
                target = min(target, int(current_soft))
            if target <= 0:
                return
            setrlimit(res_id, (target, current_hard))
            applied[label] = int(target)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"{label}: {exc}")

    _set("cpu_sec", "RLIMIT_CPU", _positive_seconds(payload.get("cpu_sec")))
    _set("memory_bytes", "RLIMIT_AS", _positive_int(payload.get("memory_bytes")))
    _set("file_bytes", "RLIMIT_FSIZE", _positive_int(payload.get("file_bytes")))
    _set("open_files", "RLIMIT_NOFILE", _positive_int(payload.get("open_files")))
    _set("max_processes", "RLIMIT_NPROC", _positive_int(payload.get("max_processes")))

    metadata["applied"] = applied
    metadata["warnings"] = warnings
    return metadata


def _configure_matplotlib_if_needed(source: str, base: Path) -> None:
    markers = (
        "matplotlib",
        "pyplot",
        "plt.",
        "font_manager",
        "mpl.",
        "pylab",
    )
    if not any(m in source for m in markers):
        return
    os.environ.setdefault("MPLBACKEND", "Agg")
    try:
        import matplotlib

        matplotlib.use("Agg", force=True)
        import matplotlib.pyplot as plt
        from matplotlib.figure import Figure
    except Exception:
        return

    ensure_cjk = None
    try:
        from StAgent.code.matplotlib_cjk import configure_matplotlib_cjk as _configure_cjk

        _configure_cjk()
        ensure_cjk = _configure_cjk
    except Exception:
        pass

    def _ensure_cjk() -> None:
        if ensure_cjk is None:
            return
        with suppress(Exception):
            ensure_cjk()

    _orig_plt_savefig = plt.savefig
    _orig_fig_savefig = Figure.savefig

    def _savefig(*args: Any, **kwargs: Any) -> Any:
        _ensure_cjk()
        return _orig_plt_savefig(*args, **kwargs)

    def _fig_savefig(self: Any, *args: Any, **kwargs: Any) -> Any:
        _ensure_cjk()
        return _orig_fig_savefig(self, *args, **kwargs)

    def _show(*args: Any, **kwargs: Any) -> None:
        if not plt.get_fignums():
            return
        target = base / f"figure_{int(time.time() * 1000)}.png"
        # Goes through wrapped savefig → re-applies CJK after script overrides.
        plt.savefig(target, dpi=150, bbox_inches="tight")

    plt.savefig = _savefig
    Figure.savefig = _fig_savefig  # type: ignore[method-assign]
    plt.show = _show


_RUNNER_V2_MAGIC = b"StAgent_RUNNER_V2\n"


def build_runner_stdin(payload: dict[str, Any]) -> bytes:
    """Build V2 stdin: magic + JSON meta (no ``source``) + length + raw UTF-8 body."""
    source = payload.get("source", "")
    if not isinstance(source, str):
        source = str(source)
    src_b = source.encode("utf-8")
    meta = {k: v for k, v in payload.items() if k != "source"}
    meta["runner_protocol"] = 2
    return (
        _RUNNER_V2_MAGIC
        + json.dumps(meta, ensure_ascii=False).encode("utf-8")
        + b"\n"
        + str(len(src_b)).encode("ascii")
        + b"\n"
        + src_b
    )


def _load_runner_payload(raw: bytes) -> dict[str, Any]:
    if raw.startswith(_RUNNER_V2_MAGIC):
        rest = raw[len(_RUNNER_V2_MAGIC) :]
        nl = rest.find(b"\n")
        if nl < 0:
            raise ValueError("missing metadata JSON after StAgent_RUNNER_V2")
        meta = json.loads(rest[:nl].decode("utf-8"))
        if not isinstance(meta, dict):
            raise ValueError("runner metadata must be a JSON object")
        tail = rest[nl + 1 :]
        nl2 = tail.find(b"\n")
        if nl2 < 0:
            raise ValueError("missing source byte length line")
        n = int(tail[:nl2].decode("ascii").strip())
        body = tail[nl2 + 1 :]
        if len(body) != n:
            raise ValueError(
                f"source length mismatch: expected {n} bytes, got {len(body)}",
            )
        out: dict[str, Any] = dict(meta)
        out["source"] = body.decode("utf-8")
        return out
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("runner payload must be a JSON object")
    return payload


def main() -> int:
    try:
        raw_in = sys.stdin.buffer.read()
        payload = _load_runner_payload(raw_in)
    except Exception as exc:  # noqa: BLE001
        json.dump({"status": "error", "error": f"bad payload: {exc}"}, sys.stdout)
        sys.stdout.write("\n")
        return 2

    source = payload.get("source", "")
    globals_in = payload.get("globals", {}) or {}
    timeout_sec = float(payload.get("timeout_sec", 30.0))
    workspace = payload.get("workspace")
    extra_scan_roots_raw = payload.get("extra_scan_roots") or []
    import_tier = str(payload.get("import_tier") or "unrestricted").strip().lower()
    extra_import_roots = tuple(str(r) for r in (payload.get("extra_import_roots") or ()))

    if workspace:
        base = Path(workspace)
        base.mkdir(parents=True, exist_ok=True)
        os.chdir(base)
    else:
        base = Path.cwd()

    extra_roots: list[Path] = []
    seen_roots: set[Path] = set()
    for raw in extra_scan_roots_raw:
        if not raw:
            continue
        try:
            candidate = Path(str(raw)).expanduser().resolve()
        except OSError:
            continue
        try:
            if candidate == base or candidate.is_relative_to(base):
                continue
        except (AttributeError, ValueError):
            pass
        if candidate in seen_roots:
            continue
        seen_roots.add(candidate)
        extra_roots.append(candidate)

    limit_metadata = _apply_resource_limits(payload)

    os.environ.setdefault("MPLBACKEND", "Agg")
    _configure_matplotlib_if_needed(source, base)
    before = _scan_files(base)
    extra_before: dict[Path, dict[Path, tuple[int, int]]] = {
        root: _scan_files(root) for root in extra_roots
    }

    started = time.monotonic()
    signal_attrs = signal.__dict__
    alarm_signal = signal_attrs.get("SIGALRM")
    setitimer = signal_attrs.get("setitimer")
    itimer_real = signal_attrs.get("ITIMER_REAL")
    use_alarm = (
        alarm_signal is not None
        and callable(setitimer)
        and itimer_real is not None
    )
    if use_alarm:
        signal.signal(cast("Any", alarm_signal), _on_alarm)
        cast("Any", setitimer)(itimer_real, timeout_sec)

    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    status = "ok"
    error_msg: str | None = None
    result_val: Any = None

    globals_env: dict[str, Any] = {
        "__name__": "__sandbox__",
        "__builtins__": _sandbox_builtins(
            workspace_path=base,
            import_tier=import_tier,
            extra_import_roots=extra_import_roots,
        ),
    }
    globals_env.update(globals_in)

    try:
        with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
            exec(compile(source, "<sandbox>", "exec"), globals_env)  # noqa: S102
        result_val = globals_env.get("result")
    except TimeoutError as exc:
        status = "timeout"
        error_msg = str(exc) or "timeout"
    except MemoryError:
        status = "memory"
        error_msg = "memory limit exceeded"
    except BaseException as exc:  # noqa: BLE001 — surfaced to caller
        status = "error"
        error_msg = f"{type(exc).__name__}: {exc}"
        stderr_buf.write("\n" + traceback.format_exc())
    finally:
        if use_alarm:
            cast("Any", setitimer)(itimer_real, 0)

    duration_ms = int((time.monotonic() - started) * 1000)
    stdout_text, stdout_trunc = _truncate(stdout_buf.getvalue(), _STDOUT_LIMIT)
    stderr_text, stderr_trunc = _truncate(stderr_buf.getvalue(), _STDOUT_LIMIT)

    envelope = {
        "status": status,
        "error": error_msg,
        "stdout": stdout_text,
        "stdout_truncated": stdout_trunc,
        "stderr": stderr_text,
        "stderr_truncated": stderr_trunc,
        "result": _safe_json(result_val),
        "produced_files": _listing(
            base,
            before,
            extra_roots=extra_roots,
            extra_before=extra_before,
        ),
        "duration_ms": duration_ms,
        "metadata": {
            "import_tier": import_tier,
            "runner_limits": limit_metadata,
        },
    }
    json.dump(envelope, sys.stdout)
    sys.stdout.write("\n")
    sys.stdout.flush()
    return 0 if status == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
