/* Syslab server dashboard (Step 11). No framework, no build step, no external requests.
   Every piece of server-supplied text goes through esc() before it reaches innerHTML:
   file and folder names come from uploads, and a name is not trusted markup. */
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const enc = encodeURIComponent;
const saved = {
  get(key) { try { return localStorage.getItem(key); } catch { return null; } },
  set(key, value) { try { localStorage.setItem(key, value); } catch { /* private window */ } },
};

const state = { operator: null, companies: [], accepted: [], timer: null, lastUpload: null };

const ICON = {
  folder: '<svg class="folder-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>',
  file: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/></svg>',
  upload: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 16V4"/><path d="m7 9 5-5 5 5"/><path d="M5 20h14"/></svg>',
  plus: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true"><path d="M12 5v14M5 12h14"/></svg>',
  play: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 12a9 9 0 1 1-3-6.7"/><path d="M21 4v5h-5"/></svg>',
  download: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 4v12"/><path d="m7 11 5 5 5-5"/><path d="M5 20h14"/></svg>',
  trash: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 7h16"/><path d="M10 11v6M14 11v6"/><path d="M6 7l1 13h10l1-13"/><path d="M9 7V4h6v3"/></svg>',
  server: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="4" width="18" height="7" rx="2"/><rect x="3" y="13" width="18" height="7" rx="2"/><path d="M7 7.5h.01M7 16.5h.01"/></svg>',
  chat: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 12a8 8 0 0 1-11.6 7.1L4 20l1-4.6A8 8 0 1 1 21 12z"/></svg>',
  vector: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="6" cy="6" r="2"/><circle cx="18" cy="8" r="2"/><circle cx="9" cy="18" r="2"/><path d="m7.7 7.3 1 8.8M8 6.3l8 1.5M10.7 17l6-7.4"/></svg>',
  jobs: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M8 6h13M8 12h13M8 18h13"/><path d="M3 6h.01M3 12h.01M3 18h.01"/></svg>',
  building: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 21h18"/><path d="M5 21V7l7-4 7 4v14"/><path d="M9 21v-5h6v5"/></svg>',
};

// ------------------------------------------------------------------ API

class ApiError extends Error {
  constructor(message, status) { super(message); this.status = status; }
}

async function api(path, { method = "GET", body, form } = {}) {
  const options = { method, headers: {}, credentials: "same-origin" };
  // Every write carries this header; the server refuses writes without it.
  if (method !== "GET") options.headers["X-Syslab-Admin"] = "1";
  if (form) options.body = form;
  else if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const res = await fetch(`/admin/api/${path}`, options);
  let data = null;
  try { data = await res.json(); } catch { /* not JSON */ }
  if (res.status === 401 && path !== "login") {
    // Only say "ended" if there was a session to end; a first visit just gets the form.
    showLogin(state.operator ? "Your session ended. Sign in again." : "");
    throw new ApiError("Signed out.", 401);
  }
  if (!res.ok) {
    const detail = data && (typeof data.detail === "string" ? data.detail
      : Array.isArray(data.detail) ? data.detail.map((d) => d.msg).join("; ") : "");
    throw new ApiError(detail || `${res.status} ${res.statusText}`, res.status);
  }
  return data;
}

// ------------------------------------------------------------------ small helpers

function toast(message, bad = false) {
  const el = document.createElement("div");
  el.className = `toast${bad ? " bad" : ""}`;
  el.textContent = message;
  $("#toasts").append(el);
  setTimeout(() => el.remove(), bad ? 7000 : 4000);
}

function fmtBytes(n) {
  if (!n) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const i = Math.min(units.length - 1, Math.floor(Math.log(n) / Math.log(1024)));
  return `${(n / 1024 ** i).toFixed(i ? 1 : 0)} ${units[i]}`;
}

function fmtTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return isNaN(d) ? esc(iso) : d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

