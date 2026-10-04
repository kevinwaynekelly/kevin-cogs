"use strict";

// Exercise the shipped frontend without adding browser or DOM dependencies.
// This verifies request and rendering behavior, not pixel layout.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const web = path.resolve(__dirname, "../dashboardplus/web");
const html = fs.readFileSync(path.join(web, "index.html"), "utf8");
const source = fs.readFileSync(path.join(web, "app.js"), "utf8");
const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map((match) => match[1]);
assert.equal(ids.length, new Set(ids).size, "static HTML IDs must be unique");
for (const match of source.matchAll(/\$\("([^"]+)"\)/g)) {
  assert(ids.includes(match[1]), `Missing static element: ${match[1]}`);
}
for (const match of html.matchAll(/<label\b[^>]*\bfor="([^"]+)"/g)) {
  assert(ids.includes(match[1]), `Label has no control: ${match[1]}`);
}

class Element {
  constructor(tag = "div") {
    this.tagName = tag;
    this.children = [];
    this.attrs = {};
    this.dataset = {};
    this.value = "";
    this.textContent = "";
    this.style = { setProperty() {} };
    this.listeners = {};
    const classes = new Set();
    this.classList = {
      add: (name) => classes.add(name),
      remove: (name) => classes.delete(name),
      contains: (name) => classes.has(name),
      toggle: (name, enabled) => enabled ? classes.add(name) : classes.delete(name),
    };
  }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = items; }
  setAttribute(key, value) { this.attrs[key] = value; }
  addEventListener(type, handler) { this.listeners[type] = handler; }
  querySelector() {
    if (!this.button) this.button = new Element("button");
    return this.button;
  }
  getBoundingClientRect() { return { left: 100, width: 260 }; }
}

const elements = new Map(ids.map((id) => [id, new Element()]));
const pending = [];
const document = {
  getElementById: (id) => {
    assert(elements.has(id), `Missing element: ${id}`);
    return elements.get(id);
  },
  createElement: (tag) => new Element(tag),
  createTextNode: (text) => {
    const item = new Element("#text");
    item.textContent = text;
    return item;
  },
  querySelectorAll: () => [],
  activeElement: null,
  documentElement: new Element(),
  hidden: false,
};
const context = vm.createContext({
  document,
  window: { innerWidth: 390 },
  URL,
  URLSearchParams,
  Intl,
  Map,
  performance: { now: () => 0 },
  setTimeout,
  clearTimeout,
  fetch: (...args) => new Promise((resolve) => pending.push({ args, resolve })),
});
const eventBindings = source.indexOf('$("login-form").addEventListener');
assert(eventBindings > 0, "frontend event binding boundary must exist");
vm.runInContext(source.slice(0, eventBindings), context, { filename: "dashboardplus/web/app.js" });
const run = (code) => vm.runInContext(code, context);
const fixture = (title, song) => ({
  cog: "intros",
  title,
  description: "Saved intros",
  columns: [{ key: "member", label: "Member" }, { key: "title", label: "Intro" }],
  rows: [{
    id: "4",
    values: { member: "<img src=x onerror=alert(1)>", title: song },
    image: "javascript:alert(1)",
    url: "https://youtube.com/watch?v=abc",
  }],
  page: 1,
  page_size: 25,
  total: 1,
  pages: 1,
});
const reply = (index, data) => pending[index].resolve({ ok: true, json: async () => data });

