"use strict";
const $ = (id) => document.getElementById(id);
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[char]));
const statuses = {waiting:"等待检查",waiting_model:"等待模型",waiting_rules:"等待保存约定",waiting_consent:"等待许可",running:"检查中",completed:"检查完成",partial:"部分完成",failed:"检查失败",unavailable:"暂不可检查",paused:"已暂停",superseded:"已被替代"};
const categories = {convention:"约定",quality:"代码质量",comment:"注释",documentation:"文档"};
const severity = {high:"高",medium:"中",low:"低"};
const repairStatuses = {queued:"等待生成",running:"生成中",ready:"待应用",failed:"修复失败",conflict:"文件冲突",applied:"已应用",validation_failed:"验证失败"};
const repairStages = {queued:"等待",reading:"读取文件",generating:"分析与定位",ready:"修改预览",applying:"应用修改",verifying:"验证",done:"完成",interrupted:"服务中断"};
const UI_KEY = "codescout-ui";
function storageGet(key) { try { return sessionStorage.getItem(key); } catch { return null; } }
function storageSet(key,value) { try { sessionStorage.setItem(key,value); } catch { /* UI preferences are optional. */ } }
function loadUi() {
  try { const saved = JSON.parse(storageGet(UI_KEY) || "{}"); return {selected:saved.selected || null,view:["findings","runs","repairs"].includes(saved.view) ? saved.view : "findings",filter:saved.filter || "open",disclosures:saved.disclosures && typeof saved.disclosures === "object" ? saved.disclosures : {}}; }
  catch { return {selected:null,view:"findings",filter:"open",disclosures:{}}; }
}
const ui = loadUi();
let state = null, selected = ui.selected, view = ui.view, refreshTask = null, editing = null, rulesExpected = "", picking = false, activeRepair = null;
let polling = null;
const pendingApplications = new Set();
const fragment = new URLSearchParams(location.hash.slice(1));
let token = fragment.get("token") || storageGet("codescout-token");
if (fragment.get("token")) { storageSet("codescout-token",token); history.replaceState(null,"",location.pathname); }
window.addEventListener("hashchange", () => {
  const nextToken = new URLSearchParams(location.hash.slice(1)).get("token");
  if (!nextToken) return;
  token = nextToken; storageSet("codescout-token",token);
  history.replaceState(null,"",location.pathname);
  $("notice").hidden = true;
  refresh(true); startPolling();
});

function saveUi() {
  ui.selected = selected; ui.view = view; ui.filter = $("finding-filter").value;
  storageSet(UI_KEY,JSON.stringify(ui));
}
function rememberDisclosures(root = document) {
  for (const detail of root.querySelectorAll("details[data-disclosure]")) {
    ui.disclosures[detail.dataset.disclosure] = detail.open;
    if (detail.dataset.preference) ui.disclosures[detail.dataset.preference] = detail.open;
  }
  saveUi();
}
function disclosureOpen(key,preference) {
  return Object.hasOwn(ui.disclosures,key) ? ui.disclosures[key] === true : Boolean(preference && ui.disclosures[preference]);
}
function disclosureAttributes(key,preference = "") {
  return `data-disclosure="${esc(key)}"${preference ? ` data-preference="${esc(preference)}"` : ""}${disclosureOpen(key,preference) ? " open" : ""}`;
}
document.addEventListener("toggle", (event) => {
  const detail = event.target;
  if (!detail.dataset?.disclosure) return;
  ui.disclosures[detail.dataset.disclosure] = detail.open;
  if (detail.dataset.preference) ui.disclosures[detail.dataset.preference] = detail.open;
  saveUi();
},true);

