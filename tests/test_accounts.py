import os
import sqlite3
import tempfile
import unittest
from http.cookies import SimpleCookie
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from main import create_app
from ragcore import access, accounts
from ragcore.storage import Store


class AccountTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = Store(temp.name)
        self.store.initialize(False)
        environment = patch.dict(os.environ, {"RAG_ADMIN_PASSWORD": "", "RAG_READER_PASSWORD": ""})
        environment.start()
        self.addCleanup(environment.stop)
        self.app = SimpleNamespace(state=SimpleNamespace(store=self.store))
        if self._testMethodName != "test_named_login_and_stable_history_identity":
            # Most cases exercise role/session behavior; keep one production-cost password test.
            fast_hash = patch.object(accounts, "_ITERATIONS", 3_000)
            fast_hash.start()
            self.addCleanup(fast_hash.stop)

    def request(self, cookie="", peer="127.0.0.1"):
        headers = [(b"host", b"127.0.0.1")]
        if cookie:
            headers.append((b"cookie", f"rag_session={cookie}".encode()))
        return Request({"type": "http", "app": self.app, "scheme": "http", "path": "/",
                        "headers": headers, "client": (peer, 34567), "server": ("127.0.0.1", 8000),
                        "query_string": b""})

    def token(self, name, password):
        result = access.login(self.request(), password, name)
        self.assertEqual(result.status_code, 200)
        cookie = SimpleCookie()
        cookie.load(result.headers["set-cookie"])
        return cookie["rag_session"].value

    def test_named_login_and_stable_history_identity(self):
        local = self.request()
        self.assertFalse(access.account(local)["auth_required"])
        accounts.manage(self.store, "add", "数仓.AlIce", password="a long user passphrase", role="reader")
        self.assertIsNone(access.account(local)["role"])
        first = self.token("数仓.ALICE", "a long user passphrase")
        second = self.token("数仓.alice", "a long user passphrase")
        self.assertNotEqual(first, second)
        self.assertEqual(access.account(self.request(first)), access.account(self.request(second)))
        identity = access.reader(self.request(second))
        self.assertEqual(identity["owner"], "user/数仓.alice")
        self.assertEqual(access.audit_actor(identity), "user/数仓.alice")
        self.assertEqual(identity["username"], "数仓.alice")
        with self.assertRaises(HTTPException) as denied:
            access.admin(self.request(second))
        self.assertEqual(denied.exception.status_code, 403)
        with self.store.db() as conn:
            row = conn.execute("SELECT salt,password_hash,iterations FROM accounts").fetchone()
            self.assertEqual((len(row["salt"]), len(row["password_hash"])), (16, 32))
            self.assertEqual(row["iterations"], 600_000)
            self.assertEqual(conn.execute("SELECT actor FROM audit WHERE action='session.login' LIMIT 1").fetchone()[0],
                             "user/数仓.alice")

    def test_revoke_sessions_and_prevent_local_downgrade(self):
        old, new = "long original passphrase", "long replacement passphrase"
        accounts.manage(self.store, "add", "admin", role="admin", password=old)
        token = self.token("admin", old)
        accounts.manage(self.store, "passwd", "admin", password=new)
        self.assertIsNone(access.account(self.request(token))["role"])
        self.assertEqual(access.login(self.request(), old, "admin").status_code, 401)
        new_token = self.token("admin", new)
        self.assertEqual(access.admin(self.request(new_token))["role"], "admin")
        accounts.manage(self.store, "disable", "admin")
        self.assertTrue(access.account(self.request())["auth_required"])
        self.assertIsNone(access.account(self.request(new_token))["role"])
        self.assertEqual(access.login(self.request(), new, "admin").status_code, 401)

    def test_legacy_roles_and_username_never_falls_back_to_shared_password(self):
        with patch.dict(os.environ, {"RAG_ADMIN_PASSWORD": "shared-admin", "RAG_READER_PASSWORD": "shared-reader"}):
            token = self.token(None, "shared-admin")
            identity = access.admin(self.request(token))
            self.assertEqual(len(identity["owner"]), 64)
            self.assertIsNone(identity["username"])
            self.assertEqual(access.reader(self.request(self.token(None, "shared-reader")))["role"], "reader")
            self.assertEqual(access.login(self.request(), "shared-admin", "missing").status_code, 401)
            self.assertEqual(access.login(self.request(), "shared-admin", "bad/user").status_code, 401)

    def test_legacy_schema_migrates_in_place(self):
        with sqlite3.connect(":memory:") as conn:
            conn.execute("CREATE TABLE sessions(digest TEXT PRIMARY KEY,role TEXT,expires INTEGER)")
            conn.execute("INSERT INTO sessions VALUES ('old','reader',123)")
            accounts.ensure_schema(conn)
            accounts.ensure_schema(conn)
            self.assertEqual(conn.execute("SELECT * FROM sessions").fetchone(), ("old", "reader", 123, None))

    def test_concurrent_rotation_wins_over_validated_login(self):
        name = "u"
        old, new = "initial long password", "new long password"
        accounts.manage(self.store, "add", name, role="reader", password=old)
        real_check = accounts.password_matches

        def rotate_between_check_and_issue(password, snapshot):
            verified = real_check(password, snapshot)
            accounts.manage(self.store, "passwd", name, password=new)
            return verified

        with patch.object(access, "password_matches", side_effect=rotate_between_check_and_issue):
            self.assertEqual(access.login(self.request(), old, name).status_code, 401)
        self.assertEqual(access.account(self.request(self.token(name, new)))["username"], name)

    def test_shared_and_named_attempts_use_same_throttle(self):
        with patch.dict(os.environ, {"RAG_ADMIN_PASSWORD": "admin"}):
            for _ in range(10):
                self.assertEqual(access.login(self.request(), "incorrect").status_code, 401)
            with self.assertRaises(HTTPException) as limited:
                access.login(self.request(), "bad", "test")
            self.assertEqual(limited.exception.status_code, 429)

    def test_cli_uses_same_database_and_hidden_confirmation(self):
        env = {"RAG_DATA_DIR": str(self.store.path.parent), "RAG_SEED_DEMO": "0"}
        with patch.dict(os.environ, env):
            with patch("getpass.getpass", side_effect=["long CLI user pass", "long CLI user pass"]):
                with patch("builtins.print"):
                    accounts.main(["add", "cli.user", "--role", "admin"])
            self.assertEqual(access.admin(self.request(self.token("cli.user", "long CLI user pass")))["username"],
                             "cli.user")

    def test_workspace_account_lifecycle(self):
        base = "/api/admin/accounts"
        password = "initial long passphrase"
        new_password = "updated long passphrase"
        app = create_app(self.store.path.parent, seed=False)
        with (TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 8000)) as admin,
              TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 8001)) as reader,
              TestClient(app) as guest):
            self.assertEqual(guest.get(base).status_code, 401)
            self.assertEqual(admin.get(base).json(), [])
            root = {"username": "Operator", "role": "admin", "password": password}
            self.assertEqual(admin.post(base, json={**root, "role": "reader"}).status_code, 409)
            self.assertEqual(admin.post(base, json=root).json(), {
                "username": "operator", "role": "admin", "enabled": True, "login_required": True})
            self.assertEqual(admin.get(base).status_code, 401)
            self.assertEqual(admin.post("/api/login", json=root).status_code, 200)
            self.assertEqual(admin.post(base, json=root).status_code, 409)
            self.assertEqual(admin.put(base + "/operator", json={"role": "reader", "enabled": True}).status_code, 409)

            member = {"username": "测试.读者", "role": "reader", "password": password}
            member_path = base + "/" + member["username"]
            self.assertEqual(admin.post(base, json=member).status_code, 201)
            self.assertEqual(admin.get(base).json(), [
                {"username": "operator", "role": "admin", "enabled": True},
                {"username": member["username"], "role": "reader", "enabled": True}])
            self.assertEqual(reader.post("/api/login", json=member).status_code, 200)
            self.assertEqual(reader.get(base).status_code, 403)
            self.assertEqual(reader.put(member_path, json={"role": "admin", "enabled": True}).status_code, 403)
            self.assertEqual(admin.put(member_path, json={"role": "reader", "enabled": False}).status_code, 200)
            self.assertFalse(reader.get("/api/session").json()["authenticated"])
            self.assertEqual(reader.post("/api/login", json=member).status_code, 401)
            self.assertEqual(admin.put(member_path, json={"role": "reader", "enabled": True}).status_code, 200)
            self.assertEqual(reader.post("/api/login", json=member).status_code, 200)
            self.assertEqual(admin.put(member_path + "/password", json={"password": new_password}).status_code, 204)
            self.assertFalse(reader.get("/api/session").json()["authenticated"])
            self.assertEqual(reader.post("/api/login", json={**member, "password": new_password}).status_code, 200)
            own_password = "/api/account/password"
            self.assertEqual(reader.put(own_password, json={"current_password": password, "password": password}).status_code, 422)
            self.assertTrue(reader.get("/api/session").json()["authenticated"])
            self.assertEqual(reader.put(own_password, json={"current_password": new_password, "password": password}).status_code, 204)
            self.assertFalse(reader.get("/api/session").json()["authenticated"])
            self.assertEqual(reader.post("/api/login", json=member).status_code, 200)
