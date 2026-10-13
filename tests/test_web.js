"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../codescout/web/app.js"), "utf8");
const markup = fs.readFileSync(path.join(__dirname, "../codescout/web/index.html"), "utf8");

// The application runs unchanged. This small DOM double models the destructive
// innerHTML replacement responsible for the original disclosure regression.
class Element {
  constructor(id = "") {
    this.id = id; this.dataset = {}; this.listeners = {}; this.children = [];
    this.hidden = false; this.disabled = false; this.open = false; this.value = "";
    this.textContent = ""; this.scrollTop = 0; this.parentElement = {hidden: false};
    this.classList = {toggle() {}};
  }
  set innerHTML(value) {
    this.html = value; this.children = [];
    for (const match of value.matchAll(/<details\b([^>]*)>([\s\S]*?)<\/details>/g)) {
      const child = new Element(); child.open = /(?:^|\s)open(?:\s|=|$)/.test(match[1]);
      for (const attr of match[1].matchAll(/data-([\w-]+)="([^"]*)"/g)) {
        child.dataset[attr[1].replace(/-([a-z])/g, (_, letter) => letter.toUpperCase())] = attr[2];
      }
      child.html = match[2]; this.children.push(child);
    }
  }
  get innerHTML() { return this.html || ""; }
  querySelectorAll(selector) {
    if (selector === "details" || selector.includes("data-disclosure")) return this.children.filter((child) => selector === "details" || child.dataset.disclosure);
    return [];
  }
  querySelector(selector) {
    if (selector === ".form-error") return null;
    if (selector === "h2") return this.heading ||= new Element();
    if (selector === "[type=submit]") return this.submit ||= new Element();
    return null;
  }
  addEventListener(type, listener) { (this.listeners[type] ||= []).push(listener); }
  async dispatch(type, event = {}) { for (const listener of this.listeners[type] || []) await listener({target:this, currentTarget:this, preventDefault() {}, ...event}); }
  setAttribute(name, value) { if (name === "open") this.open = true; this[name] = value; }
  removeAttribute(name) { if (name === "open") this.open = false; }
  showModal() { this.open = true; }
  close() { this.open = false; }
  append(child) { this.children.push(child); }
}