async function check() {
  run(`state.csrf="owner"; state.guild={id:"100",channel:"200"};
    state.datasets=[{id:"intros",label:"IntroPlus"}];
    state.dataset="intros"; state.page="data";`);
  const old = run("loadData()");
  assert.equal(pending.length, 1);
  assert.match(pending[0].args[0], /^\/api\/data\/100\?channel=200&cog=intros&page=1&query=$/);
  run('state.generation++; state.guild={id:"101",channel:"201"};');
  const current = run("loadData()");
  reply(1, fixture("Current server", "Song B"));
  await current;
  reply(0, fixture("Wrong server", "Song A"));
  await old;
  assert.equal(run("state.data.title"), "Current server", "late old-server results cannot replace current records");
  assert.equal(elements.get("data-title").textContent, "Current server");

  const table = elements.get("data-results").children[0].children[0];
  const cells = table.children[2].children[0].children;
  assert.equal(cells[0].children[0].children.length, 1, "unsafe thumbnail URLs must not render images");
  const identity = cells[0].children[0].children[0];
  assert.equal(identity.children[0].textContent, "<img src=x onerror=alert(1)>", "member content stays text");
  assert.equal(identity.children[1].href, "https://youtube.com/watch?v=abc");
  assert.equal(identity.children[1].rel, "noopener noreferrer");

  const fetched = pending.length;
  run('renderDatasets([{id:"intros",label:"IntroPlus"}]);');
  assert.equal(pending.length, fetched, "unchanged metadata polling must not fetch records again");
  run('state.dataQuery="a b & c"; state.dataPage=2;');
  const search = run("loadData()");
  assert.match(pending[2].args[0], /page=2&query=a\+b\+%26\+c$/);
  reply(2, { ...fixture("Current server", "Song B"), page: 2, total: 30, pages: 2 });
  await search;
  assert.equal(elements.get("data-next").disabled, true);
  assert.equal(elements.get("data-previous").disabled, false);

  const row = run(`settingRow({id:"voice",label:"Voice XP",description:"Award XP in voice channels.",
    global:false,kind:"bool",value:true})`);
  const help = row.children[0].children[1];
  assert.equal(help.children[1].textContent, "Award XP in voice channels.");
  assert.equal(help.children[1].attrs.role, "tooltip");
  assert.equal(help.children[0].attrs["aria-describedby"], "help-voice");
  assert.equal(row.children[1].children[0].attrs["aria-describedby"], "help-voice");
  help.children[0].listeners.focus();
  assert(help.classList.contains("help-open"), "keyboard focus should open the tooltip");
  help.children[0].listeners.keydown({ key: "Escape", stopPropagation() {} });
  assert(!help.classList.contains("help-open"), "Escape should close the tooltip");

  const stale = run("loadData()");
  run("resetData()");
  reply(3, fixture("Expired context", "Song C"));
  await stale;
  assert.equal(run("state.data"), null, "resetting context must invalidate pending data");
  assert.equal(elements.get("data-title").textContent, "Cog records");
  run("renderDatasets([])");
  assert.equal(elements.get("data-next").disabled, true);
  assert.equal(elements.get("data-cog").disabled, true);

  // A failed old-server metadata request must be as harmless as a stale success.
  elements.get("server").value = "100";
  elements.get("channel").value = "200";
  elements.get("server-error").hidden = true;
  run('state.page="overview"; state.pendingReset=true; state.guild={id:"100",channel:"200"};');
  const oldMetadata = run("refresh(true)");
  elements.get("server").value = "101";
  run('state.generation++; state.guild={id:"101",channel:"201"}; state.data={title:"Current records"};');
  pending[4].resolve({ ok: false, status: 403, json: async () => ({ error: "Old server denied" }) });
  for (let attempt = 0; attempt < 10 && pending.length < 6; attempt++) await Promise.resolve();
  assert.equal(pending.length, 6, "the new server metadata request should follow the stale request");
  assert.equal(elements.get("server-error").hidden, true, "old failures must not display in the new server");
  assert.equal(run("state.guild.id"), "101", "old failures must not clear the new server context");
  assert.equal(run("state.data.title"), "Current records", "old failures must not clear current records");
  reply(5, {
    id: "101", name: "Current server", channel: "201", colors: {},
    channels: [{ id: "201", name: "music" }], settings: [], cogs: [], datasets: [],
    music: { available: false, connected: false, current: null, queue: [] },
  });
  await oldMetadata;
  assert.equal(elements.get("server-summary").textContent, "Current server · Everything you need to keep things moving.");

  run('state.datasets=[{id:"intros",label:"IntroPlus"}]; state.dataset="intros"; state.data={title:"Private records"};');
  const forbidden = run("refresh()");
  pending[6].resolve({ ok: false, status: 403, json: async () => ({ error: "Command context denied" }) });
  await forbidden;
  assert.equal(run("state.guild"), null, "revoked context should clear the current server");
  assert.equal(run("state.data"), null, "revoked context should clear displayed records");
  assert.equal(elements.get("server-error").textContent, "Command context denied");
  console.log("Dashboard frontend checks passed.");
}

check().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
