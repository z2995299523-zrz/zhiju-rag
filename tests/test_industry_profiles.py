"""Regression cases for first-run industry templates and source-backed examples."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ragcore.connectors import validate_config
from ragcore.documents import parse_document
from ragcore.engine import query_examples
from ragcore.storage import Store, pack


DOCUMENT = "交接细则.txt"
CONNECTOR = "交接台账"
DOC_QUESTION = "交接时要登记哪些字段？"
DATA_QUESTION = "台账中有多少份交接单？"
MIXED_QUESTION = "交接单数量是多少，登记口径是什么？"


def example_profile():
    return {
        "name": "交接管理",
        "documents": {DOCUMENT: "交接登记要求填写设备编号和交接日期。"},
        "connectors": [{"name": CONNECTOR, "url": "https://records.example.test/handover"}],
        "examples": [
            {"mode": "documents", "question": DOC_QUESTION, "document": DOCUMENT},
            {"mode": "data", "question": DATA_QUESTION, "connector": CONNECTOR},
            {"mode": "mixed", "question": MIXED_QUESTION, "document": DOCUMENT, "connector": CONNECTOR},
        ],
    }


def questions(store):
    return {item["mode"]: item["question"] for item in query_examples(store)["examples"]}


class IndustryProfileTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.store = Store(self.root / "workspace")
        env = patch.dict(os.environ, {
            "RAG_SEED_DEMO": "", "RAG_INDUSTRY_PROFILE": "", "RAG_ALLOWED_HOSTS": "records.example.test",
        })
        env.start()
        self.addCleanup(env.stop)

    def write_profile(self, profile):
        path = self.root / "行业示例.json"
        path.write_text(json.dumps(profile, ensure_ascii=False), encoding="utf-8")
        os.environ["RAG_INDUSTRY_PROFILE"] = str(path)

    def test_same_search_term_uses_only_its_own_industry_vocabulary(self):
        from ragcore.retrieval import rank_document_chunks
        for label, target in [("制造", "设备交接记录"), ("仓储", "收货交接记录")]:
            self.write_profile({"name": label, "documents": {
                "设备.txt":"设备交接记录由车间主任签字。",
                "收货.txt":"收货交接记录由仓库主管签字。"}, "document_terms":{"流转单":[target]}})
            store = Store(self.root / label)
            store.initialize()
            with patch("ragcore.models.embedding_enabled", return_value=False):
                self.assertIn(target, rank_document_chunks(store, "流转单由谁签字？").ranked[0]["text"])

    def test_default_workspace_is_empty(self):
        self.store.initialize()
        self.assertEqual(questions(self.store), {})
        self.assertEqual(self.store.connectors(), [])
        with self.store.db() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM documents").fetchone()[0], 0)

    def test_both_builtins_seed_only_their_own_sources(self):
        cases = (
            ("research-finance", {"科研项目经费报销指引.md", "预算执行口径说明.md", "数据接口目录.csv"},
             {"科研预算查询"}, {"documents", "data", "mixed"}),
            ("manufacturing", {"设备交接记录说明.md"}, set(), {"documents"}),
        )
        for profile, documents, connectors, modes in cases:
            with self.subTest(profile=profile), patch.dict(os.environ, {"RAG_INDUSTRY_PROFILE": profile}):
                store = Store(self.root / profile)
                store.initialize()
                with store.db() as conn:
                    names = {r[0] for r in conn.execute("SELECT filename FROM documents WHERE active=1")}
                self.assertEqual(names, documents)
                self.assertEqual({item["name"] for item in store.connectors()}, connectors)
                self.assertEqual(set(questions(store)), modes)

    def test_legacy_finance_workspace_survives_changed_profile_and_seed_flags(self):
        # Legacy workspaces have an initialized marker, but no industry metadata.
        with patch.dict(os.environ, {"RAG_SEED_DEMO": "1"}):
            self.store.initialize()
        with self.store.db() as conn:
            conn.execute("DELETE FROM settings WHERE name IN ('industry_name','industry_examples')")
            originals = list(conn.execute("SELECT id,filename,sha256 FROM documents ORDER BY id"))
        previous_questions = questions(self.store)
        self.assertIn("报销", previous_questions["documents"])
        saved = self.store.record("reader", {
            "question": "已归档的问题", "intent": "documents", "status": "no_evidence", "answer": "没有足够原文。",
        })
        os.environ.update(RAG_INDUSTRY_PROFILE="manufacturing", RAG_SEED_DEMO="0")
        restarted = Store(self.store.path.parent)
        restarted.initialize()
        self.assertEqual(questions(restarted), previous_questions)
        with restarted.db() as conn:
            self.assertEqual(list(conn.execute("SELECT id,filename,sha256 FROM documents ORDER BY id")), originals)
            history = conn.execute("SELECT result FROM history WHERE id=?", (saved["id"],)).fetchone()
            self.assertEqual(json.loads(history[0])["answer"], saved["answer"])
        self.assertEqual([item["name"] for item in restarted.connectors()], ["科研预算查询"])

    def test_bad_reference_rolls_back_all_seed_writes_and_allows_retry(self):
        profile = example_profile()
        profile["examples"][-1]["document"] = "缺失的文件.txt"
        self.write_profile(profile)
        with self.assertRaisesRegex(ValueError, "文档不在模板中"):
            self.store.initialize()
        with self.store.db() as conn:
            for table in ("documents", "chunks", "chunk_index", "connectors", "audit", "settings"):
                self.assertEqual(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 0, table)
        self.write_profile(example_profile())
        restarted = Store(self.store.path.parent)
        restarted.initialize()
        restarted.initialize()
        self.assertEqual(set(questions(restarted).values()), {DOC_QUESTION, DATA_QUESTION, MIXED_QUESTION})
        with restarted.db() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM documents").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM connectors").fetchone()[0], 1)

    def test_new_document_version_and_removed_connector_hide_dependent_examples(self):
        self.write_profile(example_profile())
        self.store.initialize()
        content = "本版本新增保管周期的说明。".encode()
        with self.store.db() as conn:
            self.store.insert_document(conn, DOCUMENT, content, parse_document(DOCUMENT, content))
        updated = questions(self.store)
        self.assertEqual(updated["data"], DATA_QUESTION)
        self.assertNotIn(DOC_QUESTION, updated.values())
        self.assertNotIn(MIXED_QUESTION, updated.values())
        with self.store.db() as conn:
            conn.execute("DELETE FROM connectors")
        self.assertEqual(set(questions(self.store)), {"documents"})

    def test_example_can_reference_an_older_template_document(self):
        profile = example_profile()
        for i in range(25):
            profile["documents"][f"资料{i}.txt"] = f"这份资料的编号是 {i}。"
        self.write_profile(profile)
        self.store.initialize()
        self.assertEqual(questions(self.store)["documents"], DOC_QUESTION)
        self.assertEqual(questions(self.store)["mixed"], MIXED_QUESTION)

    def test_unrelated_upload_does_not_pair_the_new_document_with_a_business_connector(self):
        self.write_profile(example_profile())
        self.store.initialize()
        self.store.ingest("天气公报.txt", "沿岸地区多云。".encode(), "test")
        result = questions(self.store)
        self.assertEqual(result["documents"], DOC_QUESTION)
        self.assertEqual(result["mixed"], MIXED_QUESTION)

    def test_example_can_reference_the_first_of_multiple_template_connectors(self):
        profile = example_profile()
        profile["connectors"].append({"name": "供应商台账", "url": "https://records.example.test/vendors"})
        self.write_profile(profile)
        self.store.initialize()
        self.assertEqual(questions(self.store)["data"], DATA_QUESTION)
        self.assertEqual(questions(self.store)["mixed"], MIXED_QUESTION)

    def test_changed_connector_definition_cannot_reuse_its_curated_examples(self):
        self.write_profile(example_profile())
        self.store.initialize()
        changed = validate_config({"name": CONNECTOR, "url": "https://records.example.test/equipment"})
        with self.store.db() as conn:
            conn.execute("UPDATE connectors SET config=? WHERE name=?", (pack(changed), CONNECTOR))
        updated = questions(self.store)
        self.assertEqual(updated["documents"], DOC_QUESTION)
        self.assertNotIn(DATA_QUESTION, updated.values())
        self.assertNotIn(MIXED_QUESTION, updated.values())


if __name__ == "__main__":
    unittest.main()