function notice(message,success = false) {
  const dialog = document.querySelector("dialog[open]");
  if (dialog && !success) {
    let error = dialog.querySelector(".form-error");
    if (!error) { error = document.createElement("p"); error.className = "error-box form-error"; error.setAttribute("role","alert"); dialog.querySelector("form").append(error); }
    error.textContent = message;
  } else { $("notice").textContent = message; $("notice").hidden = false; $("notice").className = success ? "success" : ""; }
}
async function api(path,data,method) {
  const response = await fetch(path,{method:method || (data === undefined ? "GET" : "POST"),headers:{"Authorization":"Bearer " + (token || ""),"Content-Type":"application/json"},body:data === undefined ? undefined : JSON.stringify(data)});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "请求未完成");
  return result;
}
function time(value) { return value ? new Date(value).toLocaleString("zh-CN",{month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit"}) : "—"; }
function badge(status,labels = statuses) { return `<span class="badge ${esc(status)}">${esc(labels[status] || status || "—")}</span>`; }
function basename(path) { return String(path || "").replace(/[\\/]+$/g,"").split(/[\\/]/).pop() || path; }
function current() { return state?.projects.find((project) => project.id === selected); }
function repairs(project) { return project.repairs || []; }
function latestRepair(project,finding) { return repairs(project).find((repair) => repair.finding_id === finding.id); }
function runKey(project,run) { return `project:${project.id}:run:${run.id || run.digest + ":" + run.created_at}:scope`; }
function progressText(progress) {
  if (!progress || !Number.isFinite(progress.total)) return "";
  return `批次 ${progress.completed || 0}/${progress.total}${progress.current_file ? ` · ${progress.current_file}` : ""}`;
}
function stageText(repair) { return repairStages[repair.stage] || repairStatuses[repair.status] || repair.status; }
function verificationText(verification) {
  if (!verification || !Object.keys(verification).length) return "尚未验证";
  const syntax = {passed:"语法检查通过",failed:"语法检查失败",not_applicable:"无可执行的语法检查",skipped:"语法检查未执行",not_configured:"语法检查未配置"}[verification.syntax_status] || "语法检查未执行";
  if (verification.status === "not_configured") return `${syntax}；未配置项目测试命令，未运行项目测试。`;
  const command = verification.command ? ` · ${Array.isArray(verification.command) ? verification.command.join(" ") : verification.command}` : "";
  return `${syntax}；${{passed:"项目验证通过",failed:"项目验证失败",pending:"等待项目验证",running:"项目验证中"}[verification.status] || "项目验证未执行"}${command}${verification.exit_code !== undefined && verification.exit_code !== null ? ` · 退出码 ${verification.exit_code}` : ""}${verification.timed_out ? " · 命令超时" : ""}${verification.output_truncated ? " · 输出已截断" : ""}`;
}

function refresh(force = false) {
  if (refreshTask) return force ? refreshTask.then(() => refresh()) : refreshTask;
  refreshTask = (async () => {
    try {
      state = await api("/api/state");
      if (!state.projects.some((project) => project.id === selected)) selected = state.projects[0]?.id || null;
      render();
    } catch (error) { notice(error.message); }
  })().finally(() => { refreshTask = null; });
  return refreshTask;
}
function startPolling() { if (polling === null) polling = setInterval(refresh,3000); }

function render() {
  rememberDisclosures();
  $("project-count").textContent = state.projects.length;
  $("project-list").innerHTML = state.projects.map((project) => `<button data-project="${esc(project.id)}" class="${project.id === selected ? "selected" : ""}" title="${esc(basename(project.path))}">${esc(basename(project.path))}</button>`).join("");
  $("model-banner").hidden = Boolean(state.model.model);
  const project = current();
  $("empty").hidden = Boolean(project); $("project-view").hidden = !project;
  if (!project) { renderRepairPreview(); saveUi(); return; }
  $("project-name").textContent = basename(project.path); $("project-path").textContent = project.path;
  const pathDetail = $("project-path-detail"); pathDetail.dataset.disclosure = `project:${project.id}:path`; pathDetail.open = disclosureOpen(pathDetail.dataset.disclosure);
  $("project-status").className = "badge " + project.status; $("project-status").textContent = statuses[project.status] || project.status;
  $("pause-project").textContent = project.paused ? "恢复监控" : "暂停监控";
  $("open-count").textContent = project.findings.filter((finding) => finding.status === "open").length;
  const latest = project.runs[0], coverage = latest?.result?.coverage, progress = latest?.result?.progress;
  $("coverage-count").textContent = coverage?.checked ? `${coverage.checked.length} 个文件` : "—";
  $("coverage-caption").textContent = coverage ? `沿用 ${coverage.reused?.length || 0} · 未覆盖 ${coverage.uncovered?.length || 0} · 排除 ${Object.keys(coverage.excluded || {}).length}` : "尚无覆盖记录";
  $("checked-time").textContent = time(project.last_checked);
  $("check-progress").hidden = !progress;
  $("check-progress-text").textContent = progressText(progress);
  $("check-progress-bar").max = Math.max(1,progress?.total || 1);
  $("check-progress-bar").value = progress?.completed || 0;
  $("project-error").hidden = !project.error;
  $("project-error").textContent = project.error ? project.error + (project.retry_remaining ? `（还将重试 ${project.retry_remaining} 次）` : "") : "";
  $("git-caption").textContent = project.git ? `Git ${project.git.branch} · ${(project.git.head || "无提交").slice(0,8)}` : "工作目录";
  renderFindings(project); renderRuns(project); renderRepairs(project); renderRepairPreview(); saveUi();
}

function renderFindings(project) {
  const filter = $("finding-filter").value;
  const findings = project.findings.filter((finding) => filter === "all" || finding.status === filter);
  if (!findings.length) {
    const latest = project.runs[0];
    let message = "尚无检查结果";
    if (["failed","unavailable"].includes(project.status)) message = "检查未完成，请查看失败原因";
    else if (project.status === "partial") message = "已检查范围内暂无问题；未覆盖范围见检查记录";
    else if (latest?.status === "completed" && !latest.stale) message = "当前筛选范围内暂无问题";
    $("findings-list").innerHTML = `<div class="list-empty">${esc(message)}</div>`;
    return;
  }
  $("findings-list").innerHTML = findings.map((finding) => {
    const repair = latestRepair(project,finding);
    const canGenerate = finding.status === "open" && !finding.stale && project.allow_send;
    const repairAction = repair ? `<button class="secondary" data-repair="${esc(repair.id)}">${esc(repair.status === "ready" ? "预览修复" : stageText(repair))}</button>` : "";
    const generate = canGenerate && (!repair || ["failed","conflict","applied","validation_failed"].includes(repair.status)) ? `<button class="primary" data-generate="${esc(finding.id)}">${repair ? "重新生成修复" : "生成修复"}</button>` : "";
    const provenance = finding.not_rechecked ? '<span>引用文件未变化，沿用此前检查结果</span>' : "";
    return `<article class="finding"><div class="finding-header"><h3>${esc(finding.title)}</h3><span class="severity ${esc(finding.severity)}">${esc(severity[finding.severity])} · ${esc(categories[finding.category])}</span></div><div class="location">${esc(finding.path)}:${esc(finding.line)}</div><p>${esc(finding.reason)}</p><div class="evidence">${esc(finding.evidence)}</div>${(finding.related || []).map((item) => `<div class="location">关联 ${esc(item.path)}:${esc(item.line)}</div><div class="evidence">${esc(item.evidence)}</div>`).join("")}<p>约定：${esc(finding.rule)}<br>建议：${esc(finding.suggestion)}</p><div class="finding-meta"><span>置信度 ${esc(severity[finding.confidence])}</span><span>${esc({open:"待处理",ignored:"已忽略",resolved:"已解决"}[finding.status])}</span>${provenance}${finding.stale ? '<span class="stale">快照已过期，等待复查</span>' : ""}</div><div class="finding-actions">${generate}${repairAction}${finding.status !== "resolved" ? `<button class="text-button" data-finding="${esc(finding.id)}" data-status="${finding.status === "ignored" ? "open" : "ignored"}">${finding.status === "ignored" ? "恢复待处理" : "忽略"}</button>` : ""}</div></article>`;
  }).join("");
}

function coverageText(coverage) {
  const items = [...(coverage.checked || []).map((path) => "本次已检查  " + path),...(coverage.reused || []).map((path) => "沿用已检查结果（文件未变化，本次未重查）  " + path),...(coverage.uncovered || []).map((path) => "未覆盖  " + path),...Object.entries(coverage.excluded || {}).map(([path,reason]) => "已排除  " + path + " · " + reason),...(coverage.warnings || []).map((warning) => "提示  " + warning)];
  const ranges = coverage.ranges || coverage.checked_ranges;
  if (Array.isArray(ranges)) for (const range of ranges) items.push(`已检查行段  ${range.path || range.file}:${range.start_line || range.start || 1}-${range.end_line || range.end || "?"}`);
  else if (ranges && typeof ranges === "object") for (const [path,segments] of Object.entries(ranges)) items.push(`已检查行段  ${path} · ${JSON.stringify(segments)}`);
  for (const range of coverage.skipped_ranges || []) items.push(`未覆盖行段  ${range.path}:${range.start_line}-${range.end_line} · ${range.reason}`);
  return items.join("\n") || "尚无完成的覆盖记录";
}
function renderRuns(project) {
  rememberDisclosures($("runs-list"));
  $("runs-list").innerHTML = project.runs.length ? project.runs.map((run,index) => {
    const result = run.result || {}, coverage = result.coverage || {}, progress = progressText(result.progress);
    const preference = index === 0 ? `project:${project.id}:latest-scope` : "";
    return `<article class="run-card"><div class="run-heading"><strong>${esc(time(run.created_at))}</strong>${badge(run.status)}</div><p>快照 ${esc((run.digest || "").slice(0,16))}${run.stale ? ' · <span class="stale">已过期</span>' : ""}<br>模型 ${esc(result.model || "—")} · 已检查 ${(coverage.checked || []).length} 个文件 · 未覆盖 ${(coverage.uncovered || []).length} 个文件${coverage.context_chars ? ` · 上下文 ${esc(coverage.context_chars)}/${esc(coverage.budget_chars)} 字符` : ""}${progress ? `<br>${esc(progress)}` : ""}</p>${result.error ? `<p class="stale">${esc(result.error)}</p>` : ""}<details ${disclosureAttributes(runKey(project,run),preference)}><summary>检查范围与排除原因</summary><pre>${esc(coverageText(coverage))}</pre></details></article>`;
  }).join("") : '<div class="list-empty">等待首次检查</div>';
}

function renderRepairs(project) {
  rememberDisclosures($("repairs-list"));
  $("repairs-list").innerHTML = repairs(project).length ? repairs(project).map((repair) => {
    const finding = project.findings.find((item) => item.id === repair.finding_id);
    return `<article class="run-card"><div class="run-heading"><strong>${esc(finding?.title || "修复记录")}</strong>${badge(repair.status,repairStatuses)}</div><p>${esc(stageText(repair))}${repair.error ? ` · ${esc(repair.error)}` : ""}</p>${repair.verification ? `<p>${esc(verificationText(repair.verification))}</p>` : ""}<button class="secondary" data-repair="${esc(repair.id)}">查看修复</button>${repair.diff ? `<details ${disclosureAttributes(`project:${project.id}:repair:${repair.id}:diff`)}><summary>修改差异</summary><pre class="diff">${esc(repair.diff)}</pre></details>` : ""}</article>`;
  }).join("") : '<div class="list-empty">尚无修复记录</div>';
}
function openRepair(projectId,repairId) {
  rememberDisclosures();
  activeRepair = {projectId,repairId}; $("repair-form").querySelector(".form-error")?.remove();
  renderRepairPreview(); if (activeRepair && !$("repair-dialog").open) $("repair-dialog").showModal();
}
function renderRepairPreview() {
  if (!activeRepair) return;
  const project = state.projects.find((item) => item.id === activeRepair.projectId);
  const repair = project && repairs(project).find((item) => item.id === activeRepair.repairId);
  if (!repair) { $("repair-dialog").close(); activeRepair = null; return; }
  const finding = project.findings.find((item) => item.id === repair.finding_id);
  $("repair-title").textContent = finding?.title || "修复预览";
  $("repair-stage").textContent = repair.stage && repair.stage !== "done" ? stageText(repair) : repairStatuses[repair.status] || repair.status;
  const diff = repair.diff || "等待生成修改差异";
  if ($("repair-diff").textContent !== diff) { const position = $("repair-diff").scrollTop; $("repair-diff").textContent = diff; $("repair-diff").scrollTop = position; }
  $("repair-files").textContent = (repair.changes || []).map((change) => change.path).join(" · ");
  $("repair-error").hidden = !repair.error; $("repair-error").textContent = repair.error || "";
  $("repair-verification").textContent = verificationText(repair.verification);
  $("repair-output").hidden = !repair.verification?.output;
  if ($("repair-output").textContent !== (repair.verification?.output || "")) $("repair-output").textContent = repair.verification?.output || "";
  if (["applied","validation_failed","failed","conflict"].includes(repair.status)) pendingApplications.delete(repair.id);
  const stale = !finding || finding.stale || finding.status !== "open";
  const canApply = repair.status === "ready" && !stale && project.allow_send && !pendingApplications.has(repair.id);
  $("apply-repair").disabled = !canApply;
  $("apply-repair").textContent = pendingApplications.has(repair.id) ? "应用与验证中" : "应用修改";
  $("repair-apply-note").textContent = repair.status === "ready" ? (stale ? "问题状态已变化，请重新检查并生成修复。" : !project.allow_send ? "项目发送许可已关闭。" : "应用前会核对源文件，应用后自动复查。") : ["applied","validation_failed"].includes(repair.status) ? "修改已写入；问题是否解决见后续检查。" : "";
  const events = repair.events || [];
  $("repair-events").textContent = events.map((event) => `${{read_file:"读取文件",search:"搜索定位"}[event.tool] || event.tool || event.type || ""}${event.path ? " · " + event.path : ""}${event.start_line ? ":" + event.start_line + (event.end_line ? "-" + event.end_line : "") : ""}`).join("\n") || "尚无读取记录";
  const detail = $("repair-events-detail"); detail.dataset.disclosure = `project:${project.id}:repair:${repair.id}:events`; detail.open = disclosureOpen(detail.dataset.disclosure);
}

function projectDialog(project = null,path = "") {
  const form = $("project-form"); form.reset(); editing = project?.id || null; rulesExpected = project?.custom_rules || "";
  form.querySelector("h2").textContent = `${basename(project?.path || path)} · ${project ? "项目设置" : "接入项目"}`;
  form.querySelector("[type=submit]").textContent = project ? "保存" : "接入";
  form.elements.path.value = project?.path || path; form.elements.path.readOnly = true; form.elements.allow_send.required = !project;
  form.elements.rules_file.value = project?.rules_file || "CODESCOUT_RULES.md";
  form.elements.custom_rules.value = rulesExpected;
  form.elements.validation_command.value = project?.validation_command || "";
  form.elements.excludes.value = (project?.excludes || []).join("\n");
  form.elements.allow_send.checked = Boolean(project?.allow_send);
  form.querySelector(".form-error")?.remove(); $("project-dialog").showModal();
}
async function addProject() {
  if (picking) return;
  picking = true;
  const buttons = ["add-project","top-add","empty-add"].map($); buttons.forEach((button) => button.disabled = true);
  try { const result = await api("/api/pick-folder",{}); if (!result.cancelled && result.path) projectDialog(null,result.path); }
  catch (error) { notice(error.message); }
  finally { picking = false; buttons.forEach((button) => button.disabled = false); }
}
for (const id of ["add-project","top-add","empty-add"]) $(id).addEventListener("click",addProject);
$("edit-project").addEventListener("click", () => { if (current()) projectDialog(current()); });
for (const id of ["open-model","banner-model"]) $(id).addEventListener("click", () => {
  const form = $("model-form"), model = state?.model || {};
  for (const key of ["base_url","model","timeout","context_chars"]) if (model[key] !== undefined) form.elements[key].value = model[key];
  form.elements.api_key.value = ""; $("key-hint").textContent = model.has_key ? "留空保留已存密钥" : "仅保存在本机";
  form.querySelector(".form-error")?.remove(); $("model-dialog").showModal();
});
document.querySelectorAll("[data-close]").forEach((button) => button.addEventListener("click", () => { rememberDisclosures(); $(button.dataset.close).close(); if (button.dataset.close === "repair-dialog") activeRepair = null; }));
$("repair-dialog").addEventListener("close", () => { activeRepair = null; });
$("project-list").addEventListener("click", (event) => {
  const button = event.target.closest("[data-project]");
  if (button) { rememberDisclosures(); selected = button.dataset.project; $("repair-dialog").close(); activeRepair = null; render(); }
});
$("project-form").addEventListener("submit",async (event) => {
  event.preventDefault(); const form = event.currentTarget, button = form.querySelector("[type=submit]"); button.disabled = true;
  try {
    const project = await api(editing ? `/api/projects/${editing}/config` : "/api/projects",{path:form.elements.path.value.trim(),excludes:form.elements.excludes.value.split("\n").map((line) => line.trim()).filter(Boolean),custom_rules:form.elements.custom_rules.value,rules_expected:rulesExpected,validation_command:form.elements.validation_command.value.trim(),allow_send:form.elements.allow_send.checked});
    selected = project.id; form.reset(); $("project-dialog").close(); notice("项目设置已保存",true); await refresh(true);
  } catch (error) { notice(error.message); } finally { button.disabled = false; }
});
$("model-form").addEventListener("submit",async (event) => {
  event.preventDefault(); const form = event.currentTarget, button = form.querySelector("[type=submit]"); button.disabled = true;
  try { await api("/api/model",{base_url:form.elements.base_url.value.trim(),model:form.elements.model.value.trim(),api_key:form.elements.api_key.value.trim(),timeout:Number(form.elements.timeout.value),context_chars:Number(form.elements.context_chars.value)}); form.elements.api_key.value = ""; $("model-dialog").close(); notice("模型设置已保存",true); await refresh(true); }
  catch (error) { notice(error.message); } finally { button.disabled = false; }
});
async function action(action) { const project = current(); if (!project) return; try { await api(`/api/projects/${project.id}/${action}`,{}); await refresh(true); } catch (error) { notice(error.message); } }
$("pause-project").addEventListener("click", () => action(current()?.paused ? "resume" : "pause"));
$("retry-project").addEventListener("click", () => action("retry"));
$("remove-project").addEventListener("click",async () => { const project = current(); if (!project || !confirm(`移除「${basename(project.path)}」的监控和本机报告？项目源文件会保留。`)) return; try { await api(`/api/projects/${project.id}`,undefined,"DELETE"); await refresh(true); } catch (error) { notice(error.message); } });
async function generateRepair(project,findingId,button) {
  const finding = project.findings.find((item) => item.id === findingId);
  if (!finding || finding.stale || finding.status !== "open" || !project.allow_send) return;
  button.disabled = true;
  try {
    const result = await api(`/api/projects/${project.id}/repairs`,{finding_id:findingId});
    const repair = result.repair || result;
    await refresh(true); if (selected === project.id) openRepair(project.id,repair.id);
  } catch (error) { notice(error.message); } finally { button.disabled = false; }
}
$("findings-list").addEventListener("click",async (event) => {
  const project = current(); if (!project) return;
  const generate = event.target.closest("[data-generate]"); if (generate) { await generateRepair(project,generate.dataset.generate,generate); return; }
  const preview = event.target.closest("[data-repair]"); if (preview) { openRepair(project.id,preview.dataset.repair); return; }
  const button = event.target.closest("[data-finding]"); if (!button) return;
  try { await api(`/api/projects/${project.id}/findings`,{id:button.dataset.finding,status:button.dataset.status}); await refresh(true); } catch (error) { notice(error.message); }
});
$("repairs-list").addEventListener("click", (event) => { const button = event.target.closest("[data-repair]"); if (button && current()) openRepair(selected,button.dataset.repair); });
$("apply-repair").addEventListener("click",async () => {
  if (!activeRepair || $("apply-repair").disabled) return;
  const target = {...activeRepair}; pendingApplications.add(target.repairId); renderRepairPreview();
  try { await api(`/api/projects/${target.projectId}/repairs/${target.repairId}/apply`,{}); await refresh(true); }
  catch (error) { pendingApplications.delete(target.repairId); notice(error.message); renderRepairPreview(); }
});
function setView(next) {
  view = next;
  for (const name of ["findings","runs","repairs"]) { $(`${name}-list`).hidden = view !== name; $(`${name}-tab`).classList.toggle("active",view === name); }
  $("finding-filter").parentElement.hidden = view !== "findings"; saveUi();
}
for (const name of ["findings","runs","repairs"]) $(`${name}-tab`).addEventListener("click", () => setView(name));
$("finding-filter").value = ["open","all","ignored","resolved"].includes(ui.filter) ? ui.filter : "open";
$("finding-filter").addEventListener("change", () => { if (current()) renderFindings(current()); saveUi(); });
setView(view);
if (token) { refresh(); startPolling(); } else notice("请从服务启动时提供的本机链接打开页面。");
