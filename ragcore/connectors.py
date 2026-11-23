"""Read-only JSON connectors with evidence suitable for saving alongside an answer."""

import concurrent.futures
import datetime as dt
import hashlib
import http.client
import ipaddress
import json
import math
import os
import re
import socket
import ssl
import threading
import time
from decimal import Decimal, InvalidOperation
from urllib.parse import quote, urlencode, urlsplit, urlunsplit


class ConnectorError(ValueError):
    """A safe, user-facing connector configuration or execution error."""


MAX_BYTES = 1024 * 1024
MAX_ROWS = 500
TIMEOUT = 8.0
_JS_SAFE_INTEGER = 2**53 - 1
_DNS_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="rag-dns")
_DNS_SLOTS = threading.BoundedSemaphore(4)
_IDENTIFIER = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]{0,63}\Z")
_SECRET_NAME = re.compile(r"token|password|passwd|credential|secret|api.?key|authorization|cookie", re.I)
_PRIVATE_NETS = tuple(ipaddress.ip_network(net) for net in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "fc00::/7", "::1/128"
))


def _string(value, label, limit, *, required=False):
    if (not isinstance(value, str) or len(value) > limit
            or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in value)):
        raise ConnectorError(f"{label}须为 {limit} 字以内的单行文本")
    value = value.strip()
    if required and not value:
        raise ConnectorError(f"缺少{label}")
    return value


def _string_list(value, label, limit):
    if not isinstance(value, list) or len(value) > limit:
        raise ConnectorError(f"{label}须为最多 {limit} 项的列表")
    return list(dict.fromkeys(_string(v, label, 80, required=True) for v in value))


def _allowed_target(parts):
    host = parts.hostname
    try:
        port = parts.port
    except ValueError:
        raise ConnectorError("接口端口格式错误") from None
    if not host or parts.netloc.endswith(":") or port == 0:
        raise ConnectorError("接口地址缺少有效主机或端口")
    if not re.fullmatch(r"[A-Za-z0-9:.\-]+", host) or host.endswith("."):
        raise ConnectorError("请填写有效的 ASCII 域名或 IP 地址")
    if ":" in host:
        try:
            ipaddress.IPv6Address(host)
        except ValueError:
            raise ConnectorError("IPv6 地址无效") from None
        authority = f"[{host}]"
    else:
        authority = host
    expected = authority if port is None else f"{authority}:{port}"
    if expected != parts.netloc.lower():
        raise ConnectorError("接口主机或端口格式无效")
    default = 443 if parts.scheme == "https" else 80
    port = port or default
    allowed = {item.strip().lower() for item in os.environ.get("RAG_ALLOWED_HOSTS", "").split(",")}
    if f"{authority}:{port}" not in allowed and not (port == default and authority in allowed):
        raise ConnectorError("目标主机或端口未列入 RAG_ALLOWED_HOSTS")
    return host, port


