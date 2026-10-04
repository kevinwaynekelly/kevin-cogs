"use strict";

const $ = (id) => document.getElementById(id);
const state = { csrf: "", guild: null, page: "overview", overview: null, forms: new Map(), filter: "All", generation: 0, refreshing: false, pendingReset: true, polledAt: 0, datasets: [], dataset: "", dataPage: 1, dataQuery: "", data: null, dataRequest: 0, dataLoading: false };
let toastTimer;

function node(tag, className = "", text = "") {
  const item = document.createElement(tag);
  item.className = className;
  item.textContent = text;
  return item;
}

function safeUrl(value) {
  try {
    const url = new URL(value);
    return ["http:", "https:"].includes(url.protocol) && !url.username && !url.password ? url.href : "";
  } catch { return ""; }
}

function image(value, className, alt = "") {
  const url = safeUrl(value);
  if (!url) return node("div", className, "♫");
  const item = node("img", className);
  item.src = url;
  item.alt = alt;
  item.loading = "lazy";
  item.referrerPolicy = "no-referrer";
  item.addEventListener("error", () => item.replaceWith(node("div", className, "♫")), { once: true });
  return item;
}

function formatTime(milliseconds) {
  const seconds = Math.max(0, Math.floor((milliseconds || 0) / 1000));
  const minutes = Math.floor(seconds / 60);
  return `${minutes}:${String(seconds % 60).padStart(2, "0")}`;
}

function toast(message, error = false) {
  $("toast").textContent = String(message).slice(0, 800);
  $("toast").className = `toast${error ? " error" : ""}`;
  $("toast").hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { $("toast").hidden = true; }, error ? 10000 : 5000);
}

function signedOut() {
  state.csrf = "";
  state.guild = null;
  state.forms.clear();
  state.pendingReset = true;
  state.generation++;
  resetData();
  $("app").hidden = true;
  $("signin").hidden = false;
  $("code").value = "";
}

