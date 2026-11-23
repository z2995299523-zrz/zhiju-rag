"""Durable identity for a source revision and the rules used to index it."""
from pydantic import BaseModel, ConfigDict, Field, model_validator

PARSER_VERSION = "5"


class ImportOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    include_hidden: bool = False
    headerless: bool = False
    header_rows: list[int] = Field(default_factory=list, max_length=12)
    ocr_pages: list[int] = Field(default_factory=list, max_length=5)

    @model_validator(mode="after")
    def valid_rows(self):
        rows = self.header_rows
        if rows and (self.headerless or not 1 <= rows[0] <= rows[-1] <= 50000 or
                     rows != list(range(rows[0], rows[0] + len(rows)))):
            raise ValueError("表头须为从 1 开始计数的连续行，并与无表头模式互斥")
        if any(p < 1 or p > 1200 for p in self.ocr_pages) or len(set(self.ocr_pages)) != len(self.ocr_pages):
            raise ValueError("补充 OCR 的页码须在 1 至 1200 之间，最多选择 5 个不同的页面")
        return self


DOCUMENT_SCHEMA = """(
    id INTEGER PRIMARY KEY, filename TEXT NOT NULL, size INTEGER NOT NULL,
    sha256 TEXT NOT NULL, chunk_count INTEGER NOT NULL, created_at TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1, content BLOB NOT NULL,
    source_key TEXT NOT NULL DEFAULT '', parser_version TEXT NOT NULL DEFAULT '1',
    recipe TEXT NOT NULL DEFAULT '{}', report TEXT NOT NULL DEFAULT '{}',
    warning_count INTEGER NOT NULL DEFAULT 0,
    UNIQUE(source_key, filename, sha256, parser_version, recipe)
)"""


def migrate_imports(conn):
    if "source_key" not in {r["name"] for r in conn.execute("PRAGMA table_info(documents)")}:
        # Rebuild the legacy UNIQUE(filename, sha256) index while retaining all
        # document IDs and bytes. The caller holds the write transaction.
        columns = "id,filename,size,sha256,chunk_count,created_at,active,content"
        conn.execute("CREATE TABLE revised_documents " + DOCUMENT_SCHEMA)
        conn.execute(f"INSERT INTO revised_documents({columns}) SELECT {columns} FROM documents")
        conn.execute("DROP TABLE documents")
        conn.execute("ALTER TABLE revised_documents RENAME TO documents")
        if conn.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError("文档版本迁移未通过引用校验")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS active_source ON documents(source_key,filename) WHERE active=1")
    fields = {r["name"] for r in conn.execute("PRAGMA table_info(chunks)")}
    if "context" not in fields:
        conn.execute("ALTER TABLE chunks ADD COLUMN context TEXT NOT NULL DEFAULT ''")
    if "metadata" not in fields:
        conn.execute("ALTER TABLE chunks ADD COLUMN metadata TEXT NOT NULL DEFAULT '{}'")
