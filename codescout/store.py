"""SQLite state. Connections are short-lived and safe across service threads."""

import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat()


def encode(value):
    return json.dumps(value, ensure_ascii=False)


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS settings (name TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS projects (id TEXT PRIMARY KEY, path TEXT UNIQUE NOT NULL, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    created_at TEXT NOT NULL, finished_at TEXT, digest TEXT NOT NULL, check_key TEXT NOT NULL,
                    status TEXT NOT NULL, stale INTEGER NOT NULL DEFAULT 0, snapshot TEXT NOT NULL, result TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS run_project ON runs(project_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS findings (id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    run_id TEXT NOT NULL, identity TEXT NOT NULL, status TEXT NOT NULL, data TEXT NOT NULL,
                    UNIQUE(project_id, identity));
            """)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def projects(self):
        with self.connection() as db:
            return [json.loads(row["data"]) for row in db.execute("SELECT data FROM projects ORDER BY rowid")]

    def project(self, project_id):
        with self.connection() as db:
            row = db.execute("SELECT data FROM projects WHERE id=?", (project_id,)).fetchone()
        if not row:
            raise ValueError("项目不存在")
        return json.loads(row["data"])

    def add_project(self, path, name, excludes, custom_rules, allow_send):
        project = {"id": uuid.uuid4().hex, "path": path, "name": name, "excludes": excludes, "custom_rules": custom_rules,
                   "allow_send": allow_send, "paused": False, "status": "waiting", "error": "", "created_at": now()}
        try:
            with self.connection() as db:
                db.execute("INSERT INTO projects VALUES (?,?,?)", (project["id"], path, encode(project)))
        except sqlite3.IntegrityError as error:
            raise ValueError("该项目已接入") from error
        return project

    def update_project(self, project_id, **changes):
        with self.connection() as db:
            row = db.execute("SELECT data FROM projects WHERE id=?", (project_id,)).fetchone()
            if not row:
                raise ValueError("项目不存在")
            project = json.loads(row["data"])
            project.update(changes)
            db.execute("UPDATE projects SET data=? WHERE id=?", (encode(project), project_id))
        return project

    def delete_project(self, project_id):
        with self.connection() as db:
            db.execute("DELETE FROM projects WHERE id=?", (project_id,))

    def model(self):
        with self.connection() as db:
            row = db.execute("SELECT value FROM settings WHERE name='model'").fetchone()
        return json.loads(row["value"]) if row else {}

    def configure_model(self, config):
        previous = self.model()
        config = dict(config)
        if not config.get("api_key"):
            config["api_key"] = previous.get("api_key", "")
        config["version"] = previous.get("version", 0) + 1
        with self.connection() as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES ('model',?)", (encode(config),))

    def public_model(self):
        config = self.model()
        key = config.pop("api_key", "")
        return {**config, "has_key": bool(key)}

    def create_run(self, project_id, digest, check_key, snapshot):
        run = {"id": uuid.uuid4().hex, "project_id": project_id, "created_at": now(), "digest": digest, "check_key": check_key, "status": "running"}
        with self.connection() as db:
            db.execute("INSERT INTO runs (id,project_id,created_at,digest,check_key,status,snapshot,result) VALUES (?,?,?,?,?,?,?,?)",
                       (run["id"], project_id, run["created_at"], digest, check_key, "running", encode(snapshot), "{}"))
        return run

    def finish_run(self, run_id, result):
        with self.connection() as db:
            run = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if not run:  # Project may be removed while a request is running.
                return
            status = "superseded" if run["stale"] else result["status"]
            db.execute("UPDATE runs SET status=?,finished_at=?,result=? WHERE id=?", (status, now(), encode(result), run_id))
            if result["status"] not in {"completed", "partial"} or run["stale"]:
                return
            checked = set(result.get("coverage", {}).get("checked", []))
            deleted = set(result.get("coverage", {}).get("deleted", []))
            identities = set()
            occurrences = {}
            for finding in sorted(result.get("findings", []), key=lambda item: (item.get("path", ""), item.get("line", 0))):
                signature = encode([finding.get("path"), finding.get("category"), finding.get("title"), finding.get("rule"), finding.get("evidence"), [(item.get("path"), item.get("evidence")) for item in finding.get("related", [])]])
                ordinal = occurrences.get(signature, 0)
                occurrences[signature] = ordinal + 1
                identity = hashlib.sha256(encode([signature, ordinal]).encode()).hexdigest()
                identities.add(identity)
                existing = db.execute("SELECT id,status FROM findings WHERE project_id=? AND identity=?", (run["project_id"], identity)).fetchone()
                finding_id = existing["id"] if existing else uuid.uuid4().hex
                status = "ignored" if existing and existing["status"] == "ignored" else "open"
                db.execute("INSERT OR REPLACE INTO findings VALUES (?,?,?,?,?,?)", (finding_id, run["project_id"], run_id, identity, status, encode(finding)))
            for old in db.execute("SELECT * FROM findings WHERE project_id=?", (run["project_id"],)).fetchall():
                location = json.loads(old["data"]).get("path")
                if result["status"] == "completed" and old["identity"] not in identities and location in checked | deleted and old["status"] != "ignored":
                    db.execute("UPDATE findings SET status='resolved',run_id=? WHERE id=?", (run_id, old["id"]))

    def runs(self, project_id):
        with self.connection() as db:
            rows = db.execute("SELECT * FROM runs WHERE project_id=? ORDER BY created_at DESC LIMIT 100", (project_id,)).fetchall()
        return [{**{key: row[key] for key in row.keys() if key != "snapshot"}, "result": json.loads(row["result"])} for row in rows]

    def baseline(self, project_id):
        with self.connection() as db:
            row = db.execute("SELECT * FROM runs WHERE project_id=? AND status='completed' ORDER BY created_at DESC LIMIT 1", (project_id,)).fetchone()
        return {**dict(row), "snapshot": json.loads(row["snapshot"])} if row else None

    def findings(self, project_id):
        with self.connection() as db:
            rows = db.execute("SELECT f.*,r.stale FROM findings f JOIN runs r ON f.run_id=r.id WHERE f.project_id=? ORDER BY f.rowid DESC", (project_id,)).fetchall()
        return [{**json.loads(row["data"]), "id": row["id"], "status": row["status"], "stale": bool(row["stale"]), "run_id": row["run_id"]} for row in rows]

    def set_finding_status(self, project_id, finding_id, status):
        if status not in {"open", "ignored"}:
            raise ValueError("仅支持标记待处理或忽略")
        with self.connection() as db:
            db.execute("UPDATE findings SET status=? WHERE id=? AND project_id=?", (status, finding_id, project_id))

    def mark_stale(self, project_id):
        with self.connection() as db:
            db.execute("UPDATE runs SET stale=1 WHERE project_id=?", (project_id,))

    def mark_run_stale(self, run_id):
        with self.connection() as db:
            db.execute("UPDATE runs SET stale=1 WHERE id=?", (run_id,))

    def recover(self):
        with self.connection() as db:
            db.execute("UPDATE runs SET status='failed',finished_at=?,result=? WHERE status='running'", (now(), encode({"status": "failed", "error": "服务中断，等待重新检查", "findings": [], "coverage": {}})))