function fmtUptime(s) {
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m`;
}

const STATE_BADGE = {
  ready: '<span class="badge ok">Ready</span>',
  outstanding: '<span class="badge warn">Outstanding</span>',
  failed: '<span class="badge bad">Failed</span>',
  error: '<span class="badge bad">Error</span>',
  unsupported: '<span class="badge off">Not indexed</span>',
};

function stopPolling() { clearTimeout(state.timer); state.timer = null; }

function openDialog(html, onSubmit) {
  const dialog = $("#dialog"), form = $("#dialog-form");
  form.innerHTML = html;
  form.onsubmit = async (event) => {
    if (event.submitter && event.submitter.value === "cancel") return; // closes on its own
    event.preventDefault();
    const error = $(".error", form);
    const button = event.submitter;
    if (button) button.disabled = true;
    try {
      await onSubmit(new FormData(form));
      dialog.close();
    } catch (err) {
      if (error) { error.textContent = err.message; error.hidden = false; }
    } finally {
      if (button) button.disabled = false;
    }
  };
  dialog.showModal();
  const first = $("input, textarea, select", form);
  if (first) first.focus();
}

async function loadCompanies() {
  const data = await api("companies");
  state.companies = data.companies;
  state.accepted = data.accepted_types;
  return state.companies;
}

function currentCompany(fromRoute) {
  const ids = state.companies.map((c) => c.id);
  const wanted = fromRoute || saved.get("syslab-company");
  const id = ids.includes(wanted) ? wanted : ids[0];
  if (id) saved.set("syslab-company", id);
  return id;
}

function companyPicker(selected, page) {
  if (!state.companies.length) return "";
  return `<label class="field" style="min-width:240px">
    <span class="sr-only">Company</span>
    <select id="company-picker" data-page="${page}">
      ${state.companies.map((c) => `<option value="${esc(c.id)}"${c.id === selected ? " selected" : ""}>${esc(c.name)} (${esc(c.id)})</option>`).join("")}
    </select></label>`;
}

function wirePicker() {
  const picker = $("#company-picker");
  if (picker) picker.onchange = () => { location.hash = `#/${picker.dataset.page}/${enc(picker.value)}`; };
}

// ------------------------------------------------------------------ session

function showLogin(message) {
  stopPolling();
  state.operator = null;
  $("#app").hidden = true;
  $("#login").hidden = false;
  const error = $("#login-error");
  error.hidden = !message;
  error.textContent = message || "";
  if (message) error.style.cssText = "color:var(--muted);background:var(--surface-2)";
  $("#login-form [name=username]").focus();
}

function showApp(operator) {
  state.operator = operator;
  $("#login").hidden = true;
  $("#app").hidden = false;
  $("#who-name").textContent = operator.display_name;
  $("#who-id").textContent = operator.id;
  $("#who-initial").textContent = (operator.display_name || operator.id).trim().charAt(0).toUpperCase();
  route();
}

$("#login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(event.target);
  const error = $("#login-error");
  error.hidden = true;
  error.style.cssText = "";
  const button = $("button[type=submit]", event.target);
  button.disabled = true;
  try {
    const data = await api("login", { method: "POST", body: { username: form.get("username"), password: form.get("password") } });
    event.target.reset();
    showApp(data.operator);
  } catch (err) {
    error.textContent = err.message;
    error.hidden = false;
  } finally {
    button.disabled = false;
  }
});

$("#signout").addEventListener("click", async () => {
  try { await api("logout", { method: "POST" }); } catch { /* signed out either way */ }
  showLogin("Signed out.");
});

$("#password-button").addEventListener("click", () => {
  openDialog(`
    <h3>Change your password</h3>
    <p>At least 12 characters. You will be signed out everywhere and asked to sign in again.</p>
    <label class="field">Current password<input type="password" name="current" autocomplete="current-password" required></label>
    <label class="field">New password<input type="password" name="new" autocomplete="new-password" minlength="12" required></label>
    <label class="field">New password again<input type="password" name="again" autocomplete="new-password" minlength="12" required></label>
    <div class="error" hidden></div>
    <div class="dialog-actions"><button class="btn" value="cancel">Cancel</button><button class="btn primary" value="ok">Change password</button></div>`,
  async (form) => {
    if (form.get("new") !== form.get("again")) throw new Error("The two new passwords do not match.");
    await api("me/password", { method: "POST", body: { current: form.get("current"), new: form.get("new") } });
    showLogin("Password changed. Sign in with the new one.");
  });
});

$("#theme-toggle").addEventListener("click", () => {
  const root = document.documentElement;
  const dark = root.dataset.theme ? root.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
  root.dataset.theme = dark ? "light" : "dark";
  saved.set("syslab-theme", root.dataset.theme);
});

// ------------------------------------------------------------------ routing

const PAGES = { overview: renderOverview, companies: renderCompanies, corpus: renderCorpus, retrieval: renderRetrieval };

