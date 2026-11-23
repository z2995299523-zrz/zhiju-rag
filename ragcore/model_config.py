"""Workspace model settings and environment defaults, scoped to one query."""
import ipaddress
import json
import os
import re
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from .model_secrets import seal, unseal


Kind = Literal["llm", "embedding"]
PREFIX = {"llm": "LLM", "embedding": "EMBED"}


class ConfigError(ValueError):
    pass


class ModelDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["environment", "workspace", "disabled"]
    base_url: str = Field(default="", max_length=2048)
    model: str = Field(default="", max_length=512)
    api_key: SecretStr | None = Field(default=None, max_length=4096)
    clear_api_key: bool = False


@dataclass(frozen=True)
class Settings:
    base: str
    model: str
    token: str = field(repr=False)

    @property
    def local_origin(self):
        parsed = urlsplit(self.base)
        if _loopback(parsed.hostname):
            return f"{parsed.scheme}://{parsed.netloc}"
        return None


@dataclass(frozen=True)
class Profile:
    mode: str
    base_url: str
    model: str
    credential: str = field(default="", repr=False)


def _loopback(host):
    if host and host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _base(value):
    value = value.strip().rstrip("/")
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        parsed.port  # Reject malformed ports.
        if (not host or parsed.username is not None or parsed.password is not None or
                "?" in value or "#" in value or re.search(r"[\s\\\x00-\x1f\x7f]", value) or not value.isascii()):
            raise ValueError
        if parsed.scheme not in {"http", "https"} or (parsed.scheme == "http" and not _loopback(host)):
            raise ValueError
    except ValueError:
        raise ConfigError("请填写有效的 API 基础地址。远程服务使用 HTTPS，本机服务可用 HTTP；地址不可带凭据、查询参数或片段。") from None
    return value


def _name(value):
    value = value.strip()
    if not value or not value.isprintable() or len(value) > 512:
        raise ConfigError("请填写有效的模型名称。")
    return value


def _key(value):
    value = value.strip()
    if value and not re.fullmatch(r"[!-~]{1,4096}", value):
        raise ConfigError("API Key 须为单行可打印 ASCII 字符。")
    return value


def _environment(kind):
    prefix = "RAG_" + PREFIX[kind] + "_"
    try:
        base = _base(os.getenv(prefix + "BASE_URL", ""))
        model = _name(os.getenv(prefix + "MODEL", ""))
        token = _key(os.getenv(prefix + "API_KEY", ""))
    except ConfigError:
        return Settings("", "", "")
    return Settings(base, model, token)


def _load(conn, kind):
    row = conn.execute("SELECT value FROM settings WHERE name=?", ("model." + kind,)).fetchone()
    if row:
        return Profile(**json.loads(row[0]))
    env = _environment(kind)
    return Profile("environment", env.base, env.model, "environment" if env.token else "")


def _env_key(kind, profile):
    if profile.credential == "environment":
        env = _environment(kind)
        if profile.base_url == env.base:
            return env.token
    return ""


def _view(kind, profile):
    enabled = profile.mode != "disabled" and bool(profile.base_url and profile.model)
    source = "none" if profile.mode == "environment" and not enabled else profile.mode
    has_key = bool(_env_key(kind, profile) if profile.credential == "environment" else profile.credential)
    return {"mode": profile.mode, "source": source, "enabled": enabled, "base_url": profile.base_url,
            "model": profile.model, "has_api_key": has_key}


def overview(store):
    with store.db() as conn:
        return {kind: _view(kind, _load(conn, kind)) for kind in PREFIX}


def _candidate(kind, previous, draft):
    key = _key(draft.api_key.get_secret_value()) if draft.api_key else ""
    if key and draft.clear_api_key:
        raise ConfigError("填写新密钥和移除密钥只能选择一项。")
    if key and draft.mode != "workspace":
        raise ConfigError("仅可为工作区配置填写新密钥。")
    if draft.mode == "environment":
        env = _environment(kind)
        return Profile("environment", env.base, env.model, "environment" if env.token else ""), ""
    if draft.mode == "disabled":
        return replace(previous, mode="disabled", credential="" if draft.clear_api_key else previous.credential), ""
    base = _base(draft.base_url)
    model = _name(draft.model)
    credential = previous.credential if not draft.clear_api_key and previous.base_url == base else ""
    return Profile("workspace", base, model, credential), key


def save(store, kind, draft, actor):
    with store.db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        profile, key = _candidate(kind, _load(conn, kind), draft)
        name = "model." + kind
        if profile.mode == "environment":
            conn.execute("DELETE FROM settings WHERE name=?", (name,))
        else:
            if key:
                profile = replace(profile, credential=seal(store.path.parent, kind, profile.base_url, key))
            record = json.dumps(vars(profile), ensure_ascii=False)
            conn.execute("""INSERT INTO settings(name,value) VALUES (?,?)
                            ON CONFLICT(name) DO UPDATE SET value=excluded.value""", (name, record))
        store.audit(conn, actor, "model.configure", kind)
        return _view(kind, profile)


def _resolve(directory, kind, profile, key=""):
    if not _view(kind, profile)["enabled"]:
        return None
    if not key and profile.credential:
        key = (_env_key(kind, profile) if profile.credential == "environment" else
               unseal(directory, kind, profile.base_url, profile.credential))
    return Settings(profile.base_url, profile.model, key)


def probe_settings(store, kind, draft):
    with store.db() as conn:
        profile = _load(conn, kind)
    profile, key = _candidate(kind, profile, draft) if draft is not None else (profile, "")
    settings = _resolve(store.path.parent, kind, profile, key)
    if settings is None:
        raise ConfigError("该模型尚未启用，请填写服务地址与模型名称后再测试。")
    return settings


_workspace = ContextVar("workspace_model_profiles", default=None)


@contextmanager
def workspace(store):
    with store.db() as conn:
        profiles = {kind: _load(conn, kind) for kind in PREFIX}
    token = _workspace.set((store.path.parent, profiles, {}))
    try:
        yield
    finally:
        _workspace.reset(token)


def effective(prefix):
    kind = "llm" if prefix == "LLM" else "embedding"
    bound = _workspace.get()
    if bound is None:
        env = _environment(kind)
        return env if env.base and env.model else None
    directory, profiles, resolved = bound
    if kind not in resolved:
        resolved[kind] = _resolve(directory, kind, profiles[kind])
    return resolved[kind]
