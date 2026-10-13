"""Observation is independent of successful checks; failures retain pending work."""

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from .checker import check_snapshot
from .conventions import RULES_FILENAME, load_rules, save_rules
from .repair_service import RepairQueue
from .snapshot import capture
from .verification import command_args


def configuration_key(project, model):
    return hashlib.sha256(json.dumps([project["excludes"], project["custom_rules"], project["allow_send"], project.get("validation_command", ""), model.get("version", 0)], ensure_ascii=False).encode()).hexdigest()


class Service:
    def __init__(self, store, debounce_seconds=1, poll_interval=2):
        self.store = store
        self.debounce_seconds = debounce_seconds
        self.poll_interval = poll_interval
        self.lock = threading.RLock()
        self.pending = {}
        self.observed = {}
        self.active = set()
        self.stop_event = threading.Event()
        self.threads = []
        self.store.recover()
        self.repairs = RepairQueue(store, self.lock, self.project_with_rules, configuration_key, self.invalidate)

    def project_with_rules(self, project_id):
        project = self.store.project(project_id)
        root = Path(project["path"])
        legacy = project.get("custom_rules", "")
        rules = load_rules(root)
        return {**project, "name": root.name, "custom_rules": rules or (legacy if not (root / RULES_FILENAME).exists() else ""),
                "rules_file": RULES_FILENAME, "rules_migration_needed": bool(legacy and not (root / RULES_FILENAME).exists()), "validation_command": project.get("validation_command", "")}

    def invalidate(self, project_id):
        with self.lock:
            self.observed.pop(project_id, None)
            self.pending.pop(project_id, None)
            self.store.mark_stale(project_id)
            self.store.update_project(project_id, processed_key="", status="waiting" if not self.store.project(project_id)["paused"] else "paused")

    def add_project(self, data):
        path = data.get("path")
        if not isinstance(path, str) or not path.strip():
            raise ValueError("请输入本地项目文件夹的绝对路径")
        root = Path(path).expanduser()
        if not root.is_absolute() or not root.is_dir():
            raise ValueError("项目必须是可访问的绝对文件夹路径")
        root = root.resolve()
        excludes = data.get("excludes", [])
        if not isinstance(excludes, list) or len(excludes) > 100 or any(not isinstance(item, str) or len(item) > 500 for item in excludes):
            raise ValueError("排除项必须是最多 100 项的路径模式列表")
        name = root.name
        rules = data.get("custom_rules", "")
        if not isinstance(name, str) or len(name) > 200 or not isinstance(rules, str) or len(rules) > 20000:
            raise ValueError("项目名称或附加约定无效或过长")
        if not isinstance(data.get("allow_send", False), bool):
            raise ValueError("代码发送许可必须明确选择")
        for existing in self.store.projects():
            if os.path.normcase(existing["path"]) == os.path.normcase(str(root)):
                raise ValueError("该项目已接入")
        command = data.get("validation_command", "")
        if not isinstance(command, str):
            raise ValueError("验证命令必须为文本")
        if command:
            command_args(command)
        if rules:
            save_rules(root, rules, data.get("rules_expected"))
        project = self.store.add_project(str(root), name.strip(), excludes, "", data.get("allow_send", False))
        self.store.update_project(project["id"], validation_command=command)
        return self.project_with_rules(project["id"])

    def configure_model(self, data):
        base_url, model, key = data.get("base_url", ""), data.get("model", ""), data.get("api_key", "")
        if not all(isinstance(value, str) for value in (base_url, model, key)):
            raise ValueError("模型地址、名称和密钥必须为文本")
        url = urlsplit(base_url.strip())
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError("请输入不含凭据或查询参数的 HTTP(S) API 基础地址")
        if url.scheme == "http" and url.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("外部模型服务须使用 HTTPS，本机测试服务可使用 HTTP")
        if not model.strip() or len(model) > 200 or len(base_url) > 2048 or len(key) > 8192 or any(ord(char) < 32 for char in key):
            raise ValueError("模型名称或密钥格式无效")
        timeout, budget = data.get("timeout", 60), data.get("context_chars", 60000)
        if type(timeout) not in {int, float} or not 1 <= timeout <= 300 or type(budget) is not int or not 2000 <= budget <= 200000:
            raise ValueError("超时须为 1～300 秒，上下文预算须为 2000～200000 字符")
        with self.lock:
            self.store.configure_model({"base_url": base_url.strip().rstrip("/"), "model": model.strip(), "api_key": key.strip(), "timeout": timeout, "context_chars": budget})
            for project in self.store.projects():
                project_id = project["id"]
                self.store.mark_stale(project_id)
                self.observed.pop(project_id, None)
                self.pending.pop(project_id, None)
                if not project["paused"]:
                    self.store.update_project(project_id, status="waiting", error="")
        return self.store.public_model()

    def action(self, project_id, action):
        with self.lock:
            self.store.project(project_id)
            if action == "pause":
                self.pending.pop(project_id, None)
                return self.store.update_project(project_id, paused=True, status="paused", error="")
            if action not in {"resume", "retry"}:
                raise ValueError("未知项目操作")
            self.observed.pop(project_id, None)
            self.pending.pop(project_id, None)
            return self.store.update_project(project_id, paused=False, status="waiting", processed_key="", error="")

    def update_options(self, project_id, data):
        project = self.project_with_rules(project_id)
        allow_send = data.get("allow_send", project["allow_send"])
        rules = data.get("custom_rules", project["custom_rules"])
        excludes = data.get("excludes", project["excludes"])
        if not isinstance(allow_send, bool) or not isinstance(rules, str) or len(rules) > 20000:
            raise ValueError("代码发送许可或附加约定无效")
        if not isinstance(excludes, list) or len(excludes) > 100 or any(not isinstance(item, str) or len(item) > 500 for item in excludes):
            raise ValueError("排除项格式无效")
        command = data.get("validation_command", project.get("validation_command", ""))
        if not isinstance(command, str):
            raise ValueError("验证命令必须为文本")
        if command:
            command_args(command)
        with self.lock:
            if "custom_rules" in data:
                save_rules(Path(project["path"]), rules, data.get("rules_expected"))
            self.store.mark_stale(project_id)
            self.observed.pop(project_id, None)
            self.pending.pop(project_id, None)
            self.store.update_project(project_id, allow_send=allow_send, custom_rules="", validation_command=command, excludes=excludes, processed_key="", error="", status="paused" if project["paused"] else "waiting")
            return self.project_with_rules(project_id)

    def remove_project(self, project_id):
        with self.lock:
            self.pending.pop(project_id, None)
            self.observed.pop(project_id, None)
            self.repairs.remove(project_id)
            self.store.delete_project(project_id)

    def unavailable(self, project_id):
        """Discard a broken project's job without interrupting the shared worker."""
        self.pending.pop(project_id, None)
        self.observed.pop(project_id, None)
        self.store.mark_stale(project_id)
        try:
            self.store.update_project(project_id, status="unavailable", error="项目或约定文件不可读取，请检查路径和 UTF-8 编码")
        except ValueError:
            pass  # The project may have been removed during the read.

    def observe(self):
        model = self.store.model()
        for project in self.store.projects():
            project_id = project["id"]
            if project["paused"]:
                continue
            try:
                project = self.project_with_rules(project_id)
                if project["rules_migration_needed"]:
                    self.store.update_project(project_id, status="waiting_rules", error="请在项目设置中将原附加约定保存为 CODESCOUT_RULES.md")
                    continue
                configuration = configuration_key(project, model)
                snapshot = capture(Path(project["path"]), project["excludes"])
                key = hashlib.sha256((snapshot.digest + configuration).encode()).hexdigest()
                with self.lock:
                    current = self.project_with_rules(project_id)
                    if current["paused"]:
                        continue
                    if configuration_key(current, self.store.model()) != configuration:
                        continue  # Options changed while reading: discard the old scope.
                    if self.observed.get(project_id) == key:
                        # Clear a transient read error while preserving any pending check.
                        if current["status"] == "unavailable":
                            self.store.update_project(project_id, status="waiting" if project_id in self.pending else "completed", error="")
                        continue
                    prior_observation = self.observed.get(project_id)
                    self.observed[project_id] = key
                    if current.get("processed_key") == key and prior_observation is None and current["status"] in {"completed", "partial"} and project_id not in self.pending and project_id not in self.active:
                        continue
                    self.store.mark_stale(project_id)
                    self.pending[project_id] = {"snapshot": snapshot, "key": key, "configuration": configuration, "due": time.monotonic() + self.debounce_seconds, "attempt": 0}
                    state = "waiting_consent" if not project["allow_send"] else "waiting_model" if not model else "waiting"
                    self.store.update_project(project_id, status=state, error="", git=snapshot.git, observed_digest=snapshot.digest)
            except (ValueError, OSError):
                with self.lock:
                    self.pending.pop(project_id, None)
                    self.observed.pop(project_id, None)
                    try:
                        self.store.mark_stale(project_id)
                        self.store.update_project(project_id, status="unavailable", error="项目不可访问或读取期间发生变化，稍后自动核对")
                    except ValueError:
                        pass

    def process_one(self):
        with self.lock:
            selected = None
            model = self.store.model()
            for project_id, job in list(self.pending.items()):
                try:
                    project = self.project_with_rules(project_id)
                except (ValueError, OSError):
                    self.unavailable(project_id)
                    continue
                if configuration_key(project, model) != job["configuration"]:
                    self.pending.pop(project_id, None)
                    self.observed.pop(project_id, None)
                    continue
                if project["paused"] or project_id in self.active or not project["allow_send"] or not model or job["due"] > time.monotonic():
                    continue
                selected = project_id, job, project
                break
            if selected is None:
                return False
            project_id, job, project = selected
            self.pending.pop(project_id)
            self.active.add(project_id)
            run = self.store.create_run(project_id, job["snapshot"].digest, job["key"], job["snapshot"].files)
            self.store.update_project(project_id, status="running", error="")
        baseline = self.store.baseline(project_id)
        previous_result = json.loads(baseline["result"]) if baseline else {}
        previous_coverage = previous_result.get("coverage") if previous_result.get("configuration") == job["configuration"] else None
        # A changed citation invalidates the primary issue even if its own file is unchanged.
        required_paths = set()
        if baseline:
            for finding in self.store.findings(project_id):
                if any(baseline["snapshot"].get(citation["path"]) != job["snapshot"].files.get(citation["path"]) for citation in [finding, *finding.get("related", [])]):
                    required_paths.add(finding["path"])
        try:
            def current():
                with self.lock:
                    latest = self.project_with_rules(project_id)
                    return not latest["paused"] and self.observed.get(project_id) == job["key"] and configuration_key(latest, self.store.model()) == job["configuration"]
            def save_progress(progress):
                self.store.save_progress(run["id"], {**progress, "configuration": job["configuration"]})
            result = check_snapshot(job["snapshot"], baseline["snapshot"] if baseline else None, model, project["custom_rules"],
                                    resume=self.store.checkpoint(project_id, job["key"]), on_progress=save_progress, should_continue=current,
                                    previous_coverage=previous_coverage, required_paths=required_paths)
        except Exception:
            # Never persist arbitrary exception strings from providers or repository text.
            result = {"status": "failed", "error": "检查执行失败，等待重试", "findings": [], "coverage": {"checked": [], "uncovered": sorted(job["snapshot"].files)}}
        result["configuration"] = job["configuration"]
        if baseline and result.get("coverage", {}).get("reused"):
            result["coverage"]["reused_from_run"] = baseline["id"]
        with self.lock:
            self.active.discard(project_id)
            try:
                latest = self.project_with_rules(project_id)
            except (ValueError, OSError):
                self.unavailable(project_id)
                self.store.mark_run_stale(run["id"])
                self.store.finish_run(run["id"], result)
                return True
            obsolete = latest["paused"] or self.observed.get(project_id) != job["key"] or configuration_key(latest, self.store.model()) != job["configuration"]
            if obsolete:
                self.store.mark_run_stale(run["id"])
            self.store.finish_run(run["id"], result)
            if obsolete:
                return True
            if project_id in self.pending:
                return True
            if result["status"] == "failed":
                attempt = job["attempt"]
                delays = (5, 30, 120)
                if attempt < len(delays):
                    self.pending[project_id] = {**job, "due": time.monotonic() + delays[attempt], "attempt": attempt + 1}
                self.store.update_project(project_id, status="failed", error=result["error"], retry_remaining=max(0, len(delays) - attempt))
            else:
                self.store.update_project(project_id, status=result["status"], error="", processed_key=job["key"], last_checked=run["created_at"], retry_remaining=0)
        return True

    def state(self):
        projects = []
        for project in self.store.projects():
            try:
                project = self.project_with_rules(project["id"])
            except ValueError:
                project = {**project, "name": Path(project["path"]).name, "rules_file": RULES_FILENAME, "rules_error": "无法读取约定文件"}
            projects.append({**project, "findings": self.store.findings(project["id"]), "runs": self.store.runs(project["id"]), "repairs": self.store.repairs(project["id"])})
        return {"version": "0.2.0", "model": self.store.public_model(), "projects": projects, "capabilities": {"worktree": True, "local_commits": False, "remote": False, "repair": True}}

    def queue_repair(self, project_id, finding_id):
        return self.repairs.queue(project_id, finding_id)

    def queue_apply(self, project_id, repair_id):
        return self.repairs.queue_apply(project_id, repair_id)

    def process_repair_one(self):
        return self.repairs.process_one()

    def start(self):
        def observe_loop():
            while not self.stop_event.is_set():
                self.observe()
                self.stop_event.wait(self.poll_interval)
        def worker_loop():
            while not self.stop_event.is_set():
                if not self.process_repair_one():
                    self.process_one()
                self.stop_event.wait(0.25)
        for target in (observe_loop, worker_loop):
            thread = threading.Thread(target=target, daemon=True)
            thread.start()
            self.threads.append(thread)

    def stop(self):
        self.stop_event.set()
        for thread in self.threads:
            thread.join(timeout=3)