function route() {
  if (!state.operator) return;
  stopPolling();
  const [path, query = ""] = location.hash.replace(/^#\/?/, "").split("?");
  const [page, ...rest] = path.split("/");
  const name = PAGES[page] ? page : "overview";
  $$(".nav a").forEach((a) => { if (a.dataset.page === name) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current"); });
  const view = $("#view");
  view.innerHTML = '<p class="muted">Loading…</p>';
  PAGES[name](view, rest.map(decodeURIComponent), new URLSearchParams(query)).catch((err) => {
    if (err.status !== 401) view.innerHTML = `<div class="error">${esc(err.message)}</div>`;
  });
}

window.addEventListener("hashchange", route);

// ------------------------------------------------------------------ overview

async function renderOverview(view) {
  const data = await api("overview");
  const chat = data.chat, embed = data.embed;
  const chatModel = chat.reachable && chat.models[0];
  let embedBadge, embedSub;
  if (embed.reachable && embed.registered) { embedBadge = '<span class="badge ok">In use</span>'; embedSub = esc(embed.models[0]?.id || ""); }
  else if (embed.reachable) { embedBadge = '<span class="badge warn">Not in use</span>'; embedSub = "Reachable, but the app started without it. Restart the app to use it."; }
  else { embedBadge = '<span class="badge warn">Unavailable</span>'; embedSub = `<span title="${esc(embed.error)}">Nothing answering at <span class="mono">${esc(embed.base_url)}</span>. Search runs on keywords only.</span>`; }

  view.innerHTML = `
    <div class="page-head"><div><h2>Overview</h2><p>How the server is doing right now.</p></div>
      <div class="toolbar"><button class="btn" id="refresh">${ICON.play}Refresh</button></div></div>
    <div class="grid">
      <div class="card pad"><div class="card-title">${ICON.server}App</div>
        <div class="stat">${fmtUptime(data.app.uptime_seconds)}</div>
        <div class="stat-sub">Up since ${fmtTime(data.app.started_at)}<br><span class="mono">${esc(data.app.code_fingerprint)}</span></div></div>
      <div class="card pad"><div class="card-title">${ICON.chat}Chat model</div>
        <div class="stat">${chat.reachable ? '<span class="badge ok">Answering</span>' : '<span class="badge bad">Unreachable</span>'}</div>
        <div class="stat-sub">${chatModel ? `${esc(chatModel.id)}${chatModel.max_model_len ? ` · ${Number(chatModel.max_model_len).toLocaleString()} tokens` : ""}` : `<span title="${esc(chat.error)}">Nothing answering at <span class="mono">${esc(chat.base_url)}</span></span>`}</div></div>
      <div class="card pad"><div class="card-title">${ICON.vector}Embeddings</div>
        <div class="stat">${embedBadge}</div><div class="stat-sub">${embedSub}</div></div>
      <div class="card pad"><div class="card-title">${ICON.jobs}Jobs</div>
        <div class="stat">${data.jobs.running} running</div>
        <div class="stat-sub">${data.jobs.queued} queued · ${data.jobs.workers} worker${data.jobs.workers === 1 ? "" : "s"} · lost on restart</div></div>
      <div class="card pad"><div class="card-title">${ICON.building}Companies</div>
        <div class="stat">${data.companies.total}</div>
        <div class="stat-sub">${data.companies.active} active · ${data.companies.documents.toLocaleString()} files</div></div>
    </div>
    <div class="section"><h3>Recent activity</h3>
      <div class="card">${data.recent.length ? `<ul class="activity">${data.recent.map((r) => `
        <li><time>${fmtTime(r.at)}</time><span><b>${esc(r.operator_id || "—")}</b> ${esc(r.action.replaceAll("_", " "))}${r.target ? ` <span class="mono">${esc(r.target)}</span>` : ""}</span></li>`).join("")}</ul>`
        : '<div class="empty">Nothing yet.</div>'}</div></div>`;
  $("#refresh").onclick = route;
  state.timer = setTimeout(route, 15000);
}

// ------------------------------------------------------------------ companies

function slugify(name) {
  let slug = name.toLowerCase().normalize("NFKD").replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
  if (!/^[a-z]/.test(slug)) slug = `c-${slug}`;
  return slug.slice(0, 32).replace(/-+$/, "");
}

async function renderCompanies(view) {
  const companies = await loadCompanies();
  view.innerHTML = `
    <div class="page-head"><div><h2>Companies</h2><p>Each company has its own document folder, index and tokens.</p></div>
      <div class="toolbar"><button class="btn primary" id="new-company">${ICON.plus}New company</button></div></div>
    <div class="card table-wrap">${companies.length ? `<table>
      <thead><tr><th>Company</th><th>Status</th><th class="num">Documents</th><th class="num hide-sm">Ready</th><th class="num hide-sm hide-md">Outstanding</th><th class="num hide-sm">Failed</th><th class="num hide-sm">Size</th><th class="hide-sm hide-md">Created</th><th></th></tr></thead>
      <tbody>${companies.map((c) => `
        <tr>
          <td><div class="row-name">${ICON.building}<div><a href="#/corpus/${enc(c.id)}"><b>${esc(c.name)}</b></a><div class="mono muted">${esc(c.id)}</div></div></div></td>
          <td>${c.bootstrap ? '<span class="badge off">Bootstrap</span>' : c.active ? '<span class="badge ok">Active</span>' : '<span class="badge warn">Disabled</span>'}</td>
          <td class="num">${c.documents}</td>
          <td class="num hide-sm">${c.ready}</td>
          <td class="num hide-sm hide-md">${c.outstanding}</td>
          <td class="num hide-sm">${c.failed ? `<span style="color:var(--danger)">${c.failed}</span>` : 0}</td>
          <td class="num hide-sm">${fmtBytes(c.bytes)}</td>
          <td class="hide-sm hide-md when">${fmtTime(c.created_at)}</td>
          <td class="actions">
            <a class="btn ghost" href="#/corpus/${enc(c.id)}">Open</a>
            ${c.bootstrap ? "" : c.active
              ? `<button class="btn ghost" data-act="disable" data-id="${esc(c.id)}">Disable</button>`
              : `<button class="btn ghost" data-act="enable" data-id="${esc(c.id)}">Enable</button>
                 <button class="btn danger" data-act="delete" data-id="${esc(c.id)}">Delete</button>`}
          </td>
        </tr>`).join("")}</tbody></table>`
      : '<div class="empty">No companies yet. Create one to start a corpus.</div>'}</div>`;

  $("#new-company").onclick = () => {
    openDialog(`
      <h3>New company</h3>
      <p>Creates the company and its empty document folder.</p>
      <label class="field">Name<input type="text" name="name" maxlength="100" required placeholder="Acme Trading LLC"></label>
      <label class="field">Id <span class="hint">Lower-case letters, digits and dashes. Used in folder names; cannot be changed later.</span>
        <input type="text" name="id" maxlength="32" required pattern="[a-z][a-z0-9_\\-]{0,31}" class="mono"></label>
      <div class="error" hidden></div>
      <div class="dialog-actions"><button class="btn" value="cancel">Cancel</button><button class="btn primary" value="ok">Create</button></div>`,
    async (form) => {
      const data = await api("companies", { method: "POST", body: { name: form.get("name"), id: form.get("id") } });
      toast(`Created ${data.company.name}.`);
      location.hash = `#/corpus/${enc(data.company.id)}`;
    });
    const name = $("#dialog-form [name=name]"), id = $("#dialog-form [name=id]");
    let touched = false;
    id.oninput = () => { touched = true; };
    name.oninput = () => { if (!touched) id.value = slugify(name.value); };
  };

  $$("[data-act]", view).forEach((button) => {
    button.onclick = async () => {
      const id = button.dataset.id, act = button.dataset.act;
      if (act === "delete") {
        openDialog(`
          <h3>Delete ${esc(id)}?</h3>
          <p>Its tokens, index and derived data are deleted. Its documents are moved to <span class="mono">data/_removed/</span>, not destroyed.</p>
          <label class="field">Type <span class="mono">${esc(id)}</span> to confirm<input type="text" name="confirm" required autocomplete="off" class="mono"></label>
          <div class="error" hidden></div>
          <div class="dialog-actions"><button class="btn" value="cancel">Cancel</button><button class="btn danger" value="ok">Delete company</button></div>`,
        async (form) => {
          await api(`companies/${enc(id)}?confirm=${enc(form.get("confirm"))}`, { method: "DELETE" });
          toast(`Deleted ${id}.`);
          route();
        });
        return;
      }
      button.disabled = true;
      try {
        await api(`companies/${enc(id)}/${act}`, { method: "POST" });
        toast(act === "disable" ? `Disabled ${id}. Its tokens stop working now.` : `Enabled ${id}.`);
        route();
      } catch (err) { toast(err.message, true); button.disabled = false; }
    };
  });
}

// ------------------------------------------------------------------ corpus

async function renderCorpus(view, [routeCompany], params) {
  await loadCompanies();
  const company = currentCompany(routeCompany);
  if (!company) {
    view.innerHTML = `<div class="page-head"><div><h2>Corpus</h2></div></div>
      <div class="card empty">No companies yet. <a href="#/companies">Create one first.</a></div>`;
    return;
  }
  const path = params.get("path") || "";
  const listing = await api(`companies/${enc(company)}/files?path=${enc(path)}`);
  const info = state.companies.find((c) => c.id === company);
  const crumbs = [`<a href="#/corpus/${enc(company)}">${esc(info.name)}</a>`];
  let walked = "";
  for (const part of listing.path ? listing.path.split("/") : []) {
    walked = walked ? `${walked}/${part}` : part;
    crumbs.push('<span class="sep">/</span>', `<a href="#/corpus/${enc(company)}?path=${enc(walked)}">${esc(part)}</a>`);
  }

  view.innerHTML = `
    <div class="page-head"><div><h2>Corpus</h2><p>${info.documents} documents · ${info.ready} ready · ${info.outstanding} outstanding · ${info.failed} failed</p></div>
      <div class="toolbar">${companyPicker(company, "corpus")}</div></div>
    <div class="toolbar" style="margin-bottom:14px">
      <button class="btn primary" id="pick-files">${ICON.upload}Upload files</button>
      <button class="btn" id="pick-folder">${ICON.upload}Upload folder</button>
      <button class="btn" id="new-folder">${ICON.plus}New folder</button>
      <button class="btn" id="ingest">${ICON.play}Ingest now</button>
      <label class="check"><input type="checkbox" id="replace"> Replace files that already exist</label>
      <input type="file" id="files-input" multiple hidden>
      <input type="file" id="folder-input" webkitdirectory multiple hidden>
    </div>
    <div class="card" id="upload-panel" hidden></div>
    <nav class="crumbs" aria-label="Folder">${crumbs.join("")}</nav>
    <div class="card table-wrap drop" id="drop">${listing.folders.length || listing.files.length ? `<table>
      <thead><tr><th>Name</th><th>State</th><th class="num hide-sm">Size</th><th class="hide-sm">Modified</th><th></th></tr></thead>
      <tbody>
      ${listing.folders.map((f) => `<tr>
        <td colspan="4"><div class="row-name">${ICON.folder}<a href="#/corpus/${enc(company)}?path=${enc(f.path)}">${esc(f.name)}</a></div></td>
        <td class="actions"><button class="btn ghost icon" data-trash="${esc(f.path)}" data-kind="folder" aria-label="Move folder ${esc(f.name)} to trash" title="Move to trash">${ICON.trash}</button></td></tr>`).join("")}
      ${listing.files.map((f) => `<tr>
        <td><div class="row-name">${ICON.file}<span title="${esc(f.path)}">${esc(f.name)}</span></div>${f.detail ? `<div class="detail">${esc(f.detail)}</div>` : ""}</td>
        <td>${STATE_BADGE[f.state] || esc(f.state)}</td>
        <td class="num hide-sm">${fmtBytes(f.size)}</td>
        <td class="hide-sm when">${fmtTime(f.modified)}</td>
        <td class="actions">
          <a class="btn ghost icon" href="/admin/api/companies/${enc(company)}/download?path=${enc(f.path)}" aria-label="Download ${esc(f.name)}" title="Download">${ICON.download}</a>
          ${f.state === "unsupported" ? "" : `<button class="btn ghost icon" data-reingest="${esc(f.path)}" aria-label="Ingest ${esc(f.name)} again" title="Ingest again">${ICON.play}</button>`}
          <button class="btn ghost icon" data-trash="${esc(f.path)}" data-kind="file" aria-label="Move ${esc(f.name)} to trash" title="Move to trash">${ICON.trash}</button>
        </td></tr>`).join("")}
      </tbody></table>`
      : '<div class="empty">This folder is empty. Upload files or a folder, or drop them here.</div>'}</div>
    <div class="section" id="jobs-section"></div>`;

  wirePicker();
  const reload = () => route();
  $("#pick-files").onclick = () => $("#files-input").click();
  $("#pick-folder").onclick = () => $("#folder-input").click();
  $("#files-input").onchange = (e) => upload(company, listing.path, [...e.target.files].map((file) => ({ file, dir: "" })));
  $("#folder-input").onchange = (e) => upload(company, listing.path, [...e.target.files].map((file) => ({
    file, dir: file.webkitRelativePath.split("/").slice(0, -1).join("/"),
  })));

  const drop = $("#drop");
  drop.ondragover = (e) => { e.preventDefault(); drop.classList.add("over"); };
  drop.ondragleave = (e) => { if (!drop.contains(e.relatedTarget)) drop.classList.remove("over"); };
  drop.ondrop = async (e) => {
    e.preventDefault();
    drop.classList.remove("over");
    // Read the entries NOW: a DataTransfer is empty once this handler awaits.
    const entries = [...e.dataTransfer.items].map((i) => i.webkitGetAsEntry && i.webkitGetAsEntry()).filter(Boolean);
    const items = [];
    for (const entry of entries) await walkEntry(entry, "", items);
    upload(company, listing.path, items);
  };

  $("#new-folder").onclick = () => openDialog(`
      <h3>New folder</h3><p>Inside <span class="mono">/${esc(listing.path)}</span></p>
      <label class="field">Folder name<input type="text" name="name" required maxlength="120"></label>
      <div class="error" hidden></div>
      <div class="dialog-actions"><button class="btn" value="cancel">Cancel</button><button class="btn primary" value="ok">Create</button></div>`,
    async (form) => {
      const made = await api(`companies/${enc(company)}/folders`, { method: "POST", body: { path: listing.path ? `${listing.path}/${form.get("name")}` : form.get("name") } });
      location.hash = `#/corpus/${enc(company)}?path=${enc(made.path)}`;
    });

  $("#ingest").onclick = () => startIngest(company);

  $$("[data-reingest]", view).forEach((b) => {
    b.onclick = async () => {
      b.disabled = true;
      try {
        const out = await api(`companies/${enc(company)}/ingest-file`, { method: "POST", body: { path: b.dataset.reingest } });
        toast(out.queued ? "Fast steps done; the rest is queued." : "Ingested.");
        reload();
      } catch (err) { toast(err.message, true); b.disabled = false; }
    };
  });

  $$("[data-trash]", view).forEach((b) => {
    b.onclick = () => openDialog(`
        <h3>Move to trash?</h3>
        <p><span class="mono">${esc(b.dataset.trash)}</span>${b.dataset.kind === "folder" ? " and everything inside it" : ""} leaves the corpus and stops being searchable. It is moved to the server's trash folder, not deleted.</p>
        <div class="error" hidden></div>
        <div class="dialog-actions"><button class="btn" value="cancel">Cancel</button><button class="btn danger" value="ok">Move to trash</button></div>`,
      async () => {
        await api(`companies/${enc(company)}/trash`, { method: "POST", body: { path: b.dataset.trash } });
        toast("Moved to trash.");
        reload();
      });
  });

  if (state.lastUpload && state.lastUpload.company === company) showUploadSummary(state.lastUpload.html);
  renderJobs(company);
}

function showUploadSummary(html) {
  const panel = $("#upload-panel");
  if (!panel) return;
  panel.innerHTML = html;
  panel.hidden = false;
  const close = $("#close-upload");
  if (close) close.onclick = () => { panel.hidden = true; state.lastUpload = null; };
}

async function walkEntry(entry, dir, out) {
  if (entry.isFile) {
    out.push({ file: await new Promise((ok, fail) => entry.file(ok, fail)), dir });
  } else if (entry.isDirectory) {
    const sub = dir ? `${dir}/${entry.name}` : entry.name;
    const reader = entry.createReader();
    let batch;
    do {
      batch = await new Promise((ok, fail) => reader.readEntries(ok, fail));
      for (const child of batch) await walkEntry(child, sub, out);
    } while (batch.length);
  }
}

async function upload(company, here, items) {
  if (!items.length) return;
  const replace = $("#replace").checked;
  const panel = $("#upload-panel");
  const tally = { stored: 0, replaced: 0, exists: 0, skipped: 0, failed: 0 };
  const notes = [];
  const hidden = (item) => item.file.name.startsWith(".") || item.dir.split("/").some((p) => p.startsWith("."));
  const wanted = (item) => state.accepted.includes(`.${item.file.name.split(".").pop().toLowerCase()}`);
  const queue = [];
  for (const item of items) {
    const where = [item.dir, item.file.name].filter(Boolean).join("/");
    if (hidden(item)) { tally.skipped++; continue; }
    if (!wanted(item)) { tally.skipped++; notes.push(`${where}: not a type the pipeline reads`); continue; }
    queue.push(item);
  }
  let done = 0;
  panel.hidden = false;
  const draw = (finished) => {
    const pct = queue.length ? Math.round((done / queue.length) * 100) : 100;
    panel.innerHTML = `<div class="panel">
      <div class="panel-row"><b>${finished ? "Upload finished" : `Uploading ${done} of ${queue.length}…`}</b>
        <span class="muted">${tally.stored} new · ${tally.replaced} replaced · ${tally.exists} already there · ${tally.skipped} skipped · ${tally.failed} failed</span></div>
      <div class="progress"><i style="width:${pct}%"></i></div>
      ${finished && notes.length ? `<ul class="summary-list">${notes.slice(0, 200).map((n) => `<li>${esc(n)}</li>`).join("")}${notes.length > 200 ? `<li>…and ${notes.length - 200} more</li>` : ""}</ul>` : ""}
      ${finished ? '<div><button class="btn ghost" id="close-upload">Close</button></div>' : ""}</div>`;
    if (finished) { state.lastUpload = { company, html: panel.innerHTML }; showUploadSummary(panel.innerHTML); }
  };
  draw(false);

  const next = async () => {
    while (queue.length > done + inFlight.size) {
      const item = queue[done + inFlight.size];
      const form = new FormData();
      form.append("file", item.file, item.file.name);
      form.append("folder", [here, item.dir].filter(Boolean).join("/"));
      form.append("replace", replace ? "true" : "false");
      const where = [item.dir, item.file.name].filter(Boolean).join("/");
      const job = api(`companies/${enc(company)}/files`, { method: "POST", form })
        .then((r) => { tally[r.status] = (tally[r.status] || 0) + 1; if (r.status === "exists") notes.push(`${r.path}: already there (tick "Replace" to overwrite)`); if (r.status === "skipped") notes.push(`${r.path}: ${r.reason}`); })
        .catch((err) => { tally.failed++; notes.push(`${where}: ${err.message}`); })
        .finally(() => { inFlight.delete(job); done++; draw(false); });
      inFlight.add(job);
      if (inFlight.size >= 3) await Promise.race(inFlight);
    }
    await Promise.all(inFlight);
  };
  const inFlight = new Set();
  await next();
  draw(true);
  if (tally.stored + tally.replaced) {
    await startIngest(company, true);
  } else {
    toast("Nothing new was uploaded.");
  }
  route(); // show the new files now; their states update as ingestion finishes
}

async function startIngest(company, afterUpload = false) {
  try {
    await api(`companies/${enc(company)}/ingest`, { method: "POST" });
    toast(afterUpload ? "Uploaded. Ingestion started." : "Ingestion started.");
    renderJobs(company);
  } catch (err) { toast(err.message, true); }
}

async function renderJobs(company) {
  const section = $("#jobs-section");
  if (!section) return;
  let data;
  try { data = await api(`companies/${enc(company)}/jobs`); } catch { return; }
  const jobs = data.jobs.slice(0, 6);
  const active = jobs.some((j) => j.status === "queued" || j.status === "running");
  const badge = { queued: "warn", running: "warn", done: "ok", failed: "bad", cancelled: "off" };
  section.innerHTML = jobs.length ? `<h3>Ingestion jobs</h3><div class="card"><table>
    <thead><tr><th>Started</th><th>Status</th><th>Progress</th><th class="hide-sm">Note</th><th></th></tr></thead>
    <tbody>${jobs.map((j) => `<tr>
      <td class="when">${fmtTime(j.created_at)}</td>
      <td><span class="badge ${badge[j.status] || "off"}">${esc(j.status)}</span></td>
      <td style="min-width:140px"><div class="progress"><i style="width:${Math.round(j.progress * 100)}%"></i></div></td>
      <td class="hide-sm muted">${esc(j.error || j.note || "")}</td>
      <td class="actions">${j.status === "queued" || j.status === "running" ? `<button class="btn ghost" data-cancel="${esc(j.id)}">Cancel</button>` : ""}</td>
    </tr>`).join("")}</tbody></table></div>` : "";
  $$("[data-cancel]", section).forEach((b) => {
    b.onclick = async () => {
      try { await api(`companies/${enc(company)}/jobs/${enc(b.dataset.cancel)}/cancel`, { method: "POST" }); renderJobs(company); }
      catch (err) { toast(err.message, true); }
    };
  });
  stopPolling();
  if (active) {
    section.dataset.active = "1";
    state.timer = setTimeout(() => renderJobs(company), 2500);
  } else if (section.dataset.active) {
    // A job just finished: refresh the listing so the states are current.
    delete section.dataset.active;
    route();
  }
}

// ------------------------------------------------------------------ retrieval tester

async function renderRetrieval(view, [routeCompany]) {
  await loadCompanies();
  const company = currentCompany(routeCompany);
  if (!company) {
    view.innerHTML = `<div class="page-head"><div><h2>Retrieval tester</h2></div></div>
      <div class="card empty">No companies yet. <a href="#/companies">Create one first.</a></div>`;
    return;
  }
  const last = JSON.parse(saved.get("syslab-retrieval") || "{}");
  view.innerHTML = `
    <div class="page-head"><div><h2>Retrieval tester</h2><p>Exactly what the retrieval API returns to database-agent for this company.</p></div>
      <div class="toolbar">${companyPicker(company, "retrieval")}</div></div>
    <form class="card pad form-grid" id="retrieve-form">
      <label class="field wide">Question<textarea name="query" required maxlength="2000" placeholder="Which contracts can be terminated for convenience?">${esc(last.query || "")}</textarea></label>
      <label class="field">Only these documents <span class="hint">Optional. Paths, comma-separated, e.g. contracts/contract_01.pdf</span>
        <input type="text" name="sources" value="${esc(last.sources || "")}"></label>
      <label class="field">Passages<input type="number" name="k" min="1" max="50" value="${esc(last.k || 8)}"></label>
      <div class="wide"><button class="btn primary" type="submit">Search</button></div>
    </form>
    <div id="results" class="section"></div>`;
  wirePicker();

  $("#retrieve-form").onsubmit = async (event) => {
    event.preventDefault();
    const form = new FormData(event.target);
    const sources = String(form.get("sources") || "").split(",").map((s) => s.trim()).filter(Boolean);
    saved.set("syslab-retrieval", JSON.stringify({ query: form.get("query"), sources: form.get("sources"), k: form.get("k") }));
    const results = $("#results");
    results.innerHTML = '<p class="muted">Searching…</p>';
    try {
      const r = await api(`companies/${enc(company)}/retrieve`, {
        method: "POST",
        body: { query: form.get("query"), k: Number(form.get("k")) || 8, ...(sources.length ? { sources } : {}) },
      });
      const unavailable = Object.entries(r.retrievers_unavailable || {});
      results.innerHTML = `
        <div class="meaning">${esc(r.what_this_means)}</div>
        <p class="muted" style="margin:12px 0">
          Retrievers: ${r.retrievers.map((x) => `<span class="chip">${esc(x)}</span>`).join(" ") || "none"}
          ${unavailable.length ? ` · unavailable: ${unavailable.map(([k, v]) => `<span class="chip" title="${esc(v)}">${esc(k)}</span>`).join(" ")}` : ""}
          · ${r.coverage.returned} returned · ${r.coverage.matched} of ${r.coverage.searched} passages contain the words · ${r.tokens_returned} tokens${r.truncated ? " · cut by the token budget" : ""}
        </p>
        <div class="card">${r.passages.length ? r.passages.map((p) => `
          <article class="passage">
            <div class="passage-head">
              <span class="rank">${p.rank}</span>
              <a href="#/corpus/${enc(company)}?path=${enc(p.source.split("/").slice(0, -1).join("/"))}" class="mono">${esc(p.source)}</a>
              <span class="muted mono">${esc(p.chunk_id)}</span>
              ${p.found_by.map((f) => `<span class="chip">${esc(f)}</span>`).join(" ")}
              <span class="muted">${p.tokens} tokens</span>
            </div>
            <div class="passage-text">${esc(p.text)}</div>
            <div><button class="btn ghost" data-expand>Show all</button></div>
          </article>`).join("") : '<div class="empty">No passages came back.</div>'}</div>`;
      $$("[data-expand]", results).forEach((b) => {
        b.onclick = () => { const t = b.parentElement.previousElementSibling; t.classList.toggle("open"); b.textContent = t.classList.contains("open") ? "Show less" : "Show all"; };
      });
    } catch (err) {
      results.innerHTML = `<div class="error">${esc(err.message)}</div>`;
    }
  };
}

// ------------------------------------------------------------------ start

(async () => {
  try {
    const data = await api("me");
    showApp(data.operator);
  } catch (err) {
    if (err.status !== 401) showLogin(err.message);
  }
})();