async function api(path, body) {
  const response = await fetch(path, {
    method: body === undefined ? "GET" : "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": state.csrf },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  const data = await response.json();
  if (!response.ok) {
    if (response.status === 401 && path !== "/api/login") signedOut();
    const error = new Error(data.error || "The dashboard could not complete this request.");
    error.status = response.status;
    throw error;
  }
  return data;
}

function page(name) {
  if (!["overview", "music", "settings", "data", "cogs"].includes(name)) return;
  state.page = name;
  for (const element of document.querySelectorAll(".page")) element.hidden = element.id !== name;
  for (const button of document.querySelectorAll("[data-page]")) {
    button.classList.toggle("active", button.dataset.page === name);
    button.setAttribute("aria-current", button.dataset.page === name ? "page" : "false");
  }
  $("page-label").textContent = name[0].toUpperCase() + name.slice(1);
  if (name === "data" && !state.data && !state.dataLoading) loadData();
}

async function initialise() {
  const session = await api("/api/session");
  state.csrf = session.csrf;
  $("owner-name").textContent = session.owner;
  state.overview = await api("/api/overview");
  $("signin").hidden = true;
  $("app").hidden = false;
  $("brand-name").replaceChildren(document.createTextNode(state.overview.bot), node("span", "brand-sub", "DASHBOARDPLUS"));
  document.title = `${state.overview.bot} · Dashboard`;
  $("latency").textContent = state.overview.latency === null ? "—" : `${state.overview.latency} ms`;
  $("cog-count").textContent = state.overview.cogs.length;
  $("server-count").textContent = state.overview.servers.length;
  $("connection-label").textContent = state.overview.ready ? "Connected" : "Reconnecting";
  $("server").replaceChildren();
  for (const server of state.overview.servers) {
    const option = node("option", "", server.name);
    option.value = server.id;
    option.disabled = !server.accessible;
    $("server").append(option);
  }
  const first = state.overview.servers.find((server) => server.accessible);
  if (first) { $("server").value = first.id; await refresh(true); }
  else {
    $("server-error").textContent = "Join a server with your bot-owner account to manage it here.";
    $("server-error").hidden = false;
  }
}

async function refresh(reset = false) {
  if (!state.csrf || !$("server").value) return;
  if (state.refreshing) return;
  const generation = state.generation;
  const server = $("server").value;
  state.refreshing = true;
  try {
    const channel = reset ? "" : $("channel").value;
    const guild = await api(`/api/guild/${encodeURIComponent(server)}?channel=${encodeURIComponent(channel)}`);
    if (generation !== state.generation || !state.csrf) return;
    state.guild = guild;
    state.polledAt = performance.now();
    $("server-error").hidden = true;
    $("server-summary").textContent = `${guild.name} · Everything you need to keep things moving.`;
    if (reset || !$("channel").children.length) {
      $("channel").replaceChildren();
      for (const channel of guild.channels) {
        const option = node("option", "", `# ${channel.name}`);
        option.value = channel.id;
        $("channel").append(option);
      }
    }
    $("channel").value = guild.channel;
    document.documentElement.style.setProperty("--accent", /^#[0-9a-f]{6}$/i.test(guild.colors.info || "") ? guild.colors.info : "#9b91ff");
    renderMusic(guild.music);
    renderCogs(guild.cogs);
    renderSettings(guild.settings, reset);
    renderDatasets(guild.datasets || []);
    $("refresh-state").textContent = "Live · refreshed just now";
  } catch (error) {
    if (generation !== state.generation || !state.csrf) return;
    $("refresh-state").textContent = "Connection needs attention";
    $("server-error").textContent = error.message;
    $("server-error").hidden = false;
    if (reset || error.status === 403) {
      state.guild = null;
      renderMusic({ available: false, connected: false, current: null, queue: [] });
      $("settings-grid").replaceChildren();
      $("cog-list").replaceChildren();
      renderDatasets([]);
    }
  } finally {
    state.refreshing = false;
    if (generation !== state.generation && state.csrf) await refresh(state.pendingReset);
  }
}

function progress() {
  const music = state.guild?.music;
  const duration = music?.current?.duration || 0;
  const elapsed = music?.current && !music.paused && !music.preparing && music.connected ? performance.now() - state.polledAt : 0;
  const position = Math.max(0, Math.min((music?.position || 0) + elapsed, duration || Infinity));
  $("progress").max = duration || 1;
  $("progress").value = duration ? position : 0;
  $("position").textContent = formatTime(position);
  $("duration").textContent = music?.current ? duration ? formatTime(duration) : "Live" : "0:00";
}

function renderMusic(music) {
  const track = music.current;
  $("queue-count").textContent = music.queue.length;
  $("queue-badge").textContent = music.queue.length;
  $("voice-state").textContent = music.connected ? `♫ ${music.channel}` : "Not connected";
  $("voice-state").classList.toggle("enabled", music.connected);
  $("overview-player").replaceChildren(image(track?.thumbnail, "mini-art"));
  const description = node("div");
  description.append(node("h3", "", track?.title || "A little quiet in here."), node("p", "", track?.author || "Queue your next favorite from the music tab."));
  description.append(node("span", "badge", music.preparing ? "Preparing audio…" : track ? music.paused ? "Paused" : "Playing now" : music.available ? "Ready when you are" : "Load AudioPlus to play music"));
  $("overview-player").append(description);
  $("music-art").replaceChildren();
  if (track?.thumbnail) $("music-art").append(image(track.thumbnail, "", "Video thumbnail"));
  else $("music-art").textContent = "♫";
  $("track-title").textContent = track?.title || "A little quiet in here.";
  $("track-author").textContent = track?.author || (music.available ? "Queue a song to get started." : "Load AudioPlus to use music controls.");
  $("source-link").hidden = !safeUrl(track?.url);
  if (safeUrl(track?.url)) $("source-link").href = safeUrl(track.url);
  $("pause-control").dataset.action = music.paused ? "resume" : "pause";
  $("pause-control").textContent = music.paused ? "▶" : "Ⅱ";
  $("pause-control").title = music.paused ? "Resume" : "Pause";
  $("pause-control").setAttribute("aria-label", $("pause-control").title);
  if (document.activeElement !== $("volume")) $("volume").value = Math.min(200, music.volume ?? 100);
  $("volume-output").textContent = `${music.volume ?? 100}%`;
  if (document.activeElement !== $("repeat")) $("repeat").value = music.repeat || "off";
  $("listeners").textContent = music.connected ? `${music.listeners} listening · ${music.channel}` : "No voice connection";
  for (const button of document.querySelectorAll("#music [data-action]")) button.disabled = !music.available || !music.connected;
  $("play-form").querySelector("button").disabled = !music.available;
  $("volume").disabled = $("repeat").disabled = !music.available || !music.connected;
  $("queue-list").replaceChildren();
  if (!music.queue.length) {
    const empty = node("div", "empty-state");
    empty.append(node("span", "", "♫"), node("div", "", "The queue has room for something good."));
    $("queue-list").append(empty);
  }
  music.queue.forEach((item, index) => {
    const row = node("div", "queue-row");
    const info = node("div", "track-info");
    info.append(node("strong", "", item.title), node("small", "", item.author));
    const remove = node("button", "icon-button", "×");
    remove.title = `Remove song ${index + 1}`;
    remove.setAttribute("aria-label", remove.title);
    remove.addEventListener("click", () => action("music", "remove", index + 1, remove));
    row.append(node("span", "number", String(index + 1).padStart(2, "0")), image(item.thumbnail, "track-placeholder"), info, node("span", "track-duration", formatTime(item.duration)), remove);
    $("queue-list").append(row);
  });
  progress();
}

function renderCogs(cogs) {
  $("cog-list").replaceChildren();
  $("cogs-badge").textContent = `${cogs.length} loaded`;
  $("cog-count").textContent = cogs.length;
  for (const cog of cogs) {
    const row = node("div", "cog-row");
    row.append(node("span", "cog-icon", cog.name.replace(/[^A-Z]/g, "").slice(0, 2) || cog.name.slice(0, 2)), node("strong", "", cog.name), node("span", `badge${cog.enabled ? " enabled" : ""}`, cog.enabled ? "Enabled in server" : "Disabled in server"));
    $("cog-list").append(row);
  }
}

function settingRow(spec) {
  const row = node("form", "setting-row");
  const label = node("label", "setting-label", spec.label);
  label.htmlFor = `setting-${spec.id}`;
  if (spec.global) label.append(node("small", "", "Applies across all bot servers"));
  const heading = node("div", "setting-heading");
  const help = node("span", "setting-help");
  const helpButton = node("button", "help-button", "?");
  helpButton.type = "button";
  helpButton.setAttribute("aria-label", `About ${spec.label}`);
  const tooltip = node("span", "setting-tooltip", spec.description || `Configure ${spec.label.toLowerCase()} for this cog.`);
  tooltip.id = `help-${spec.id}`;
  tooltip.setAttribute("role", "tooltip");
  helpButton.setAttribute("aria-describedby", tooltip.id);
  const showHelp = () => {
    for (const other of document.querySelectorAll(".setting-help.help-open")) other.classList.remove("help-open");
    help.classList.add("help-open");
    const anchor = help.getBoundingClientRect();
    const width = tooltip.getBoundingClientRect().width;
    const left = Math.max(12, Math.min(anchor.left - 35, window.innerWidth - width - 12));
    tooltip.style.left = `${left - anchor.left}px`;
    tooltip.style.right = "auto";
    tooltip.style.setProperty("--tip-arrow", `${anchor.left + 5 - left}px`);
  };
  help.addEventListener("mouseenter", showHelp);
  help.addEventListener("mouseleave", () => { if (document.activeElement !== helpButton) help.classList.remove("help-open"); });
  helpButton.addEventListener("focus", showHelp);
  helpButton.addEventListener("blur", () => help.classList.remove("help-open"));
  helpButton.addEventListener("click", () => { showHelp(); });
  helpButton.addEventListener("keydown", (event) => { if (event.key === "Escape") { help.classList.remove("help-open"); event.stopPropagation(); } });
  help.append(helpButton, tooltip);
  heading.append(label, help);
  const controls = node("div", "setting-value");
  const fields = [];
  const saved = { spec, row, fields, dirty: false };
  if (["bool", "onoff"].includes(spec.kind)) {
    const input = node("input", "switch");
    input.type = "checkbox";
    input.id = label.htmlFor;
    input.checked = spec.value;
    fields.push(input);
    input.addEventListener("change", async () => {
      saved.dirty = true;
      const success = await action("setting", spec.id, input.checked, input);
      if (!success) input.checked = saved.spec.value;
      saved.dirty = false;
    });
    controls.append(input);
  } else {
    const keys = spec.kind === "limits" ? ["max_seconds", "per_member"] : [""];
    for (const key of keys) {
      let input;
      if (spec.kind === "channel") {
        input = node("select");
        const clear = node("option", "", "Not selected"); clear.value = "0"; input.append(clear);
        for (const channel of state.guild.channels) { const option = node("option", "", `# ${channel.name}`); option.value = channel.id; input.append(option); }
        input.value = spec.value;
      } else {
        input = node("input"); input.type = "number";
        input.min = key ? "0" : spec.min;
        input.max = key ? key === "max_seconds" ? "86400" : "100" : spec.max;
        input.step = spec.kind === "float" ? "0.1" : "1";
        input.value = key ? spec.value[key] : spec.value;
        if (key) { input.title = key === "max_seconds" ? "Maximum song length in seconds; zero disables the limit" : "Tracks per person; zero disables the limit"; input.setAttribute("aria-label", input.title); }
      }
      input.id = fields.length ? `${label.htmlFor}-${fields.length}` : label.htmlFor;
      input.dataset.key = key;
      input.addEventListener("input", () => { saved.dirty = true; });
      fields.push(input); controls.append(input);
    }
    const save = node("button", "save-button", "Save"); save.type = "submit"; controls.append(save);
    row.addEventListener("submit", async (event) => {
      event.preventDefault();
      const value = spec.kind === "limits" ? Object.fromEntries(fields.map((field) => [field.dataset.key, Number(field.value)])) : spec.kind === "channel" ? fields[0].value : Number(fields[0].value);
      if (await action("setting", spec.id, value, save)) saved.dirty = false;
    });
  }
  for (const field of fields) field.setAttribute("aria-describedby", tooltip.id);
  row.append(heading, controls);
  state.forms.set(spec.id, saved);
  return row;
}

function renderSettings(settings, reset) {
  if (!reset && settings.length === state.forms.size && settings.every((spec) => state.forms.has(spec.id))) {
    for (const spec of settings) {
      const saved = state.forms.get(spec.id);
      saved.spec = spec;
      if (saved.dirty || saved.fields.includes(document.activeElement)) continue;
      saved.fields.forEach((field) => {
        if (field.type === "checkbox") field.checked = spec.value;
        else field.value = field.dataset.key ? spec.value[field.dataset.key] : spec.value;
      });
    }
    return;
  }
  state.forms.clear();
  $("settings-grid").replaceChildren();
  $("setting-tabs").replaceChildren();
  const cogs = [...new Set(settings.map((spec) => spec.cog))];
  for (const name of ["All", ...cogs]) {
    const button = node("button", "filter-tab", name === "All" ? "All cogs" : name.replace(/Plus$/, ""));
    button.dataset.filter = name;
    button.addEventListener("click", () => { state.filter = name; filterSettings(); });
    $("setting-tabs").append(button);
  }
  for (const name of cogs) {
    const card = node("article", "card"); card.dataset.cog = name;
    const heading = node("div", "card-heading"); heading.append(node("h2", "", name.replace(/Plus$/, "")), node("span", "badge", "Live settings")); card.append(heading);
    for (const spec of settings.filter((spec) => spec.cog === name)) card.append(settingRow(spec));
    $("settings-grid").append(card);
  }
  if (!cogs.length) $("settings-grid").append(node("div", "empty-state", "No supported settings are available in this channel. Check loaded cogs and command permissions."));
  if (!cogs.includes(state.filter)) state.filter = "All";
  filterSettings();
}

function filterSettings() {
  for (const card of document.querySelectorAll("#settings-grid [data-cog]")) card.hidden = state.filter !== "All" && card.dataset.cog !== state.filter;
  for (const button of document.querySelectorAll("[data-filter]")) button.classList.toggle("active", button.dataset.filter === state.filter);
}

function resetData() {
  state.dataRequest++;
  state.dataLoading = false;
  state.datasets = [];
  state.dataset = "";
  state.dataPage = 1;
  state.dataQuery = "";
  state.data = null;
  $("data-query").value = "";
  $("data-cog").replaceChildren();
  $("data-title").textContent = "Cog records";
  $("data-description").textContent = "";
  $("data-total").textContent = "No data selected";
  $("data-page-status").textContent = "";
  $("data-results").replaceChildren(node("div", "empty-state", "Choose an available server and channel to browse cog records."));
  $("data-results").setAttribute("aria-busy", "false");
  dataControls();
}

function dataControls() {
  const available = Boolean(state.guild && state.datasets.length);
  $("data-cog").disabled = !available;
  $("data-query").disabled = !available;
  $("data-search-form").querySelector("button").disabled = !available || state.dataLoading;
  $("data-refresh").disabled = !available || state.dataLoading;
  $("data-previous").disabled = !state.data || state.dataLoading || state.data.page <= 1;
  $("data-next").disabled = !state.data || state.dataLoading || state.data.page >= state.data.pages;
}

function renderDatasets(datasets) {
  const changed = JSON.stringify(state.datasets) !== JSON.stringify(datasets);
  const previous = state.dataset;
  state.datasets = datasets;
  if (!datasets.some((item) => item.id === state.dataset)) state.dataset = datasets[0]?.id || "";
  if (changed) {
    $("data-cog").replaceChildren();
    for (const dataset of datasets) {
      const option = node("option", "", dataset.label);
      option.value = dataset.id;
      $("data-cog").append(option);
    }
    $("data-cog").value = state.dataset;
  }
  if (previous !== state.dataset) {
    state.dataRequest++;
    state.dataLoading = false;
    state.data = null;
    state.dataPage = 1;
    state.dataQuery = "";
    $("data-query").value = "";
  }
  const selected = datasets.find((item) => item.id === state.dataset);
  if (!state.data) {
    $("data-title").textContent = selected?.label || "Cog records";
    $("data-description").textContent = selected?.description || "";
  }
  if (!datasets.length) {
    state.dataRequest++;
    state.dataLoading = false;
    state.data = null;
    $("data-total").textContent = "No available views";
    $("data-page-status").textContent = "";
    $("data-results").replaceChildren(node("div", "empty-state", "No cog data views are available in this channel. Check loaded cogs and command permissions."));
    $("data-results").setAttribute("aria-busy", "false");
  }
  dataControls();
  if (state.page === "data" && selected && !state.data && !state.dataLoading) loadData();
}

function dataValue(value) {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (typeof value === "number") return new Intl.NumberFormat().format(value);
  return String(value);
}

function renderData(result) {
  $("data-title").textContent = result.title;
  $("data-description").textContent = result.description;
  $("data-total").textContent = `${new Intl.NumberFormat().format(result.total)} ${result.total === 1 ? "record" : "records"}`;
  $("data-results").replaceChildren();
  if (!result.rows.length) {
    $("data-results").append(node("div", "empty-state", state.dataQuery ? "No records match this search. Try a member name, ID or another term." : "There are no records to display for this cog yet."));
  } else {
    const wrap = node("div", "data-table-wrap");
    const table = node("table", "data-table");
    const caption = node("caption", "sr-only", `${result.title} records`);
    const head = node("thead");
    const headRow = node("tr");
    for (const column of result.columns) { const cell = node("th", "", column.label); cell.scope = "col"; headRow.append(cell); }
    head.append(headRow);
    const body = node("tbody");
    for (const record of result.rows) {
      const row = node("tr");
      result.columns.forEach((column, index) => {
        const cell = node("td");
        cell.dataset.label = column.label;
        const content = node("div", index === 0 ? "data-identity" : "data-cell");
        if (index === 0 && safeUrl(record.image)) content.append(image(record.image, "data-art", "Record thumbnail"));
        const text = node("span", "data-value", dataValue(record.values[column.key]));
        if (index === 0 && safeUrl(record.url)) {
          const link = node("a", "data-source", "Open source ↗");
          link.href = safeUrl(record.url); link.target = "_blank"; link.rel = "noopener noreferrer";
          const detail = node("div"); detail.append(text, link); content.append(detail);
        } else content.append(text);
        cell.append(content); row.append(cell);
      });
      body.append(row);
    }
    table.append(caption, head, body); wrap.append(table); $("data-results").append(wrap);
  }
  const first = result.total ? (result.page - 1) * result.page_size + 1 : 0;
  const last = Math.min(result.page * result.page_size, result.total);
  $("data-page-status").textContent = result.total ? `${first}–${last} of ${new Intl.NumberFormat().format(result.total)} · Page ${result.page} of ${result.pages}` : "0 records";
}

async function loadData() {
  if (!state.csrf || !state.guild || !state.dataset) { dataControls(); return; }
  const request = ++state.dataRequest;
  const generation = state.generation;
  const guild = state.guild.id;
  const channel = state.guild.channel;
  const dataset = state.dataset;
  const params = new URLSearchParams({ channel, cog: dataset, page: String(state.dataPage), query: state.dataQuery });
  state.dataLoading = true;
  state.data = null;
  $("data-total").textContent = "Loading…";
  $("data-page-status").textContent = "";
  $("data-results").setAttribute("aria-busy", "true");
  $("data-results").replaceChildren(node("div", "empty-state", "Reading cog records…"));
  dataControls();
  try {
    const result = await api(`/api/data/${encodeURIComponent(guild)}?${params}`);
    if (request !== state.dataRequest || generation !== state.generation || !state.csrf) return;
    state.data = result;
    state.dataPage = result.page;
    renderData(result);
  } catch (error) {
    if (request !== state.dataRequest || generation !== state.generation) return;
    $("data-total").textContent = "Could not load records";
    const message = node("div", "empty-state error-text", error.message);
    message.setAttribute("role", "alert");
    $("data-results").replaceChildren(message);
  } finally {
    if (request === state.dataRequest && generation === state.generation) {
      state.dataLoading = false;
      $("data-results").setAttribute("aria-busy", "false");
      dataControls();
    }
  }
}

function plain(value) {
  return String(value || "").replace(/\[([^\]]+)\]\([^)]*\)/g, "$1").replace(/[*`]/g, "").replace(/<#[0-9]+>/g, "the selected channel");
}

async function action(kind, name, value = null, button = null) {
  if (!state.guild) { toast("Choose an available server and channel first.", true); return false; }
  if (button) button.disabled = true;
  try {
    const result = await api("/api/action", { guild: state.guild.id, channel: state.guild.channel, kind, action: name, value });
    const text = result.messages.flatMap((message) => [message.content, ...message.embeds.map((embed) => embed.description || embed.title)]).filter(Boolean).map(plain).join("\n");
    toast(text || "Done.", result.failed);
    await refresh();
    return !result.failed;
  } catch (error) { toast(error.message, true); return false; }
  finally { if (button) button.disabled = false; }
}

$("login-form").addEventListener("submit", async (event) => {
  event.preventDefault(); $("login-submit").disabled = true; $("login-error").textContent = "";
  try { await api("/api/login", { code: $("code").value.trim() }); $("code").value = ""; await initialise(); }
  catch (error) { $("login-error").textContent = error.message; }
  finally { $("login-submit").disabled = false; }
});
for (const id of ["logout", "mobile-logout"]) $(id).addEventListener("click", async () => { try { await api("/api/logout", {}); signedOut(); } catch (error) { toast(error.message, true); } });
for (const button of document.querySelectorAll("[data-page],[data-open]")) button.addEventListener("click", () => page(button.dataset.page || button.dataset.open));
for (const button of document.querySelectorAll("[data-action]")) button.addEventListener("click", () => action("music", button.dataset.action, null, button));
$("server").addEventListener("change", () => { state.generation++; state.pendingReset = true; state.guild = null; state.forms.clear(); resetData(); refresh(true); });
$("channel").addEventListener("change", () => { state.generation++; state.pendingReset = false; state.guild = null; state.forms.clear(); resetData(); refresh(false); });
$("data-cog").addEventListener("change", () => {
  state.dataset = $("data-cog").value; state.dataPage = 1; state.dataQuery = ""; $("data-query").value = "";
  const selected = state.datasets.find((item) => item.id === state.dataset);
  $("data-title").textContent = selected?.label || "Cog records";
  $("data-description").textContent = selected?.description || "";
  loadData();
});
$("data-search-form").addEventListener("submit", (event) => { event.preventDefault(); state.dataPage = 1; state.dataQuery = $("data-query").value.trim(); loadData(); });
$("data-refresh").addEventListener("click", () => loadData());
$("data-previous").addEventListener("click", () => { state.dataPage = Math.max(1, state.dataPage - 1); loadData(); });
$("data-next").addEventListener("click", () => { if (state.data) state.dataPage = Math.min(state.data.pages, state.dataPage + 1); loadData(); });
document.addEventListener("click", (event) => { if (!event.target.closest(".setting-help")) for (const help of document.querySelectorAll(".setting-help.help-open")) help.classList.remove("help-open"); });
$("play-form").addEventListener("submit", async (event) => { event.preventDefault(); if (await action("music", "play", $("query").value, $("play-form").querySelector("button"))) $("query").value = ""; });
$("volume").addEventListener("input", () => { $("volume-output").textContent = `${$("volume").value}%`; });
$("volume").addEventListener("change", () => action("music", "volume", Number($("volume").value), $("volume")));
$("repeat").addEventListener("change", () => action("music", "repeat", $("repeat").value, $("repeat")));
setInterval(() => { if (state.csrf && !document.hidden) refresh(); }, 5000);
setInterval(() => { if (state.csrf && !document.hidden) progress(); }, 1000);
document.addEventListener("visibilitychange", () => { if (!document.hidden && state.csrf) refresh(); });
initialise().catch(() => signedOut());