function browser(storage = new Map(), handler = () => ({projects:[],model:{}})) {
  const nodes = new Map([...markup.matchAll(/\bid="([^"]+)"/g)].map((match) => [match[1], new Element(match[1])]));
  for (const id of ["project-form", "model-form"]) {
    const form = nodes.get(id);
    const formMarkup = markup.match(new RegExp(`<form id="${id}">([\\s\\S]*?)</form>`))[1];
    form.elements = Object.fromEntries([...formMarkup.matchAll(/\bname="([^"]+)"/g)].map((match) => [match[1],new Element()]));
    form.reset = () => { for (const element of Object.values(form.elements)) {element.value = ""; element.checked = false;} };
  }
  const doc = {
    getElementById: (id) => nodes.get(id) || null,
    querySelectorAll: (selector) => [...nodes.values()].flatMap((node) => [...(selector.includes("data-disclosure") && node.dataset.disclosure ? [node] : []),...node.querySelectorAll(selector)]),
    querySelector: (selector) => selector === "dialog[open]" ? [...nodes.values()].find((node) => node.id.endsWith("-dialog") && node.open) : null,
    createElement: () => new Element(), addEventListener() {},
  };
  const requests = [];
  const context = vm.createContext({document:doc,window:{addEventListener() {}},location:{hash:"",pathname:"/"},history:{replaceState() {}},URLSearchParams,
    sessionStorage:{getItem:(key) => storage.get(key) || null,setItem:(key,value) => storage.set(key,value)},
    fetch:async (url,options) => { requests.push({url,options}); const result = await handler(url,options); return {ok:true,json:async () => result}; },
    setInterval() {},confirm:() => true,console});
  vm.runInContext(source, context);
  return {nodes,context,storage,requests,run: (code) => vm.runInContext(code,context)};
}

function project(id = "one") {
  return {id,name:"user supplied label",path:`C:\\projects\\${id}`,status:"completed",paused:false,allow_send:true,excludes:[],custom_rules:"original",rules_file:"CODESCOUT_RULES.md",validation_command:"",
    findings:[],repairs:[],runs:[{id:`${id}-run`,created_at:"2026-10-13T00:00:00Z",digest:"a".repeat(64),status:"completed",stale:false,result:{model:"model",coverage:{checked:["src/main.py"],uncovered:[],excluded:{},warnings:[]}}}]};
}

function show(app, projects, selected = projects[0].id) {
  app.context.fixture = {projects,model:{model:"model"}}; app.context.pick = selected;
  app.run("state = fixture; selected = pick; render();");
}

test("coverage opened by the user survives polling and a newly completed report", () => {
  const app = browser(), first = project(); show(app,[first]);
  app.nodes.get("runs-list").querySelectorAll("details")[0].open = true;
  for (let i = 0; i < 3; i++) { show(app,[first]); assert.equal(app.nodes.get("runs-list").querySelectorAll("details")[0].open,true); }
  const next = {...first,runs:[{...first.runs[0],id:"new-run",digest:"b".repeat(64)},...first.runs]}; show(app,[next]);
  assert.equal(app.nodes.get("runs-list").querySelectorAll("details")[0].open,true);
  assert.equal(app.nodes.get("runs-list").querySelectorAll("details")[1].open,true);
});

test("disclosure preferences stay with the project through switching and reload", () => {
  const app = browser(), one = project(), two = project("two"); show(app,[one,two]);
  app.nodes.get("runs-list").querySelectorAll("details")[0].open = true;
  show(app,[one,two],"two"); assert.equal(app.nodes.get("runs-list").querySelectorAll("details")[0].open,false);
  show(app,[one,two],"one"); assert.equal(app.nodes.get("runs-list").querySelectorAll("details")[0].open,true);
  const reloaded = browser(app.storage); show(reloaded,[one,two]);
  assert.equal(reloaded.nodes.get("runs-list").querySelectorAll("details")[0].open,true);
});

test("adding opens the folder picker directly and cancellation leaves the form closed", async () => {
  const app = browser(new Map(), () => ({path:null,cancelled:true}));
  await app.nodes.get("add-project").dispatch("click");
  assert.equal(app.requests[0]?.url,"/api/pick-folder");
  assert.equal(app.nodes.get("project-dialog").open,false);
});

test("chosen path uses the folder basename and sends the original rules for conflict checking", async () => {
  const existing = project();
  const app = browser(new Map(), (url) => url === "/api/pick-folder" ? {path:"C:\\projects\\my-folder",cancelled:false} : url === "/api/state" ? {projects:[existing],model:{model:"model"}} : existing);
  await app.nodes.get("add-project").dispatch("click");
  assert.equal(app.nodes.get("project-form").elements.path.value,"C:\\projects\\my-folder");
  assert.equal(app.nodes.get("project-form").elements.path.readOnly,true);
  show(app,[existing]); assert.equal(app.nodes.get("project-name").textContent,"one");
  app.run("projectDialog(current())");
  const form = app.nodes.get("project-form"); form.elements.custom_rules.value = "changed";
  await form.dispatch("submit");
  const payload = JSON.parse(app.requests.find((item) => item.url.endsWith("/config")).options.body);
  assert.equal(payload.rules_expected,"original"); assert.equal(payload.custom_rules,"changed");
  assert.equal(Object.hasOwn(payload,"name"),false);
});

test("repair preview survives refresh and stale findings cannot be applied", () => {
  const app = browser(), p = project();
  p.findings = [{id:"finding",status:"open",stale:false}];
  p.repairs = [{id:"repair",finding_id:"finding",status:"ready",stage:"ready",diff:"--- src/main.py\n+++ src/main.py\n-old\n+new",verification:{status:"not_configured",syntax_status:"passed"},changes:[{path:"src/main.py"}]}];
  show(app,[p]); app.run("openRepair(selected, 'repair')");
  app.nodes.get("repair-diff").scrollTop = 80;
  for (let i = 0; i < 3; i++) show(app,[p]);
  assert.equal(app.nodes.get("repair-dialog").open,true);
  assert.equal(app.nodes.get("repair-diff").scrollTop,80);
  assert.equal(app.nodes.get("apply-repair").disabled,false);
  show(app,[{...p,findings:[{...p.findings[0],stale:true}]}]);
  assert.equal(app.nodes.get("apply-repair").disabled,true);
  assert.match(app.nodes.get("repair-verification").textContent,/未.*测试/);
});

test("generating during an in-flight poll waits for a fresh repair report", async () => {
  const p = project(); p.findings = [{id:"finding",status:"open",stale:false}];
  const repair = {id:"repair",finding_id:"finding",status:"ready",stage:"ready",diff:"the new preview",changes:[]};
  let releasePoll, stateRequests = 0;
  const app = browser(new Map(), (url) => {
    if (url.endsWith("/repairs")) return repair;
    if (url === "/api/state" && ++stateRequests === 1) return new Promise((resolve) => {releasePoll = () => resolve({projects:[p],model:{model:"model"}});});
    return {projects:[{...p,repairs:[repair]}],model:{model:"model"}};
  });
  show(app,[p]);
  const poll = app.run("refresh()");
  app.context.generateButton = new Element();
  const generated = app.run("generateRepair(current(), 'finding', generateButton)");
  await new Promise((resolve) => setImmediate(resolve));
  releasePoll(); await Promise.all([poll,generated]);
  assert.equal(app.nodes.get("repair-diff").textContent,"the new preview");
  assert.equal(app.nodes.get("repair-dialog").open,true);
});

test("path disclosure stays with each project", () => {
  const app = browser(), one = project(), two = project("two"); show(app,[one,two]);
  app.nodes.get("project-path-detail").open = true;
  show(app,[one,two],"two"); assert.equal(app.nodes.get("project-path-detail").open,false);
  show(app,[one,two],"one"); assert.equal(app.nodes.get("project-path-detail").open,true);
});

test("applying uses the preview project and blocks duplicate applications until completion", async () => {
  const p = project(); p.findings = [{id:"finding",status:"open",stale:false}];
  p.repairs = [{id:"repair",finding_id:"finding",status:"ready",stage:"ready",diff:"preview",changes:[]}];
  const app = browser(new Map(), () => ({projects:[p],model:{model:"model"}}));
  show(app,[p]); app.run("openRepair(selected, 'repair')");
  await app.nodes.get("apply-repair").dispatch("click");
  await app.nodes.get("apply-repair").dispatch("click");
  const writes = app.requests.filter((item) => item.url.endsWith("/apply"));
  assert.equal(writes.length,1); assert.equal(writes[0].url,"/api/projects/one/repairs/repair/apply");
  assert.equal(app.nodes.get("apply-repair").disabled,true);
  show(app,[{...p,repairs:[{...p.repairs[0],status:"applied",stage:"done",verification:{status:"not_configured",syntax_status:"passed"}}]}]);
  assert.equal(app.nodes.get("apply-repair").disabled,true);
  assert.match(app.nodes.get("repair-verification").textContent,/未运行项目测试/);
});

test("coverage shows skipped line reasons", () => {
  const app = browser(), p = project();
  p.runs[0].result.coverage = {checked:[],uncovered:["huge.py"],ranges:[],skipped_ranges:[{path:"huge.py",start_line:8,end_line:8,reason:"单行超过请求预算"}],excluded:{},warnings:[]};
  show(app,[p]); assert.match(app.nodes.get("runs-list").innerHTML,/huge.py:8-8.*单行超过请求预算/);
});

test("verification reports timeout and truncated output", () => {
  const app = browser();
  app.context.verification = {status:"failed",syntax_status:"passed",command:"python tests.py",exit_code:1,timed_out:true,output_truncated:true};
  const explanation = app.run("verificationText(verification)");
  assert.match(explanation,/超时/); assert.match(explanation,/截断/);
});

test("incremental coverage and findings identify reused results", () => {
  const app = browser(), p = project();
  p.runs[0].result.coverage.reused = ["unchanged.py"];
  p.findings = [{id:"issue",title:"Existing issue",path:"unchanged.py",line:1,status:"open",stale:false,not_rechecked:true,related:[]}];
  show(app,[p]);
  assert.match(app.nodes.get("runs-list").innerHTML,/沿用已检查结果.*unchanged.py/);
  assert.match(app.nodes.get("findings-list").innerHTML,/引用文件未变化/);
  assert.match(app.nodes.get("coverage-caption").textContent,/沿用 1/);
});