def validate_config(config: dict) -> dict:
    """Validate and normalize a persisted connector definition without making any network request."""
    if not isinstance(config, dict):
        raise ConnectorError("接口配置须为 JSON 对象")
    name = _string(config.get("name", ""), "接口名称", 100, required=True)
    description = _string(config.get("description", ""), "接口说明", 800)
    url = _string(config.get("url", ""), "接口地址", 1500, required=True)
    records_path = _string(config.get("records_path", ""), "记录路径", 160)
    if records_path and not all(re.fullmatch(r"[\w-]{1,64}", k) for k in records_path.split(".")):
        raise ConnectorError("记录路径应填写 JSON 字段名，以点连接嵌套字段，读取根时留空")
    token_env = _string(config.get("token_env") or "", "凭据环境变量", 100)
    if token_env and not re.fullmatch(r"RAG_CONNECTOR_[A-Z][A-Z0-9_]*", token_env):
        raise ConnectorError("凭据环境变量须以 RAG_CONNECTOR_ 开头，并使用大写英文、数字或下划线")
    try:
        parts = urlsplit(url)
    except ValueError:
        raise ConnectorError("接口地址格式无效") from None
    if url != "demo://budget":
        if parts.scheme not in {"https", "http"} or not parts.netloc:
            raise ConnectorError("仅支持 HTTP(S) 接口或内置预算演示")
        if parts.username is not None or parts.password is not None or any(c in url for c in "\\?# "):
            raise ConnectorError("接口地址只填写协议、主机及路径，查询参数和凭据请在各自配置项中填写")
        _allowed_target(parts)
    if token_env and parts.scheme != "https":
        raise ConnectorError("Bearer 凭据须通过 HTTPS 传输")
    raw_params = config.get("parameters", [])
    if not isinstance(raw_params, list) or len(raw_params) > 24:
        raise ConnectorError("参数定义应为最多 24 项的列表")
    parameters = []
    names = set()
    for raw in raw_params:
        if not isinstance(raw, dict):
            raise ConnectorError("每个参数定义应为 JSON 对象")
        key = _string(raw.get("name", ""), "参数名", 64, required=True)
        kind = raw.get("type", "string")
        if not _IDENTIFIER.fullmatch(key) or key in names:
            raise ConnectorError("参数名须为唯一的英文标识符")
        if _SECRET_NAME.search(key):
            raise ConnectorError("查询参数只用于业务筛选，凭据请使用 token_env")
        if kind not in ("string", "integer", "number", "date"):
            raise ConnectorError(f"参数 {key} 的类型无效")
        if url == "demo://budget" and (key, kind) not in {("project", "string"), ("year", "integer")}:
            raise ConnectorError("预算演示仅支持 project 字符串与 year 整数两种参数")
        required = raw.get("required", False)
        if not isinstance(required, bool):
            raise ConnectorError(f"参数 {key} 的 required 必须为布尔值")
        names.add(key)
        parameters.append({
            "name": key,
            "label": _string(raw.get("label", key), "参数显示名", 80, required=True),
            "type": kind,
            "required": required,
            "aliases": _string_list(raw.get("aliases", []), "参数别名", 24),
        })
    return {
        "name": name, "description": description, "url": url,
        "keywords": _string_list(config.get("keywords", []), "意图关键词", 64),
        "parameters": parameters, "records_path": records_path, "token_env": token_env,
    }


def _parameters(definitions, values):
    if not isinstance(values, dict) or set(values) - {p["name"] for p in definitions}:
        raise ConnectorError("查询含有未声明的参数")
    result = {}
    for p in definitions:
        key, label, kind = p["name"], p["label"], p["type"]
        value = values.get(key)
        if value is None or value == "":
            if p["required"]:
                raise ConnectorError(f"缺少必填参数「{label}」")
            continue
        try:
            if kind == "integer":
                if isinstance(value, bool) or not re.fullmatch(r"[+-]?[0-9]{1,19}", str(value)):
                    raise ValueError()
                value = int(value)
                if not -(2**63) <= value < 2**63:
                    raise ValueError()
            elif kind == "number":
                # Preserve the supplied decimal literal when encoding the query.
                if (type(value) not in (str, int, float) or len(str(value)) > 100
                        or not re.fullmatch(r"[+-]?(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", str(value))):
                    raise ValueError()
            elif kind == "date":
                if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
                    raise ValueError()
                value = dt.date.fromisoformat(value).isoformat()
            else:
                value = _string(value, label, 256, required=True)
        except (ValueError, OverflowError):
            raise ConnectorError(f"参数「{label}」不符合 {kind} 格式") from None
        result[key] = value
    return result


def _permitted_ip(address):
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        elif ip.sixtofour or ip.teredo or ip in ipaddress.ip_network("64:ff9b::/96") or ip in ipaddress.ip_network("64:ff9b:1::/48"):
            return False
    if ip.is_link_local or ip.is_multicast or ip.is_unspecified:
        return False
    if ip.is_global and not ip.is_reserved and not ip.is_loopback:
        return True
    return os.getenv("RAG_ALLOW_PRIVATE_NETWORK") == "1" and any(
        ip.version == net.version and ip in net for net in _PRIVATE_NETS
    )


