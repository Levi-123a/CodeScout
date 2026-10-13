"""Persisted repair orchestration, independent from automatic check scheduling."""

from collections import deque
from pathlib import Path

from .repair import RepairError, apply_repair, digest, generate_repair, safe_target
from .snapshot import capture, read_file


class RepairQueue:
    def __init__(self, store, lock, load_project, configuration, invalidate):
        self.store, self.lock = store, lock
        self.load_project, self.configuration, self.invalidate = load_project, configuration, invalidate
        self.jobs = deque()
        self.recover()

    def recover(self):
        for project in self.store.projects():
            root = Path(project["path"])
            for repair in self.store.repairs(project["id"], public=False):
                if repair.get("stage") != "interrupted" or not repair.get("changes"):
                    continue
                applied = []
                for change in repair["changes"]:
                    try:
                        if digest(read_file(root, safe_target(root, change["path"]))) == change["after_hash"]:
                            applied.append(change["path"])
                    except (ValueError, OSError):
                        pass
                self.store.update_repair(repair["id"], applied=applied, verification={"status": "not_configured"})

    def queue(self, project_id, finding_id):
        with self.lock:
            project, config = self.load_project(project_id), self.store.model()
            if not project["allow_send"] or not config:
                raise ValueError("请先配置模型并允许发送项目上下文")
            finding = next((item for item in self.store.findings(project_id) if item["id"] == finding_id), None)
            if not finding or finding["stale"] or finding["status"] != "open":
                raise ValueError("请先检查当前源码并选择未过期的待处理问题")
            snapshot = capture(Path(project["path"]), project["excludes"])
            source_run = next((run for run in self.store.runs(project_id) if run["id"] == finding["run_id"]), None)
            if not source_run or source_run["digest"] != snapshot.digest:
                raise ValueError("源码已变化，请等待自动复查后再生成修复")
            if any(item["status"] in {"queued", "running"} for item in self.store.repairs(project_id)):
                raise ValueError("该项目已有修复任务，请等待完成")
            signature = self.configuration(project, config)
            repair = self.store.create_repair(project_id, {"finding_id": finding_id, "configuration": signature, "source_digest": snapshot.digest})
            self.jobs.append({"mode": "generate", "repair": repair, "project": project, "config": config, "snapshot": snapshot, "finding": finding})
            return {key: repair[key] for key in ("id", "status", "stage")}

    def queue_apply(self, project_id, repair_id):
        with self.lock:
            repair = self.store.repair(project_id, repair_id)
            if repair["status"] != "ready":
                raise ValueError("只有已生成且未应用的修复可以应用")
            self.store.update_repair(repair_id, status="queued", stage="applying")
            self.jobs.append({"mode": "apply", "repair": repair})
            return {"id": repair_id, "status": "queued", "stage": "applying"}

    def remove(self, project_id):
        self.jobs = deque(job for job in self.jobs if job["repair"]["project_id"] != project_id)

    def process_one(self):
        with self.lock:
            if not self.jobs:
                return False
            job = self.jobs.popleft()
            repair = job["repair"]
            project_id, repair_id = repair["project_id"], repair["id"]
            self.store.update_repair(repair_id, status="running", stage="reading" if job["mode"] == "generate" else "applying")
        def current():
            project = self.load_project(project_id)
            config = self.store.model()
            if not project["allow_send"] or self.configuration(project, config) != repair["configuration"]:
                raise RepairError("项目约定、范围或模型配置已变化，请重新生成修复")
            return project, config
        try:
            project, config = current()
            root = Path(project["path"])
            if job["mode"] == "generate":
                events = []
                def event(item):
                    current()
                    events.append(item)
                    self.store.update_repair(repair_id, events=list(events), stage="reading")
                def stage(value):
                    current()
                    self.store.update_repair(repair_id, stage=value)
                proposal = generate_repair(root, job["snapshot"], job["finding"], config, project["custom_rules"], event, stage)
                current()
                proposal.pop("id")
                self.store.update_repair(repair_id, **proposal)
            else:
                def applied(paths):
                    self.store.update_repair(repair_id, applied=paths, stage="verifying" if len(paths) == len(repair["changes"]) else "applying")
                result = apply_repair(root, repair, self.store.path.parent / "repair-backups", project.get("validation_command", ""), applied, [config.get("api_key", "")])
                self.store.update_repair(repair_id, **result)
                self.invalidate(project_id)
        except (ValueError, OSError) as error:
            self.store.update_repair(repair_id, status="conflict" if job["mode"] == "apply" else "failed", stage="done", error=str(error) if isinstance(error, (RepairError, ValueError)) else "修复无法完成，请检查项目状态")
        except Exception:
            self.store.update_repair(repair_id, status="failed", stage="done", error="修复执行失败，未自动继续应用")
        return True
