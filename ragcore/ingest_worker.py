"""Run each uploaded document parser in its own resource-limited process."""

import json
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from .documents import MAX_FILE_BYTES, SUPPORTED_EXTENSIONS
from .ocr import IMAGE_EXTENSIONS


WALL_TIMEOUT_SECONDS = 40
_CPU_SECONDS = 30
_MEMORY_BYTES = 768 * 1024 * 1024
_OCR_MEMORY_BYTES = 2 * 1024 * 1024 * 1024
_MAX_RESULT_BYTES = 24 * 1024 * 1024
_SLOTS = threading.BoundedSemaphore(2)
_PROJECT = Path(__file__).resolve().parent.parent
_RUNTIME_ENV = {
    "PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TMPDIR", "HOME", "USERPROFILE",
    "LOCALAPPDATA", "APPDATA", "LANG", "LC_ALL", "TZ", "LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH",
}


class UploadBusyError(ValueError):
    """Both parser slots are in use. The caller can retry the request."""


def _worker_limits(suffix):
    if os.name != "posix":
        return
    import resource
    import signal

    def limit(kind, ceiling):
        soft, hard = resource.getrlimit(kind)
        combined = [ceiling] + [v for v in (soft, hard) if v != resource.RLIM_INFINITY]
        bounded = min(combined)
        resource.setrlimit(kind, (bounded, bounded))

    limit(resource.RLIMIT_CPU, _CPU_SECONDS)
    if hasattr(resource, "RLIMIT_AS"):
        # ONNX and PDFium map native libraries and model weights into virtual memory.
        uses_ocr = os.getenv("RAG_OCR_ENABLED") == "1" and (suffix in IMAGE_EXTENSIONS or suffix == ".pdf")
        limit(resource.RLIMIT_AS, _OCR_MEMORY_BYTES if uses_ocr else _MEMORY_BYTES)
    limit(resource.RLIMIT_FSIZE, _MAX_RESULT_BYTES)
    # The child also times out if its parent exits unexpectedly.
    signal.alarm(WALL_TIMEOUT_SECONDS)


def _worker_main(suffix):
    from .documents import parse_document

    try:
        _worker_limits(suffix)
        data = sys.stdin.buffer.read(MAX_FILE_BYTES + 1)
        entries = parse_document("upload" + suffix, data)
        encoded = json.dumps(entries, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > _MAX_RESULT_BYTES:
            raise ValueError("提取结果超过 24 MiB，请拆分文件")
    except ValueError as exc:
        # Third-party error text can contain untrusted fragments of the input.
        sys.stdout.buffer.write(str(exc)[:800].encode("utf-8", errors="replace"))
        return 2
    except MemoryError:
        return 3
    sys.stdout.buffer.write(encoded)
    return 0


def _run_worker(suffix, content):
    env = {key: value for key, value in os.environ.items() if key.upper() in _RUNTIME_ENV}
    if os.environ.get("RAG_OCR_ENABLED") == "1":
        env["RAG_OCR_ENABLED"] = "1"
    env.update(OPENBLAS_NUM_THREADS="1", MKL_NUM_THREADS="1", OMP_NUM_THREADS="1")
    # Temporary file handles avoid pipe backpressure and unbounded in-memory capture.
    with tempfile.TemporaryFile() as source, tempfile.TemporaryFile() as output:
        source.write(content)
        source.seek(0)
        try:
            result = subprocess.run(
                [sys.executable, "-B", "-m", "ragcore.ingest_worker", suffix],
                stdin=source, stdout=output, stderr=subprocess.DEVNULL,
                cwd=_PROJECT, env=env, timeout=WALL_TIMEOUT_SECONDS, check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired as exc:
            # subprocess.run kills the process and waits for it before raising.
            raise ValueError("文档解析超时，请拆分文件后重试") from exc
        output.seek(0)
        if result.returncode == 2:
            message = output.read(4096).decode("utf-8", errors="replace")
            raise ValueError(message or "文件解析失败")
        if result.returncode != 0:
            raise ValueError("文档解析进程异常结束或达到资源上限，请拆分文件后重试")
        payload = output.read(_MAX_RESULT_BYTES + 1)
        if len(payload) > _MAX_RESULT_BYTES:
            raise ValueError("文档提取结果过大，请拆分文件")
        try:
            return json.loads(payload)
        except (ValueError, RecursionError) as exc:
            raise ValueError("文档解析进程返回无效结果") from exc


def parse_upload(filename: str, content: bytes) -> list[dict]:
    """Return the same evidence as parse_document, or raise ValueError on a failed import."""
    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise ValueError(f"不支持此文件扩展名：{suffix or '无'}")
    if not content or len(content) > MAX_FILE_BYTES:
        raise ValueError("文件为空或超过单文件 20 MiB 上限")
    if not _SLOTS.acquire(blocking=False):
        raise UploadBusyError("文件解析任务已满，请稍后重试")
    try:
        return _run_worker(suffix, content)
    finally:
        _SLOTS.release()


if __name__ == "__main__":
    sys.exit(_worker_main(sys.argv[1]) if len(sys.argv) == 2 else 1)