def _resolve(host, port):
    if not _DNS_SLOTS.acquire(blocking=False):
        raise ConnectorError("接口 DNS 解析忙，请稍后重试")
    try:
        task = _DNS_POOL.submit(socket.getaddrinfo, host, port, 0, socket.SOCK_STREAM)
    except RuntimeError:
        _DNS_SLOTS.release()
        raise ConnectorError("DNS 解析器不可用") from None
    task.add_done_callback(lambda _: _DNS_SLOTS.release())
    try:
        addresses = task.result(timeout=min(3, TIMEOUT))
    except (OSError, concurrent.futures.TimeoutError):
        raise ConnectorError("接口主机解析超时或失败") from None
    if not addresses or any(not _permitted_ip(addr[4][0]) for addr in addresses):
        raise ConnectorError("目标地址被网络访问策略拒绝")
    return addresses


def _abort(sock):
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


class ConnectorRead:
    """Let a disconnected query interrupt the blocking upstream HTTP read."""
    def __init__(self):
        self._lock = threading.Lock()
        self._socket = None
        self._cancelled = False

    def attach(self, sock):
        with self._lock:
            if self._cancelled:
                raise ConnectorError("查询已停止")
            self._socket = sock

    def cancel(self):
        with self._lock:
            self._cancelled = True
            if self._socket is not None:
                _abort(self._socket)


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError()
    return remaining


def _connect(addresses, deadline):
    for family, socktype, proto, _, addr in addresses:
        sock = socket.socket(family, socktype, proto)
        try:
            sock.settimeout(_remaining(deadline))
            sock.connect(addr)
            return sock
        except OSError:
            sock.close()
    raise ConnectorError("无法连接接口，请检查服务、端口及网络设置")


def _get_body(parts, target, token_env, read=None):
    host, port = _allowed_target(parts)
    headers = {"Accept": "application/json", "Accept-Encoding": "identity", "Connection": "close"}
    if token_env:
        token = os.environ.get(token_env, "")
        if not re.fullmatch(r"[A-Za-z0-9._~+/=-]{1,8192}", token):
            raise ConnectorError("接口凭据环境变量为空或格式无效")
        headers["Authorization"] = "Bearer " + token
    deadline = time.monotonic() + TIMEOUT
    addresses = _resolve(host, port)
    sock = None
    timer = None
    # HTTPConnection writes HTTP/1.1 over our already-connected authenticated socket;
    # its automatic reconnection is disabled so the host cannot be resolved again.
    conn = http.client.HTTPConnection(host, port)
    conn.auto_open = False
    headers["Host"] = parts.netloc
    try:
        sock = _connect(addresses, deadline)
        if parts.scheme == "https":
            sock.settimeout(_remaining(deadline))
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
        conn.sock = sock
        if read is not None:
            read.attach(sock)
        # A socket's read timeout resets for each read. Enforce the full request
        # deadline even if a server sends the headers or body a byte at a time.
        timer = threading.Timer(_remaining(deadline), _abort, (sock,))
        timer.daemon = True
        timer.start()
        conn.request("GET", target, headers=headers)
        with conn.getresponse() as response:
            if not 200 <= response.status < 300:
                raise ConnectorError(f"接口返回 HTTP {response.status}")
            if response.getheader("Content-Encoding", "identity").lower() != "identity":
                raise ConnectorError("接口须返回未压缩的 JSON")
            length = response.getheader("Content-Length")
            if length is not None and (not re.fullmatch(r"[0-9]{1,15}", length) or int(length) > MAX_BYTES):
                raise ConnectorError("接口响应超过 1 MiB 或长度标记无效")
            body = response.read(MAX_BYTES + 1)
            if len(body) > MAX_BYTES:
                raise ConnectorError("接口响应超过 1 MiB，请缩小查询范围")
            if length is not None and len(body) != int(length):
                raise ConnectorError("接口响应传输不完整")
            _remaining(deadline)
    except TimeoutError:
        raise ConnectorError("接口查询超时") from None
    except (OSError, http.client.HTTPException):
        raise ConnectorError("接口连接、证书校验或响应读取失败") from None
    finally:
        if timer:
            timer.cancel()
        conn.close()
        if sock:
            sock.close()
    return body


