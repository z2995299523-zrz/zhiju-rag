"""Start the Windows workspace from Explorer or a terminal."""
import argparse
import http.client
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import time
import webbrowser

from ragcore.workspace_id import workspace_id


ROOT = Path(__file__).resolve().parent
REQUIREMENTS = ROOT / "requirements.txt"
WHEELS = ROOT / "vendor" / "wheels"
DEFAULT_PORTS = range(8781, 8786)


def missing_requirements():
    from importlib.metadata import PackageNotFoundError, version
    try:
        from packaging.requirements import Requirement
    except ImportError:
        from pip._vendor.packaging.requirements import Requirement

    if sys.version_info < (3, 11):
        raise RuntimeError("需要 Python 3.11 或更高版本")
    with sqlite3.connect(":memory:") as conn:
        conn.execute("CREATE VIRTUAL TABLE probe USING fts5(content)")
    missing = []
    for line in REQUIREMENTS.read_text(encoding="utf-8-sig").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        req = Requirement(line)
        try:
            if version(req.name) in req.specifier:
                continue
        except PackageNotFoundError:
            pass
        missing.append(req.name)
    return missing


def inspect_runtime(executable):
    probe = subprocess.run([str(executable), str(Path(__file__).resolve()), "--check-runtime"],
                           cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    if probe.returncode:
        return None
    return json.loads(probe.stdout)


def choose_runtime():
    venv = ROOT / ".venv"
    local = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if local.is_file() and inspect_runtime(local) == []:
        return local
    base = Path(sys.executable)
    missing = inspect_runtime(base)
    if missing == []:
        return base
    if missing is None:
        raise RuntimeError("当前 Python 无法读取依赖或缺少 SQLite FTS5。请使用带 pip 和 FTS5 的 Python 3.11+。")
    if not local.is_file():
        print("正在创建项目虚拟环境…", flush=True)
        args = [str(base), "-m", "venv"]
        if missing == ["python-multipart"]:
            args.extend(["--without-pip", "--system-site-packages"])
        subprocess.run([*args, str(venv)], check=True)
    install = [str(local), "-m", "pip", "install", "--disable-pip-version-check", "--no-input"]
    if WHEELS.is_dir() and any(WHEELS.glob("*.whl")):
        offline = subprocess.run([*install, "--no-index", "--find-links", str(WHEELS), "-r", str(REQUIREMENTS)])
        if not offline.returncode and inspect_runtime(local) == []:
            return local
    print("正在从 Python 软件源安装其余项目依赖…", flush=True)
    subprocess.run([*install, "-r", str(REQUIREMENTS)], check=True)
    if inspect_runtime(local) != []:
        raise RuntimeError("依赖校验失败，请查看上方安装输出。")
    return local


def response_json(port, path):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
    try:
        conn.request("GET", path, headers={"Accept": "application/json"})
        response = conn.getresponse()
        if response.status != 200:
            raise ValueError("unexpected HTTP status")
        return json.loads(response.read(1_000_000))
    finally:
        conn.close()


def supports_embedded_reader(port):
    # Previous backends serve the same static files but refuse to frame the reader.
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
    try:
        conn.request("HEAD", "/reader.html")
        response = conn.getresponse()
        return response.status == 200 and response.getheader("X-Frame-Options", "").upper() == "SAMEORIGIN"
    finally:
        conn.close()


def service_state(port):
    # On Windows a connection to a free loopback port may time out before Winsock
    # reports ECONNREFUSED. Only a successful bind proves the port is available.
    with socket.socket() as probe:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            pass
        else:
            return "offline"
    try:
        health = response_json(port, "/healthz")
        # The child runs in ROOT, including when start.ps1 is invoked from another directory.
        expected = workspace_id(ROOT / (os.getenv("RAG_DATA_DIR") or "var"))
        if health.get("status") != "ok" or health.get("workspace_id") != expected:
            return "other"
        schema = response_json(port, "/api/openapi.json")
        paths = schema.get("paths", {})
        required = {"/api/query", "/api/query/stream", "/api/query/examples", "/api/admin/models",
                    "/api/documents", "/api/documents/{doc_id}/preview"}
        if (schema.get("info", {}).get("title") == "企业知识检索工作台" and required.issubset(paths)
                and supports_embedded_reader(port)):
            return "ready"
    except (OSError, ValueError, AttributeError, http.client.HTTPException):
        pass
    return "other"


def open_workspace(port, no_browser):
    url = f"http://127.0.0.1:{port}/"
    print(f"工作台已就绪：{url}", flush=True)
    if not no_browser:
        webbrowser.open(url)


def select_port(explicit_port):
    if explicit_port is not None:
        return explicit_port, service_state(explicit_port)
    free_port = None
    for candidate in DEFAULT_PORTS:
        state = service_state(candidate)
        if state == "ready":
            return candidate, state
        if free_port is None and state == "offline":
            free_port = candidate
    if free_port is None:
        raise RuntimeError(r"默认端口 8781–8785 均不可用。请用 .\start.cmd --port 其他端口 启动。")
    return free_port, "offline"


def run(explicit_port, no_browser):
    port, state = select_port(explicit_port)
    if state == "ready":
        open_workspace(port, no_browser)
        return 0
    if state == "other":
        raise RuntimeError(f"端口 {port} 已被其他程序占用或服务无响应。可运行 .\\start.cmd --port {port+1 if port<65535 else 8782}。")
    executable = choose_runtime()
    print(f"启动知据（{executable}）。保持此窗口开启，按 Ctrl+C 可停止服务。", flush=True)
    child = subprocess.Popen([str(executable), "-m", "uvicorn", "main:app", "--host", "127.0.0.1",
                              "--port", str(port), "--no-proxy-headers"], cwd=ROOT)
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if service_state(port) == "ready":
                open_workspace(port, no_browser)
                return child.wait()
            if child.poll() is not None:
                raise RuntimeError(f"服务提前退出（{child.returncode}），请查看上方日志。")
            time.sleep(0.3)
        raise RuntimeError("服务在 60 秒内未通过健康检查，请查看上方日志。")
    except KeyboardInterrupt:
        return 0
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--check-runtime", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.port is not None and not 1 <= args.port <= 65535:
        parser.error("port must be 1–65535")
    if args.check_runtime:
        print(json.dumps(missing_requirements()))
        return 0
    return run(args.port, args.no_browser)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"启动失败：{exc}", file=sys.stderr)
        sys.exit(1)
