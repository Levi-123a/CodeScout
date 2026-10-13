# 巡码首个可运行版本 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 完成阶段 1 的可运行闭环，并提供工作目录变化后的自动复查。

**Architecture:** 单机 Python 服务提供本机 HTTP API 和原生网页；SQLite 持久化项目、模型配置、检查快照及报告。后台观察工作目录并串行执行模型检查，浏览器关闭不影响任务。各模块分别负责快照、存储、模型协议、检查调度和 HTTP 交互。

**Tech Stack:** Python 3.11+ 标准库、SQLite、HTML/CSS/JavaScript；外部模型采用 OpenAI-compatible Chat Completions 协议。

**Spec:** `docs/superpowers/specs/2026-10-13-codescout-design.md`，以及 `docs/requirements.md` 中 FR-01～03、FR-06、FR-08～10；工作目录自动检测对应 FR-04 的第一项。

用户在 2026-10-13 要求根据文档开始编码，并确认 Python + SQLite + 原生网页方案。原文档的“仅交付文档”限制已被本次指令更新。当前交付按路线图首阶段执行；不将首阶段称为完整第一版。

## Global Constraints

- 本地后台服务 + 浏览器界面。
- 检测发现问题本身不会自动授权修改。
- 失败不能表现为“无问题”。
- 访问凭据不返回给浏览器页面，不写入项目源码、检查报告和普通日志。
- 关闭浏览器页面不应停止已运行服务的监控。
- 移除监控不得删除项目源文件。
- 修复不能静默覆盖用户并发编辑；此阶段不提供修改功能。

## 实现决定与当前边界

- 无 Git 目录可接入，但界面明确仅监控工作目录；Git 项目显示分支与 HEAD，提交与远程快照检查留在后续阶段。
- 自动读取快照内各目录的 AGENTS.md、CLAUDE.md，注明作用域；附加约定为项目配置文本。冲突由模型报告，不静默建立未确认的优先级。
- 默认每 2 秒核对、稳定 1 秒后检查；相同内容和检查配置去重。模型失败按 5/30/120 秒有限重试，配置变化立即重新检查。
- 排除依赖/构建目录、Git 元数据、常见凭据文件、二进制、符号链接；文件上限 256 KiB、快照上限 8 MiB、2000 文件，明确报告遗漏。
- 外部代码发送默认关闭，添加项目必须显式选择允许；模型密钥存储于本机用户数据目录的数据库，不通过网页回显。
- 模型输入预算默认 60000 字符，超出预算保存部分完成及未覆盖文件；未配置模型显示等待配置。
- 仅绑定 127.0.0.1；随机会话令牌保护 API；校验 Host/Origin，禁止第三方页面写入；请求大小与模型响应大小有限制。
- 模型只返回结构化问题，文件位置与引文需要核对快照；无效输出标记失败。无真实模型凭据时使用测试服务验证协议，不宣称真实语义质量已验收。

## Review Focus

- 不可访问、持续编辑或链接到项目外的文件：不得形成稳定成功报告或发送项目外内容。
- 无效/超时模型及包含凭据的错误：保留失败原因并脱敏，不推进成功基线。
- 上下文预算不足：准确展示未覆盖范围，不解决未检查的问题。
- 重启或配置变化：恢复待检查状态，保持报告历史，重新安排检查。
- 跨来源 HTTP 请求、恶意路径和 HTML：拒绝未授权访问，按纯文本展示模型输出。

### Task 1: 快照与持久化

**Files:** `codescout/snapshot.py`, `codescout/store.py`, `tests/test_snapshot.py`, `tests/test_store.py`, `.gitignore`, `pyproject.toml`。

**Interfaces:**
- `capture(root: Path, excludes: list[str]) -> Snapshot`；Snapshot 提供 `files`, `excluded`, `warnings`, `digest`, `git`。
- `Store(path: Path)`：项目 CRUD、模型配置版本、报告/快照持久化及重启任务恢复；公开配置移除密钥。

- [x] 编写测试：真实临时目录的文件变化、删除、约定、排除、二进制、链接、限制和真实临时数据库的持久化、脱敏、恢复。
- [x] `python -m unittest tests.test_snapshot tests.test_store -v`，确认功能缺失导致失败。
- [x] 实现稳定内容快照和 SQLite 事务接口；无 Git 情况可工作。
- [x] 同命令测试通过。

### Task 2: 外部模型检查

**Files:** `codescout/model.py`, `codescout/checker.py`, `tests/test_checker.py`。

**Interfaces:**
- `ModelClient.check(config: dict, context: dict) -> list[dict]`：HTTP 协议、超时、失败脱敏、格式与证据验证。
- `check_snapshot(snapshot: Snapshot, previous: dict | None, config: dict, custom_rules: str) -> dict`：生成 diff/上下文、覆盖范围与结果；返回状态和问题，不写项目文件。

- [x] 编写测试：本机假模型 HTTP 服务返回有效证据、错误、非法位置、含密钥错误、超限上下文及读取未变文档。
- [x] `python -m unittest tests.test_checker -v`，观察失败。
- [x] 实现真实 HTTP 适配及上下文预算；无效问题不伪装为成功。
- [x] 同命令及已有测试通过。

### Task 3: 自动调度与 API

**Files:** `codescout/service.py`, `codescout/server.py`, `tests/test_service.py`, `tests/test_server.py`。

**Interfaces:**
- `Service(store: Store)`：`add_project(data)`, `configure_model(data)`, `observe()`, `process_one()`；后台循环复用同一行为。
- `create_server(service: Service, port: int, token: str) -> ThreadingHTTPServer`。
- API：`GET /api/state`, `GET /api/projects/{id}/runs`, `POST /api/projects`, `POST /api/model`, `POST /api/projects/{id}/pause|resume|retry`, `DELETE /api/projects/{id}`。

- [x] 编写测试：接入自动检查、保存触发、内容去重、规则变化、失败不更新基线、部分覆盖不解决旧问题、暂停/移除、重启和同源访问保护。
- [x] `python -m unittest tests.test_service tests.test_server -v`，观察失败。
- [x] 实现串行任务执行、观察/成功基线分离、有限重试与本机 API。
- [x] 同命令及完整测试通过。

### Task 4: 浏览器界面与运行交付

**Files:** `codescout/web/index.html`, `codescout/web/app.js`, `codescout/web/style.css`, `codescout/__main__.py`, `README.md`, `docs/README.md`, `docs/roadmap.md`, `docs/requirements.md`, `docs/implementation-status.md`。

- [x] 先写 CLI 启动与真实 HTTP 集成测试，确认缺失入口导致失败。
- [x] 实现中文界面：项目添加、代码发送许可、模型配置、监控状态、检查进度、覆盖范围、证据问题和报告历史；轮询更新。
- [x] CLI `python -m codescout --port 8765`；用户数据默认存于 LOCALAPPDATA/CodeScout，支持 `--data-dir`；输出可打开的带令牌 URL。
- [x] 更新文档，清晰列出已经实现与尚未实现的路线图能力。
- [x] 完整测试、JavaScript 语法检查、真实启动及浏览器检查；独立代码审查；修复重要问题后重新验证。

## 执行记录

在当前会话直接实现，不创建额外工作区；使用 `feat/codescout-foundation` 分支隔离本地开发。保持修改可审阅，不提交或推送到远程。阶段验证记录写入 `docs/implementation-status.md`。