def _load_json(body):
    try:
        payload = json.loads(body.decode("utf-8-sig"), parse_constant=_invalid_constant,
                             parse_float=_exact_number, parse_int=_exact_integer, object_pairs_hook=_object)
        # Reject isolated surrogate escapes before passing text to UTF-8 storage.
        json.dumps(payload, ensure_ascii=False).encode("utf-8")
        return payload
    except (ValueError, RecursionError):
        raise ConnectorError("接口响应须为有效的 UTF-8 JSON") from None


def _invalid_constant(_):
    raise ValueError()


def _object(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError()
    return result


def _exact_integer(text):
    if len(text.lstrip("-")) > 16:
        return text
    value = int(text)
    return value if abs(value) <= _JS_SAFE_INTEGER else text


def _exact_number(text):
    # JSON numbers which cannot be represented exactly by IEEE 754 binary64
    # remain source strings, including financial decimals such as 0.10.
    if len(text) > 128:
        return text
    try:
        exact = Decimal(text)
        value = float(exact)
    except (InvalidOperation, OverflowError):
        return text
    if not math.isfinite(value) or Decimal.from_float(value) != exact:
        return text
    if value.is_integer() and abs(value) > _JS_SAFE_INTEGER:
        return text
    return value


def _demo_budget(params):
    cases = [
        ("材料研发平台", 2025, 1000, 680),
        ("材料研发平台", 2026, 1200, 900),
        ("智能制造项目", 2026, 800, 480),
    ]
    return [
        {"项目名称": name, "年度": year, "预算金额（万元）": budget,
         "已执行金额（万元）": used, "预算执行率": f"{used / budget:.0%}"}
        for name, year, budget, used in cases
        if ("project" not in params or params["project"] == name)
        and ("year" not in params or params["year"] == year)
    ]


def _records(payload, path):
    value = payload
    if path:
        for key in path.split("."):
            if isinstance(value, dict) and key in value:
                value = value[key]
            else:
                raise ConnectorError(f"响应中未找到已配置的记录路径「{path}」")
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise ConnectorError("记录路径须指向 JSON 对象或对象数组")
    if len(value) > MAX_ROWS:
        raise ConnectorError(f"接口返回超过 {MAX_ROWS} 行，请缩小查询范围")
    return value


def execute(config: dict, params: dict, *, demo_handler=None, include_raw=False, read=None) -> dict:
    """Fetch matching rows and the sanitized GET request used to produce them."""
    settings = validate_config(config)
    values = _parameters(settings["parameters"], params)
    parts = urlsplit(settings["url"])
    query = urlencode(values)
    path = quote(parts.path, safe="/:@%+!$&'()*,;=-._~")
    request = urlunsplit((parts.scheme, parts.netloc, path, query, ""))
    if parts.scheme == "demo":
        payload = (demo_handler or _demo_budget)(values)
        body = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    else:
        target = (path or "/") + ("?" + query if query else "")
        body = _get_body(parts, target, settings["token_env"], read)
    payload = _load_json(body)
    rows = _records(payload, settings["records_path"])
    result = {
        "rows": rows, "total_count": len(rows), "response_sha256": hashlib.sha256(body).hexdigest(),
        "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "request": "GET " + request,
    }
    if include_raw:
        result["raw_body"] = body
        result["applied_params"] = values
        if isinstance(payload, dict) and isinstance(payload.get("executed_sql"), str) and payload["executed_sql"].strip():
            result["executed_sql"] = payload["executed_sql"]
    return result
