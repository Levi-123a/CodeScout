"use strict";
const $ = (id) => document.getElementById(id);
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[char]));
const statuses = {waiting:"等待检查",waiting_model:"等待模型配置",waiting_consent:"等待发送许可",running:"检查中",completed:"检查完成",partial:"部分完成",failed:"检查失败",unavailable:"暂不可检查",paused:"已暂停",superseded:"已被新状态替代"};
const categories = {convention:"约定符合性",quality:"代码质量",comment:"注释准确性",documentation:"文档一致性"};
const severity = {high:"高",medium:"中",low:"低"};
let state = null, selected = null, view = "findings", refreshing = false, editing = null;
const fragment = new URLSearchParams(location.hash.slice(1));
let token = fragment.get("token") || sessionStorage.getItem("codescout-token");
if (fragment.get("token")) { sessionStorage.setItem("codescout-token", token); history.replaceState(null, "", location.pathname); }
window.addEventListener("hashchange", () => {
  const nextToken = new URLSearchParams(location.hash.slice(1)).get("token");
  if (!nextToken) return;
  token = nextToken; sessionStorage.setItem("codescout-token", token);
  history.replaceState(null, "", location.pathname);
  selected = null; $("notice").hidden = true;
  refresh();
});

function notice(message, success = false) {
  const dialog = document.querySelector("dialog[open]");
  if (dialog && !success) {
    let error = dialog.querySelector(".form-error");
    if (!error) { error = document.createElement("p"); error.className = "error-box form-error"; error.setAttribute("role","alert"); dialog.querySelector("form").append(error); }
    error.textContent = message;
  } else { $("notice").textContent = message; $("notice").hidden = false; $("notice").className = success ? "success" : ""; }
}
async function api(path, data, method) {
  const response = await fetch(path, {method: method || (data === undefined ? "GET" : "POST"), headers: {"Authorization":"Bearer " + (token || ""),"Content-Type":"application/json"}, body:data === undefined ? undefined : JSON.stringify(data)});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "请求未完成");
  return result;
}
function time(value) { return value ? new Date(value).toLocaleString("zh-CN", {month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit"}) : "—"; }
function badge(status) { return `<span class="badge ${esc(status)}">${esc(statuses[status] || status)}</span>`; }
function current() { return state?.projects.find((project) => project.id === selected); }

async function refresh() {
  if (refreshing) return;
  refreshing = true;
  try { state = await api("/api/state"); if (!state.projects.some((project) => project.id === selected)) selected = state.projects[0]?.id || null; render(); }
  catch (error) { notice(error.message); }
  finally { refreshing = false; }
}

function render() {
  $("project-count").textContent = state.projects.length;
  $("project-list").innerHTML = state.projects.map((project) => `<button data-project="${esc(project.id)}" class="${project.id === selected ? "selected" : ""}">◈ &nbsp; ${esc(project.name)}</button>`).join("");
  $("model-banner").hidden = Boolean(state.model.model);
  const project = current();
  $("empty").hidden = Boolean(project); $("project-view").hidden = !project;
  if (!project) return;
  $("project-name").textContent = project.name; $("project-path").textContent = project.path;
  $("project-status").className = "badge " + project.status; $("project-status").textContent = statuses[project.status] || project.status;
  $("pause-project").textContent = project.paused ? "恢复监控" : "暂停监控";
  $("open-count").textContent = project.findings.filter((finding) => finding.status === "open").length;
  const latest = project.runs[0]; const coverage = latest?.result.coverage;
  $("coverage-count").textContent = coverage?.checked ? `${coverage.checked.length} 个文件` : "—";
  $("coverage-count").className = "time-value";
  $("coverage-caption").textContent = coverage ? `未覆盖 ${coverage.uncovered?.length || 0} · 排除 ${Object.keys(coverage.excluded || {}).length}` : "等待首次检查";
  $("checked-time").textContent = time(project.last_checked);
  $("target-caption").textContent = latest ? `工作目录 · ${latest.digest.slice(0,10)}` : "工作目录快照";
  $("project-error").hidden = !project.error; $("project-error").textContent = project.error + (project.retry_remaining ? `（最多还将自动重试 ${project.retry_remaining} 次）` : "");
  $("git-caption").textContent = project.git ? `Git ${project.git.branch} · ${(project.git.head || "无提交").slice(0,8)}` : "无 Git · 仅监控工作目录";
  renderFindings(project); renderRuns(project);
}

function renderFindings(project) {
  const filter = $("finding-filter").value;
  const findings = project.findings.filter((finding) => filter === "all" || finding.status === filter);
  if (!findings.length) {
    const latest = project.runs[0];
    let title = "尚无检查结果", detail = "检查开始后，问题、依据和建议会显示在这里。";
    if (project.status === "failed" || project.status === "unavailable") { title = "检查尚未完成"; detail = "请查看失败原因。当前没有问题列表不代表检查通过。"; }
    else if (project.status === "partial") { title = "已检查范围内暂无待处理问题"; detail = "仍有未覆盖内容，请在检查记录中查看具体范围。"; }
    else if (latest?.status === "completed" && !latest.stale) { title = "当前筛选范围内暂无问题"; detail = "已完成本轮检查。模型结论与实际覆盖范围可在检查记录中复核。"; }
    $("findings-list").innerHTML = `<div class="list-empty"><strong>${esc(title)}</strong>${esc(detail)}</div>`;
    return;
  }
  $("findings-list").innerHTML = findings.map((finding) => `<article class="finding"><div class="finding-header"><h3>${esc(finding.title)}</h3><span class="severity ${esc(finding.severity)}">${esc(severity[finding.severity])} · ${esc(categories[finding.category])}</span></div><div class="location">${esc(finding.path)}:${esc(finding.line)}</div><p>${esc(finding.reason)}</p><div class="evidence">${esc(finding.evidence)}</div>${(finding.related || []).map((item) => `<div class="location">关联 ${esc(item.path)}:${esc(item.line)}</div><div class="evidence">${esc(item.evidence)}</div>`).join("")}<p>约定：${esc(finding.rule)}<br>建议：${esc(finding.suggestion)}</p><div class="finding-meta"><span>置信度：${esc(severity[finding.confidence])}</span><span>${esc({open:"待处理",ignored:"已忽略",resolved:"已解决"}[finding.status])}</span>${finding.stale ? '<span class="stale">来源快照已过期，等待新检查核对</span>' : ""}<button data-finding="${esc(finding.id)}" data-status="${finding.status === "ignored" ? "open" : "ignored"}">${finding.status === "ignored" ? "恢复待处理" : "忽略此问题"}</button></div></article>`).join("");
}

function renderRuns(project) {
  $("runs-list").innerHTML = project.runs.length ? project.runs.map((run) => {
    const result = run.result, coverage = result.coverage || {};
    const scope = [...(coverage.checked || []).map((path) => "已检查  " + path), ...(coverage.uncovered || []).map((path) => "未覆盖  " + path), ...Object.entries(coverage.excluded || {}).map(([path,reason]) => "已排除  " + path + " · " + reason), ...(coverage.warnings || []).map((warning) => "提示  " + warning)].join("\n");
    return `<article class="run-card"><div class="run-heading"><strong>${esc(time(run.created_at))} · 工作目录</strong>${badge(run.status)}</div><p>快照 ${esc(run.digest.slice(0,16))}${run.stale ? ' · <span class="stale">已过期</span>' : ""}<br>模型 ${esc(result.model || "—")} · 已检查 ${(coverage.checked || []).length} 个文件 · 未覆盖 ${(coverage.uncovered || []).length} 个文件${coverage.context_chars ? ` · 上下文 ${coverage.context_chars}/${coverage.budget_chars} 字符` : ""}</p>${result.error ? `<p class="stale">${esc(result.error)}</p>` : ""}<details><summary>查看实际检查范围与排除原因</summary><pre>${esc(scope || "尚无完成的覆盖记录")}</pre></details></article>`;
  }).join("") : '<div class="list-empty"><strong>等待首次检查</strong>自动检查开始后会记录快照、覆盖范围和结果。</div>';
}

function projectDialog(project = null) {
  const form = $("project-form"); form.reset(); editing = project?.id || null;
  form.querySelector("h2").textContent = project ? "项目设置" : "接入本地项目";
  form.querySelector("[type=submit]").textContent = project ? "保存并重新核对" : "接入并自动检查";
  form.elements.path.disabled = Boolean(project); form.elements.name.disabled = Boolean(project); form.elements.allow_send.required = !project;
  form.querySelector(".form-error")?.remove();
  if (project) { form.elements.path.value = project.path; form.elements.name.value = project.name; form.elements.excludes.value = project.excludes.join("\n"); form.elements.custom_rules.value = project.custom_rules; form.elements.allow_send.checked = project.allow_send; }
  $("project-dialog").showModal();
}
for (const id of ["add-project","top-add","empty-add"]) $(id).addEventListener("click", () => projectDialog());
$("edit-project").addEventListener("click", () => { if (current()) projectDialog(current()); });
for (const id of ["open-model","banner-model"]) $(id).addEventListener("click", () => {
  const form = $("model-form"), model = state?.model || {};
  for (const key of ["base_url","model","timeout","context_chars"]) if (model[key] !== undefined) form.elements[key].value = model[key];
  form.elements.api_key.value = ""; $("key-hint").textContent = model.has_key ? "已保存，留空保留原密钥" : "只保存在本机";
  form.querySelector(".form-error")?.remove(); $("model-dialog").showModal();
});
document.querySelectorAll("[data-close]").forEach((button) => button.addEventListener("click", () => $(button.dataset.close).close()));
$("project-list").addEventListener("click", (event) => { const button = event.target.closest("[data-project]"); if (button) { selected = button.dataset.project; render(); } });
$("project-form").addEventListener("submit", async (event) => {
  event.preventDefault(); const form = event.currentTarget, button = form.querySelector("[type=submit]"); button.disabled = true;
  try {
    const project = await api(editing ? `/api/projects/${editing}/config` : "/api/projects", {path:form.elements.path.value.trim(),name:form.elements.name.value.trim(),excludes:form.elements.excludes.value.split("\n").map((line) => line.trim()).filter(Boolean),custom_rules:form.elements.custom_rules.value,allow_send:form.elements.allow_send.checked});
    selected = project.id; form.reset(); $("project-dialog").close(); notice(editing ? "项目设置已保存，后台将重新核对。" : "项目已接入，后台将自动安排首次检查。",true); await refresh();
  } catch (error) { notice(error.message); } finally { button.disabled = false; }
});
$("model-form").addEventListener("submit", async (event) => {
  event.preventDefault(); const form = event.currentTarget, button = form.querySelector("[type=submit]"); button.disabled = true;
  try { await api("/api/model", {base_url:form.elements.base_url.value.trim(),model:form.elements.model.value.trim(),api_key:form.elements.api_key.value.trim(),timeout:Number(form.elements.timeout.value),context_chars:Number(form.elements.context_chars.value)}); form.elements.api_key.value = ""; $("model-dialog").close(); notice("模型配置已保存，等待中的项目将自动开始检查。",true); await refresh(); }
  catch (error) { notice(error.message); } finally { button.disabled = false; }
});
async function action(action) { const project = current(); if (!project) return; try { await api(`/api/projects/${project.id}/${action}`, {}); await refresh(); } catch (error) { notice(error.message); } }
$("pause-project").addEventListener("click", () => action(current()?.paused ? "resume" : "pause"));
$("retry-project").addEventListener("click", () => action("retry"));
$("remove-project").addEventListener("click", async () => { const project = current(); if (!project || !confirm(`移除「${project.name}」的监控及本机报告？项目源文件会保留。`)) return; try { await api(`/api/projects/${project.id}`,undefined,"DELETE"); await refresh(); } catch (error) { notice(error.message); } });
$("findings-list").addEventListener("click", async (event) => { const button = event.target.closest("[data-finding]"); if (!button) return; try { await api(`/api/projects/${selected}/findings`,{id:button.dataset.finding,status:button.dataset.status}); await refresh(); } catch (error) { notice(error.message); } });
function setView(next) { view = next; $("findings-list").hidden = view !== "findings"; $("runs-list").hidden = view !== "runs"; $("finding-filter").parentElement.hidden = view !== "findings"; $("findings-tab").classList.toggle("active",view === "findings"); $("runs-tab").classList.toggle("active",view === "runs"); }
$("findings-tab").addEventListener("click", () => setView("findings")); $("runs-tab").addEventListener("click", () => setView("runs")); $("finding-filter").addEventListener("change", () => { if (current()) renderFindings(current()); });
if (token) { refresh(); setInterval(refresh,3000); } else notice("请使用服务启动时显示的完整本机链接，链接包含访问当前会话所需的令牌。");
