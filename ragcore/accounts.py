"""Manage individual workspace accounts using the same database as the server."""

import argparse
import getpass
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import unicodedata
import warnings
from pathlib import Path


_ITERATIONS = 600_000
_USER = re.compile(r"[\w][\w.-]{0,63}\Z", re.UNICODE)


def normalize_username(value):
    name = unicodedata.normalize("NFKC", value).strip().casefold()
    if not _USER.fullmatch(name):
        raise ValueError("用户名须为 1–64 个字母、数字、下划线、点或连字符，首字符不能是点或连字符")
    return name


def ensure_schema(conn):
    """Run in Store.initialize's write transaction after creating the sessions table."""
    conn.execute("""CREATE TABLE IF NOT EXISTS accounts (
        username TEXT PRIMARY KEY, role TEXT NOT NULL CHECK(role IN ('reader','admin')),
        enabled INTEGER NOT NULL CHECK(enabled IN (0,1)), salt BLOB NOT NULL,
        password_hash BLOB NOT NULL, iterations INTEGER NOT NULL
    )""")
    if "principal" not in {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}:
        conn.execute("ALTER TABLE sessions ADD COLUMN principal TEXT REFERENCES accounts(username)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_principal ON sessions(principal)")


def _derive(password, salt, count):
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, count)


def password_matches(password, account):
    # Missing and disabled users perform the same work and receive the same HTTP response.
    salt = account["salt"] if account else b"\0" * 16
    rounds = account["iterations"] if account else _ITERATIONS
    expected = account["password_hash"] if account else b"\0" * 32
    matched = hmac.compare_digest(_derive(password, salt, rounds), expected)
    return bool(account and account["enabled"] and matched)


def manage(store, action, username, *, password=None, role=None):
    name = normalize_username(username)
    if action not in {"add", "passwd", "disable"} or (action == "add" and role not in {"admin", "reader"}):
        raise ValueError("无效的账号操作或角色")
    if action != "disable":
        if not isinstance(password, str) or not 12 <= len(password) <= 1024:
            raise ValueError("个人账号密码长度须为 12–1024 个字符")
        salt = secrets.token_bytes(16)
        password_hash = _derive(password, salt, _ITERATIONS)
    with store.db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if action == "add":
            try:
                conn.execute("""INSERT INTO accounts(username,role,enabled,salt,password_hash,iterations)
                             VALUES (?,?,1,?,?,?)""", (name, role, salt, password_hash, _ITERATIONS))
            except sqlite3.IntegrityError as exc:
                raise ValueError("该用户名已存在") from exc
        else:
            if action == "disable":
                cursor = conn.execute("UPDATE accounts SET enabled=0 WHERE username=?", (name,))
            else:
                cursor = conn.execute("""UPDATE accounts SET salt=?, password_hash=?, iterations=?
                                      WHERE username=?""", (salt, password_hash, _ITERATIONS, name))
            if not cursor.rowcount:
                raise ValueError("该用户名不存在")
            conn.execute("DELETE FROM sessions WHERE principal=?", (name,))
        store.audit(conn, "cli", "account." + action, "user/" + name)
    return name


def _prompt_password():
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        first = getpass.getpass("新密码（至少 12 个字符）: ")
        second = getpass.getpass("再次输入新密码: ")
    if first != second:
        raise ValueError("两次输入的密码不一致")
    return first


def main(argv=None):
    from .storage import Store

    parser = argparse.ArgumentParser(description="管理当前知识工作区的个人账号")
    commands = parser.add_subparsers(dest="action", required=True)
    create = commands.add_parser("add", help="新建账号")
    create.add_argument("username")
    create.add_argument("--role", required=True, choices=("reader", "admin"))
    commands.add_parser("passwd", help="修改密码并撤销已有会话").add_argument("username")
    commands.add_parser("disable", help="停用账号并撤销已有会话").add_argument("username")
    args = parser.parse_args(argv)
    try:
        name = normalize_username(args.username)
        password = None if args.action == "disable" else _prompt_password()
        store = Store(os.getenv("RAG_DATA_DIR") or Path(__file__).resolve().parent.parent / "var")
        store.initialize()
        manage(store, args.action, name, password=password, role=getattr(args, "role", None))
    except (EOFError, getpass.GetPassWarning, KeyboardInterrupt):
        parser.exit(2, "请在可隐藏输入密码的交互式终端执行此命令。\n")
    except ValueError as exc:
        parser.exit(2, f"{exc}\n")
    print(f"已完成 {args.action} 操作，账号 {name}")


if __name__ == "__main__":
    main()
