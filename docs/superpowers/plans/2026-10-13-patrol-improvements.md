# CodeScout Patrol Improvements Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans for integration and superpowers:dispatching-parallel-agents for independent file-owned tasks. Steps use checkbox syntax.

**Goal:** 实现用户八项接入、说明、检查预算、实际修复和易读性要求。
**Architecture:** 保持单机服务；新增独立约定、Windows 选择器、分批上下文及修复模块；SQLite 保存批次进度和修复证据。
**Tech Stack:** Python 3.11+ 标准库、SQLite、原生网页、Windows BAT。
**Spec:** `docs/superpowers/specs/2026-10-13-patrol-improvements-design.md`。

## Global Constraints

- 附加约定为用途明确的独立 Markdown 文件。
- 用户选定问题才生成修复，用户应用才写源码。
- 预算包含规范、源码、工具回复和模型输入开销；不得静默截断后报告完整检查。
- 轮询保留用户打开的详情，输出按文本转义。
- 所有失败和未执行的验证准确记录；不提交、推送或合并。

## Review Focus

- 选择器取消、并发打开、无 GUI 环境及非文件夹结果。
- 外部修改约定文件后保存、项目链接与源文件并发修改。
- 大文件、多批次中途失败、恢复、行号偏移和规范预算过大。
- Agent 越界路径、模糊替换、非法工具、无测试命令和测试超时。
- 详情/修复差异打开后多轮刷新，项目切换后状态不串用。

### Task 1: Windows 接入与独立约定

Owner files: `codescout/native.py`, `codescout/conventions.py`, `start.bat`, `../start.bat`, `tests/test_native.py`, `tests/test_conventions.py`, `tests/test_launcher.py`。

Interfaces: `choose_folder() -> str | None`; `RULES_FILENAME = 'CODESCOUT_RULES.md'`; `load_rules(root: Path) -> str`; `save_rules(root: Path, text: str, expected: str | None) -> None` (compare existing content before saving).

- [x] 先测试选择器调用/取消/错误、约定保存/读取/冲突/链接和 BAT 实际失败路径。
- [x] 观察失败；实现模块和入口，测试通过。

### Task 2: 界面说明与详情稳定

Owner files: `codescout/web/*`, `tests/test_web.js`。

Interfaces: `POST /api/pick-folder -> {path, cancelled}`; project uses folder basename plus path/details, custom_rules, rules_file, validation_command; run.result.progress={completed,total,current_file}, coverage retains old fields; project.repairs contains id/status/diff/verification/error/changes metadata. `POST /api/projects/{id}/repairs {finding_id}`; `POST /api/projects/{id}/repairs/{repair_id}/apply {}`.

- [x] 重现轮询重建详情；添加可运行前端行为测试。
- [x] 删除营销文案、调用选择器、名称/路径/约定和验证设置、进度与修复预览/应用；保留展开状态。
- [x] 脚本语法及行为测试通过。

### Task 3: 分批检查与恢复

Owner files: `codescout/context.py`, `codescout/checker.py`, `codescout/analysis.py`, `tests/test_batches.py`；集成 store/service/model。

Interfaces: `check_snapshot(..., resume=None, on_progress=None, should_continue=None) -> dict`；resume 是成功批次元数据，on_progress 持久化当前 result。

- [x] 测试请求不包含全部文件、硬预算、片段行号、关联未变文档、失败保留成功批次、恢复不重复请求。
- [x] 先观察失败；实现计划器、AST 定位与持久进度，再运行完整后端测试。

### Task 4: 实际 Agent 修复和验证

Owner files: `codescout/repair.py`, `codescout/verification.py`, `tests/test_repair.py`；集成 store/service/server/model。

Interfaces: repair 使用快照 read_file/search 工具，生成受验证的精确替换；Store 保存源内容/哈希、diff、结果；Service 创建和应用修复，HTTP 仅提供已接入项目内操作。

- [x] 测试工具真实读取、diff 预览不写源文件、实际应用、冲突不覆盖、越界拒绝、语法失败和配置命令实际执行。
- [x] 观察失败；实现并通过全部测试。

### Task 5: 集成与交付

- [x] 集成约定文件外部变化、文件夹选择接口、项目名称、验证命令、进度恢复及修复 API。
- [x] 完整测试、浏览器刷新详情复验、独立审查与重要问题修复。
- [x] 更新实际支持范围和启动说明，保留功能分支及本地可运行结果。
