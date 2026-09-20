// The settings window. Every change applies at once and says so.
import {
  $, esc, uid, S, persist, hooks, env, cat, T, api, toast, ask, STANCES,
  vendorOf, liveKeys, jevKey, settingsPayload, dropdown,
} from "./core.js";
import { setPreference } from "./motion.js";

const TABS = ["keys", "models", "skills", "tools", "loops", "memory", "behaviour"];
let tab = "keys";
const TAB_LABEL = { keys: "tabKeys", models: "tabModels", skills: "tabSkills", tools: "tabTools", loops: "tabLoops", memory: "tabMemory", behaviour: "tabBehaviour" };
const NOTE = { keys: "keysNote", models: "modelsNote", skills: "skillsNote", tools: "toolsNote", loops: "loopsNote", memory: "memoryNote", behaviour: "behaviourNote" };

export function openPrefs(which) {
  tab = TABS.includes(which) ? which : tab;
  $("prefs").classList.add("on"); $("scrim").classList.add("on");
  paintPrefs();
}
export function closePrefs() {
  $("prefs").classList.remove("on");
  clearInterval(loopTimer);
  if (!document.querySelector(".sheet.on")) $("scrim").classList.remove("on");
  hooks.paint(); hooks.render();
}

export function paintPrefs() {
  if (!$("prefs").classList.contains("on")) return;
  const t = T();
  $("prefs-title").textContent = t.prefs;
  $("prefs-close").textContent = t.close;
  $("prefs-nav").innerHTML = TABS.map((k, i) => `<button data-tab="${k}" class="${k === tab ? "on" : ""}">
    <span class="ix num">0${i + 1}</span><span>${esc(t[TAB_LABEL[k]])}</span></button>`).join("");
  $("prefs-nav").querySelectorAll("[data-tab]").forEach((b) => (b.onclick = () => { tab = b.dataset.tab; paintPrefs(); }));
  $("pane-title").textContent = t[TAB_LABEL[tab]];
  $("pane-note").textContent = t[NOTE[tab]];
  $("pane-action").innerHTML = "";
  clearInterval(loopTimer);
  ({ keys: paneKeys, models: paneModels, skills: paneSkills, tools: paneTools, loops: paneLoops, memory: paneMemory, behaviour: paneBehaviour })[tab]();
}

const body = () => $("pane-body");
const saved = (msg) => { persist(); toast(msg || T().saved); };

/* ================= keys ================= */
let adding = null;           // vendor id whose form is open

function abbr(v) { return (v.name || v.id).replace(/\(.*\)/, "").trim().split(/\s+/).map((w) => w[0]).join("").slice(0, 2).toUpperCase(); }
const mask = (k) => (k.length > 12 ? k.slice(0, 6) + "…" + k.slice(-4) : "••••");

function paneKeys() {
  const t = T(), keys = liveKeys();
  let h = "";
  if (!jevKey() && !env.key) h += `<div class="banner warn">${esc(t.jevNeedsKey)}</div>`;
  h += `<div class="keys">`;
  if (env.key) h += `<div class="krow"><span class="ab">.e</span><div><div class="nm">OpenRouter <span class="tag">${esc(t.envKey)}</span></div>
      <div class="sub">.env</div></div><span class="st ok">${esc(t.jevHere)} · ${esc(t.chatHere)}</span></div>`;
  h += keys.map((k) => {
    const v = vendorOf(k.vendor);
    const can = [v.serves_jev ? t.jevHere : "", v.chats ? t.chatHere : ""].filter(Boolean).join(" · ");
    return `<div class="krow"><span class="ab">${esc(abbr(v))}</span>
      <div><div class="nm">${esc(k.label || v.name)} ${k.vendor === "custom" ? `<span class="tag">${esc(k.base_url || "")}</span>` : ""}</div>
        <div class="sub num">${esc(mask(k.api_key))} · <span class="st ${k.ok === false ? "bad" : "ok"}">${esc(k.note || can)}</span></div></div>
      <button class="btn ghost sm danger" data-rmkey="${esc(k.ref)}">${esc(t.remove)}</button></div>`;
  }).join("");
  if (!keys.length && !env.key) h += `<div class="empty">${esc(t.noKeys)}</div>`;
  h += `</div><div class="lbl" style="margin-top:28px">${esc(t.addKey)}</div>
    <div class="vgrid">${cat.vendors.map((v) => `<button class="vtile ${adding === v.id ? "on" : ""}" data-vendor="${esc(v.id)}">
      <span class="n">${esc(v.name)}</span><span class="d">${esc([v.serves_jev ? "Jev" : "", v.chats ? t.chatHere : ""].filter(Boolean).join(" + "))}</span></button>`).join("")}</div>`;
  if (adding) {
    const v = vendorOf(adding);
    h += `<div class="keyform">
      <div class="row">
        <label class="field"><span class="lbl">${esc(v.name)}</span><input class="input" id="kf-key" type="password" autocomplete="off" placeholder="${esc(v.key_hint ? v.key_hint + "…" : t.keyPlaceholder)}"></label>
        <label class="field"><span class="lbl">${esc(t.label)}</span><input class="input" id="kf-label" value="${esc(v.name.replace(/\s*\(.*\)/, ""))}"></label>
      </div>
      ${v.id === "custom" ? `<label class="field"><span class="lbl">${esc(t.baseUrl)}</span><input class="input" id="kf-base" placeholder="https://…/v1"></label>` : ""}
      <div style="display:flex;gap:10px;align-items:center">
        <button class="btn fill" id="kf-add">${esc(t.addAndTest)}</button>
        ${v.keys_url ? `<a href="${esc(v.keys_url)}" target="_blank" rel="noopener noreferrer">${esc(t.getKey)} ↗</a>` : ""}
        <span class="sp"></span><span class="muted" id="kf-msg" style="font-size:12.5px"></span></div></div>`;
  }
  body().innerHTML = h;
  body().querySelectorAll("[data-vendor]").forEach((b) => (b.onclick = () => {
    adding = adding === b.dataset.vendor ? null : b.dataset.vendor; paneKeys();
    const k = $("kf-key"); if (k) k.focus();
  }));
  body().querySelectorAll("[data-rmkey]").forEach((b) => (b.onclick = () => {
    S.keys = S.keys.filter((k) => k.ref !== b.dataset.rmkey);
    saved(t.keyRemoved); paneKeys();
  }));
  if (adding) {
    $("kf-add").onclick = addKey;
    $("kf-key").addEventListener("keydown", (e) => { if (e.key === "Enter") addKey(); });
  }
}

async function addKey() {
  const t = T(), v = vendorOf(adding), api_key = $("kf-key").value.trim();
  if (!api_key) { $("kf-key").focus(); return; }
  const key = { ref: "k" + uid(), vendor: v.id, api_key, label: $("kf-label").value.trim() || v.name,
    base_url: $("kf-base") ? $("kf-base").value.trim() : "" };
  // A plain-http endpoint that is not on this machine sends the key in the
  // clear. Local model servers are a real case, so this warns rather than refuses.
  if (/^http:\/\//i.test(key.base_url) && !/^http:\/\/(localhost|127\.0\.0\.1|\[::1\])(:|\/|$)/i.test(key.base_url)) {
    toast(t.plainHttpKey, true);
  }
  S.keys.push(key);
  // The starter models live on OpenRouter; if nothing else could run them,
  // point them at the first OpenRouter key.
  if (v.id === "openrouter") S.models.forEach((m) => { if (!liveKeys().some((k) => k.ref === m.credential_ref)) m.credential_ref = key.ref; });
  persist();
  if (!v.chats) { key.note = t.jevHere; saved(t.keyAdded); adding = null; paneKeys(); return; }
  $("kf-msg").textContent = t.testing; $("kf-add").disabled = true;
  try {
    const d = await api("/api/models", { settings: settingsPayload(), ref: key.ref });
    CATALOG[key.ref] = d.models || [];
    key.ok = true; key.note = t.keyOk((d.models || []).length);
    saved(t.keyAdded); adding = null;
  } catch (e) {
    key.ok = false; key.note = String(e.message || e).slice(0, 120);
    persist(); toast(key.note, true);
  }
  paneKeys();
}

/* ================= models ================= */
const CATALOG = {};            // ref -> models that key can run
let browseRef = "", filter = "all", query = "";
const blended = (m) => (Number(m.price_in) || 0) + 3 * (Number(m.price_out) || 0);
const priceLabel = (m, t) => (m.priced === false || m.price_in == null
  ? `<span class="muted">${esc(t.noPrice)}</span>`
  : !m.price_in && !m.price_out ? `<span class="free">${esc(t.free)}</span>` : `$${m.price_in} / $${m.price_out}`);

// Rank by price into tiers 2-4. The floor is 2: any current model can draft a
// reply, and starting at 1 exiles a cheap-but-capable model from everyday work.
function autoTier() {
  const order = S.models.filter((m) => m.model && m.priced !== false).sort((a, b) => blended(a) - blended(b));
  const n = order.length || 1;
  order.forEach((m, i) => { if (!m.capLocked) m.capability = Math.min(4, Math.max(2, 2 + Math.floor((i * 3) / n))); });
}

function chatKeys() {
  const keys = liveKeys().filter((k) => vendorOf(k.vendor).chats);
  if (env.key) keys.push({ ref: "default", vendor: "openrouter", label: "OpenRouter (.env)" });
  return keys;
}

function paneModels() {
  const t = T();
  autoTier();
  const keys = chatKeys();
  const keyName = (ref) => { const k = keys.find((x) => x.ref === ref); return k ? k.label || vendorOf(k.vendor).name : ""; };
  let h = `<div class="lbl">${esc(t.yourModels)}</div><div class="mcards">`;
  h += S.models.length ? S.models.map((m, i) => `<div class="mcard">
      <div style="min-width:0"><div class="nm">${esc(m.label || m.model)}</div>
        <div class="sub">${esc(m.model)}${keyName(m.credential_ref) ? " · " + esc(keyName(m.credential_ref)) : ""} · ${esc(t.tierWhy[m.capability] || "")}</div></div>
      <span class="pr num">${priceLabel(m, t)}</span>
      <span style="display:flex;gap:10px;align-items:center"><span class="tiers">${[1, 2, 3, 4].map((n) =>
        `<button data-tier="${i}:${n}" class="${m.capability === n ? "on" : ""}" title="${esc(t.tierWhy[n])}">${n}</button>`).join("")}</span>
        <button class="btn ghost sm danger" data-rm="${i}">×</button></span></div>`).join("")
    : `<div class="empty">${esc(t.noModels)}</div>`;
  h += `</div><div class="browser"><div class="lbl" style="margin-top:28px">${esc(t.addModels)}</div>`;
  if (!keys.length) h += `<div class="banner warn" style="margin-top:10px">${esc(t.needChatKey)}</div>`;
  else {
    if (!keys.some((k) => k.ref === browseRef)) browseRef = keys[0].ref;
    h += `<div class="filters">
        <button id="mb-key" style="min-width:220px"></button>
        <input class="input" id="mb-q" style="flex:1;min-width:160px" placeholder="${esc(t.search)}" value="${esc(query)}">
        <span class="seg" id="mb-f">${[["all", t.tabAll], ["free", t.tabFree], ["cheap", t.tabCheap], ["added", t.tabAdded]].map(([k, l]) =>
          `<button data-f="${k}" class="${filter === k ? "on" : ""}">${esc(l)}</button>`).join("")}</span></div>
      <div class="mlist" id="mb-list"><div class="empty">${esc(t.loading)}</div></div>`;
  }
  h += `</div>`;
  body().innerHTML = h;
  body().querySelectorAll("[data-tier]").forEach((b) => (b.onclick = () => {
    const [i, n] = b.dataset.tier.split(":").map(Number);
    S.models[i].capability = n; S.models[i].capLocked = true; saved(); paneModels();
  }));
  body().querySelectorAll("[data-rm]").forEach((b) => (b.onclick = () => { S.models.splice(+b.dataset.rm, 1); saved(t.modelRemoved); paneModels(); }));
  if (!keys.length) return;
  dropdown($("mb-key"), keys.map((k) => ({ value: k.ref, label: `${t.browseWith} · ${k.label || vendorOf(k.vendor).name}` })),
    browseRef, (ref) => { browseRef = ref; paneModels(); });
  $("mb-q").oninput = (e) => { query = e.target.value; drawList(); };
  $("mb-f").querySelectorAll("[data-f]").forEach((b) => (b.onclick = () => {
    filter = b.dataset.f; $("mb-f").querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b)); drawList();
  }));
  listModels();
}

async function listModels() {
  const ref = browseRef;
  if (!CATALOG[ref]) {
    try { CATALOG[ref] = (await api("/api/models", { settings: settingsPayload(), ref })).models || []; }
    catch (e) { if ($("mb-list")) $("mb-list").innerHTML = `<div class="banner warn">${esc(String(e.message || e))}</div>`; return; }
  }
  // Prices move; refresh the ones already in use from what the vendor says now.
  S.models.forEach((m) => {
    if (m.credential_ref !== ref) return;
    const hit = CATALOG[ref].find((c) => c.model === m.model);
    if (hit) Object.assign(m, { label: hit.label || m.label, price_in: hit.price_in, price_out: hit.price_out, priced: hit.priced !== false });
  });
  persist();
  drawList();
}

function drawList() {
  const t = T(), list = $("mb-list");
  if (!list) return;
  const chosen = new Set(S.models.filter((m) => m.credential_ref === browseRef).map((m) => m.model));
  let rows = (CATALOG[browseRef] || []).slice();
  if (filter === "free") rows = rows.filter((m) => m.priced !== false && !m.price_in && !m.price_out);
  if (filter === "cheap") rows = rows.filter((m) => m.priced !== false && blended(m) < 1);
  if (filter === "added") rows = rows.filter((m) => chosen.has(m.model));
  const q = query.trim().toLowerCase();
  if (q) rows = rows.filter((m) => (m.model + " " + (m.label || "")).toLowerCase().includes(q));
  rows.sort((a, b) => (a.priced === false) - (b.priced === false) || blended(a) - blended(b));
  list.innerHTML = rows.length ? rows.slice(0, 200).map((m) => `<div class="mitem">
      <span class="nm">${esc(m.label || m.model)}<small>${esc(m.model)}</small></span>
      <span class="pr num">${priceLabel(m, t)}</span>
      <button class="btn sm ${chosen.has(m.model) ? "fill" : ""}" data-pick="${esc(m.model)}">${esc(chosen.has(m.model) ? t.added : t.add)}</button></div>`).join("")
    : `<div class="empty">—</div>`;
  list.querySelectorAll("[data-pick]").forEach((b) => (b.onclick = () => toggleModel(b.dataset.pick)));
}

function toggleModel(slug) {
  const t = T();
  const at = S.models.findIndex((m) => m.model === slug && m.credential_ref === browseRef);
  if (at >= 0) { S.models.splice(at, 1); saved(t.modelRemoved); }
  else {
    const hit = (CATALOG[browseRef] || []).find((m) => m.model === slug) || { model: slug, label: slug };
    S.models.push({ id: "m" + uid(), model: hit.model, label: hit.label || hit.model, capability: 2,
      price_in: hit.price_in || 0, price_out: hit.price_out || 0, priced: hit.priced !== false, credential_ref: browseRef });
    saved(t.modelAdded);
  }
  const scroll = $("mb-list").scrollTop;
  paneModels();
  requestAnimationFrame(() => { const l = $("mb-list"); if (l) l.scrollTop = scroll; });
}

/* ================= skills ================= */
let editing = null;          // {id, text, editable} or {id:"", text:template}
const TEMPLATE = "---\nname: \ndescription: \nwhen: \n---\n\n";

async function skillsCall(action, extra) {
  const d = await api("/api/skills", { settings: settingsPayload(), action, ...(extra || {}) });
  if (d.skills) cat.skills = d.skills;
  return d;
}

// A skill file the user picked: one .md, or a .zip holding several. The file
// is sent as bytes and read on the server; nothing in it is executed.
async function uploadSkills(files) {
  const t = T();
  for (const file of files) {
    try {
      const bytes = new Uint8Array(await file.arrayBuffer());
      let binary = "";
      for (let i = 0; i < bytes.length; i += 8192) binary += String.fromCharCode(...bytes.subarray(i, i + 8192));
      const res = await api("/api/skills", { settings: settingsPayload(), action: "upload",
        name: file.name, data: btoa(binary) });
      cat.skills = res.skills || cat.skills;
      toast(t.skillsAdded((res.installed || []).length, (res.ignored || []).length));
    } catch (err) { toast(`${file.name}: ${err.message || err}`, true); }
  }
  paneSkills(); hooks.paint();
}

function paneSkills() {
  const t = T();
  if (editing) return paneSkillEditor();
  $("pane-action").innerHTML = `<button class="btn sm" id="sk-upload">${esc(t.uploadSkill)}</button>
    <button class="btn fill sm" id="sk-new">${esc(t.newSkill)}</button>
    <input type="file" id="sk-file" accept=".md,.markdown,.zip,text/markdown,application/zip" hidden multiple>`;
  $("sk-new").onclick = () => { editing = { id: "", text: TEMPLATE, editable: true }; paneSkills(); };
  $("sk-upload").onclick = () => { if (!S.workspace) { toast(t.skillsNeedWs, true); return; } $("sk-file").click(); };
  $("sk-file").onchange = async (e) => { await uploadSkills([...e.target.files]); e.target.value = ""; };
  let h = S.workspace ? "" : `<div class="banner warn">${esc(t.skillsNeedWs)}</div>`;
  h += `<div class="list">${cat.skills.map((s) => `<div class="li" data-skill="${esc(s.id)}">
      <div><div class="nm">${esc(s.name)} <span class="tag ${s.editable ? "blue" : ""}">${esc(s.editable ? t.yours : t.builtIn)}</span></div>
        <div class="sub">${esc(s.summary || s.description)}</div></div><span class="muted">→</span></div>`).join("")}</div>
    <div class="keyform"><label class="field" style="margin:0"><span class="lbl">${esc(t.installFrom)}</span>
      <div style="display:flex;gap:8px"><input class="input" id="sk-url" placeholder="https://github.com/…/SKILL.md">
      <button class="btn fill" id="sk-install" ${S.workspace ? "" : "disabled"}>${esc(t.install)}</button></div></label></div>`;
  body().innerHTML = h;
  body().querySelectorAll("[data-skill]").forEach((el) => (el.onclick = async () => {
    try {
      const d = await skillsCall("get", { id: el.dataset.skill });
      editing = { id: el.dataset.skill, text: d.text, editable: !!(d.skill && d.skill.editable) };
      paneSkills();
    } catch (e) { toast(String(e.message || e), true); }
  }));
  $("sk-install").onclick = async () => {
    const url = $("sk-url").value.trim();
    if (!url) return $("sk-url").focus();
    $("sk-install").disabled = true;
    try { await skillsCall("install", { url }); toast(t.skillInstalled); paneSkills(); hooks.paint(); }
    catch (e) { toast(String(e.message || e), true); $("sk-install").disabled = false; }
  };
}

function paneSkillEditor() {
  const t = T(), e = editing;
  $("pane-action").innerHTML = `<button class="btn ghost sm" id="sk-back">← ${esc(t.back)}</button>`;
  $("sk-back").onclick = () => { editing = null; paneSkills(); };
  body().innerHTML = `${S.workspace ? "" : `<div class="banner warn">${esc(t.skillsNeedWs)}</div>`}
    <div class="editor"><textarea class="input" id="sk-text" spellcheck="false">${esc(e.text)}</textarea></div>
    <div style="display:flex;gap:8px;margin-top:12px">
      <button class="btn fill" id="sk-save" ${S.workspace ? "" : "disabled"}>${esc(t.save)}</button>
      ${e.editable && e.id ? `<button class="btn danger" id="sk-del">${esc(t.del)}</button>` : ""}</div>`;
  $("sk-save").onclick = async () => {
    try {
      const d = await skillsCall("save", { id: e.id || undefined, text: $("sk-text").value });
      editing = { id: d.skill.id, text: $("sk-text").value, editable: true };
      toast(t.skillSaved); hooks.paint();
    } catch (err) { toast(String(err.message || err), true); }
  };
  if ($("sk-del")) $("sk-del").onclick = async () => {
    if (!(await ask(t.del + "?", "", { confirm: true, danger: true }))) return;
    try { await skillsCall("delete", { id: e.id }); editing = null; toast(t.skillDeleted); paneSkills(); hooks.paint(); }
    catch (err) { toast(String(err.message || err), true); }
  };
}

/* ================= loops ================= */
let JOBS = [], loopTimer = 0;
const EVERY = [5, 15, 30, 60, 180, 720, 1440];

async function jobs(action, extra) {
  if (!S.workspace) return;
  try { JOBS = (await api("/api/schedule", { settings: settingsPayload(), action, ...(extra || {}) })).jobs || []; }
  catch (e) { toast(String(e.message || e), true); }
}

function paneLoops() {
  const t = T();
  if (!S.workspace) { body().innerHTML = `<div class="banner warn">${esc(t.loopsNeedWs)}</div>`; return; }
  body().innerHTML = `<div class="newjob">
      <label class="field"><span class="lbl">${esc(t.loopPrompt)}</span><textarea class="input" id="lp-prompt" rows="2"></textarea></label>
      <label class="field"><span class="lbl">${esc(t.loopGoal)}</span><input class="input" id="lp-goal" placeholder="${esc(t.loopGoalHint)}"></label>
      <div class="row">
        <label class="field"><span class="lbl">${esc(t.loopName)}</span><input class="input" id="lp-name"></label>
        <label class="field"><span class="lbl">${esc(t.loopEvery)}</span><button id="lp-every"></button></label>
        <label class="field"><span class="lbl">${esc(t.loopMax)}</span><input class="input num" id="lp-max" type="number" min="1" max="200" value="10"></label>
      </div>
      <div style="display:flex;align-items:center;gap:14px">
        <label class="check" style="border:0;padding:0"><input type="checkbox" id="lp-now" checked><span><b>${esc(t.startNow)}</b></span></label>
        <span class="sp" style="flex:1"></span><button class="btn fill" id="lp-add">${esc(t.createLoop)}</button></div>
      <p class="note" style="margin:12px 0 0">${esc(t.loopKeyWarn)}</p></div>
    <div id="lp-list"><div class="empty">${esc(t.loading)}</div></div>`;
  let every = 60;
  const pickEvery = () => dropdown($("lp-every"), EVERY.map((m) => ({ value: m, label: t.everyN(m) })), every,
    (m) => { every = m; pickEvery(); });
  pickEvery();
  $("lp-add").onclick = async () => {
    const prompt = $("lp-prompt").value.trim();
    if (!prompt) return $("lp-prompt").focus();
    await jobs("add", { prompt, name: $("lp-name").value.trim() || prompt.replace(/\s+/g, " ").slice(0, 80),
      every_minutes: every, until: $("lp-goal").value.trim(),
      max_runs: +$("lp-max").value || 0, start_now: $("lp-now").checked });
    $("lp-prompt").value = ""; $("lp-goal").value = ""; $("lp-name").value = "";
    toast(t.loopCreated); drawJobs();
  };
  jobs("list").then(drawJobs);
  // A round can take a minute; keep the list honest while the window is open.
  loopTimer = setInterval(() => { if (JOBS.some((j) => j.running)) jobs("list").then(drawJobs); }, 4000);
}

function drawJobs() {
  const t = T(), list = $("lp-list");
  if (!list) return;
  list.innerHTML = JOBS.length ? JOBS.map((j) => {
    const hist = (j.history || []).slice(-6).reverse();
    return `<div class="job"><div class="jt"><div>
        <div class="nm">${j.running ? '<span class="live"></span>' : ""}${esc(j.name)}
          <span class="tag ${j.status === "active" ? "ink" : j.status === "failed" ? "red" : ""}">${esc(t.status[j.status] || j.status)}</span>
          ${j.is_loop ? `<span class="tag blue">${esc(t.loopTag)}</span>` : ""}</div>
        <div class="sub">${esc(t.everyN(j.every_minutes))} · ${esc(t.lastRun)} ${j.last_run ? new Date(j.last_run * 1000).toLocaleString() : esc(t.never)}
          ${j.max_runs ? ` · ${j.runs}/${j.max_runs} ${esc(t.rounds)}` : ""}</div>
        ${j.until ? `<div class="goal"><b>${esc(t.goal)}</b> ${esc(j.until)}</div>` : ""}
        ${j.last_error ? `<div class="goal" style="color:var(--red)">${esc(j.last_error)}</div>` : ""}</div>
      <div class="ja"><button class="btn sm" data-jrun="${j.id}" ${j.running ? "disabled" : ""}>${esc(t.runNow)}</button>
        <button class="btn sm" data-jtog="${j.id}">${esc(j.enabled ? t.pause : t.resume)}</button>
        <button class="btn sm danger" data-jdel="${j.id}">×</button></div></div>
      ${hist.length ? `<div class="hist">${hist.map((r, i) => `<div class="hr"><span class="rn num">${String(j.runs - i).padStart(2, "0")}</span>
          <span class="gm ${r.goal_met >= 0.75 ? "met" : ""} num">${r.goal_met != null ? Math.round(r.goal_met * 100) + "%" : r.ok === false ? "✕" : "✓"}</span>
          <span>${esc((r.preview || r.error || "").slice(0, 160))}${r.stopped
            ? ` <span class="tag ${r.stopped === "goal met" ? "acid" : ""}">${esc(r.stopped === "goal met" ? t.dp.goalMet : t.dp.limitReached)}</span>` : ""}
            ${(r.conditions || []).map((c) => `<span class="cond ${c.met === true ? "ok" : c.met === false ? "no" : ""}">
              ${esc(c.text)} · ${esc(c.checker === "code" ? t.dp.checkedByCode : t.dp.checkedByJev)} ·
              ${c.checker === "code" ? (c.measured != null ? `<span class="num">${c.measured}/${c.limit}</span>` : esc(c.met ? "✓" : "✕"))
                : c.probability != null ? `<span class="num">${esc(t.dp.pYes)} ${(+c.probability).toFixed(2)}</span>` : esc(t.dp.unavailable)}</span>`).join("")}</span></div>`).join("")}</div>` : ""}
    </div>`;
  }).join("") : `<div class="empty">${esc(t.noLoops)}</div>`;
  list.querySelectorAll("[data-jrun]").forEach((b) => (b.onclick = async () => { await jobs("run", { id: b.dataset.jrun }); setTimeout(() => jobs("list").then(drawJobs), 400); }));
  list.querySelectorAll("[data-jtog]").forEach((b) => (b.onclick = async () => { await jobs("toggle", { id: b.dataset.jtog }); drawJobs(); }));
  list.querySelectorAll("[data-jdel]").forEach((b) => (b.onclick = async () => {
    if (!(await ask(t.del + "?", "", { confirm: true, danger: true }))) return;
    await jobs("remove", { id: b.dataset.jdel }); drawJobs();
  }));
}

/* ================= memory ================= */
let FACTS = [];
async function memory(action, extra) {
  if (!S.workspace) return;
  try { FACTS = (await api("/api/memory", { settings: settingsPayload(), action, ...(extra || {}) })).facts || []; }
  catch (e) { toast(String(e.message || e), true); }
}
function paneMemory() {
  const t = T();
  const toggle = `<label class="check"><input type="checkbox" id="mem-on" ${S.memory_enabled !== false ? "checked" : ""}><span><b>${esc(t.memoryOn)}</b></span></label>`;
  if (!S.workspace) { body().innerHTML = toggle + `<div class="banner warn">${esc(t.memoryNeedsWs)}</div>`; wireMemToggle(); return; }
  $("pane-action").innerHTML = `<button class="btn sm danger" id="mem-clear">${esc(t.forgetAll)}</button>`;
  $("mem-clear").onclick = async () => {
    if (!(await ask(t.forgetAll + "?", "", { confirm: true, danger: true }))) return;
    await memory("clear"); drawFacts(); toast(t.saved);
  };
  body().innerHTML = `${toggle}
    <div style="display:flex;gap:8px;margin:14px 0 18px"><input class="input" id="mem-add" placeholder="${esc(t.rememberThis)}">
      <button class="btn fill" id="mem-add-go">${esc(t.add)}</button></div>
    <div class="list" id="mem-list"></div>`;
  wireMemToggle();
  const add = async () => { const v = $("mem-add").value.trim(); if (!v) return; await memory("add", { text: v }); $("mem-add").value = ""; drawFacts(); toast(t.saved); };
  $("mem-add-go").onclick = add;
  $("mem-add").addEventListener("keydown", (e) => { if (e.key === "Enter") add(); });
  memory("list").then(drawFacts);
}
function wireMemToggle() { $("mem-on").onchange = (e) => { S.memory_enabled = e.target.checked; saved(); }; }
function drawFacts() {
  const t = T(), list = $("mem-list");
  if (!list) return;
  list.innerHTML = FACTS.length ? FACTS.map((f) => `<div class="fact"><div>${esc(f.text)}
      <small>${esc(f.source === "typed" ? t.typed : t.stated)} · ${new Date(f.created * 1000).toLocaleDateString()}</small></div>
      <button class="btn ghost sm danger" data-forget="${esc(f.text)}">${esc(t.forget)}</button></div>`).join("")
    : `<div class="empty">${esc(t.noFacts)}</div>`;
  list.querySelectorAll("[data-forget]").forEach((b) => (b.onclick = async () => { await memory("forget", { text: b.dataset.forget }); drawFacts(); }));
}

/* ================= tools ================= */
let SERVERS = [];

async function mcp(action, extra) {
  const d = await api("/api/mcp", { settings: settingsPayload(), action, ...(extra || {}) });
  SERVERS = d.servers || [];
  return d;
}

function paneTools() {
  const t = T(), L = t.dp;
  const opt = (key, title, desc, on) => `<label class="check"><input type="checkbox" data-opt="${key}" ${on ? "checked" : ""}>
    <span><b>${esc(title)}</b><span>${esc(desc)}</span></span></label>`;
  body().innerHTML = `
    <label class="field"><span class="lbl">${esc(L.qualityTitle)}</span><button id="quality-pick"></button>
      <span class="note">${esc(L.qualityNote)}</span></label>
    <label class="field"><span class="lbl">${esc(L.toolsTitle)}</span><button id="tools-pick2"></button>
      <span class="note">${esc(L.toolsNote)}</span></label>
    ${opt("plan_mode", L.planModePref, L.planModeNote, !!S.plan_mode)}
    ${opt("approve_changes", L.approvePref, L.approveNote, !!S.approve_changes)}
    <div class="lbl" style="margin-top:26px">${esc(L.mcpTitle)}</div>
    <p class="note" style="margin:4px 0 12px">${esc(L.mcpNote)}</p>
    ${S.workspace ? `<div class="list" id="mcp-list"></div>
    <div class="keyform"><div class="row">
      <label class="field"><span class="lbl">${esc(L.mcpName)}</span><input class="input" id="mcp-name" placeholder="filesystem"></label>
      <label class="field"><span class="lbl">${esc(L.mcpCommand)}</span><input class="input" id="mcp-cmd" placeholder="npx -y @modelcontextprotocol/server-filesystem ."></label>
    </div>
    <div style="display:flex;gap:10px;align-items:center;margin-top:10px">
      <button class="btn fill" id="mcp-add">${esc(L.mcpAdd)}</button>
      <span class="note">${esc(L.mcpWarning)}</span></div></div>`
    : `<div class="banner warn">${esc(t.skillsNeedWs)}</div>`}`;
  body().querySelectorAll("[data-opt]").forEach((el) => (el.onchange = () => { S[el.dataset.opt] = el.checked; saved(); }));
  dropdown($("quality-pick"), ["thrift", "best"].map((v) => ({
    value: v, label: L.qualities[v], desc: L.qualityNotes[v] })),
    S.quality || "thrift", (v) => { S.quality = v; saved(); paneTools(); });
  dropdown($("tools-pick2"), ["read", "write", "full"].map((v) => ({
    value: v, label: L.toolSets[v], desc: L.toolSetNotes[v] })),
    S.tools || "write", (v) => { S.tools = v; saved(); paneTools(); });
  if (!S.workspace) return;
  $("mcp-add").onclick = async () => {
    const name = $("mcp-name").value.trim(), command = $("mcp-cmd").value.trim();
    if (!name || !command) return $("mcp-name").focus();
    try {
      await mcp("add", { server: { name, command: command.split(/\s+/) } });
      $("mcp-name").value = ""; $("mcp-cmd").value = "";
      drawServers(); toast(t.saved);
    } catch (e) { toast(String(e.message || e), true); }
  };
  mcp("list").then(drawServers).catch(() => drawServers());
}

function drawServers() {
  const t = T(), L = t.dp, list = $("mcp-list");
  if (!list) return;
  list.innerHTML = SERVERS.length ? SERVERS.map((s) => `<div class="li">
      <div><div class="nm">${esc(s.name)}
        <span class="tag ${s.status === "ready" ? "blue" : s.error ? "red" : ""}">${esc(s.error ? L.mcpFailed : L.mcpReady(s.tools.length))}</span>
        ${s.enabled ? "" : `<span class="tag">${esc(L.mcpOff)}</span>`}</div>
        <div class="sub num">${esc(s.command.join(" "))}</div>
        ${s.error ? `<div class="sub" style="color:var(--red)">${esc(s.error)}</div>`
          : s.tools.length ? `<div class="sub">${esc(s.tools.slice(0, 8).join(", "))}</div>` : ""}</div>
      <span style="display:flex;gap:8px">
        <button class="btn ghost sm" data-mcptog="${esc(s.name)}">${esc(s.enabled ? L.mcpDisable : L.mcpEnable)}</button>
        <button class="btn ghost sm danger" data-mcpdel="${esc(s.name)}">×</button></span></div>`).join("")
    : `<div class="empty">${esc(L.mcpEmpty)}</div>`;
  list.querySelectorAll("[data-mcptog]").forEach((b) => (b.onclick = async () => {
    await mcp("toggle", { name: b.dataset.mcptog }); drawServers();
  }));
  list.querySelectorAll("[data-mcpdel]").forEach((b) => (b.onclick = async () => {
    if (!(await ask(T().del + "?", "", { confirm: true, danger: true }))) return;
    await mcp("remove", { name: b.dataset.mcpdel }); drawServers();
  }));
}

/* ================= behaviour ================= */
function paneBehaviour() {
  const t = T();
  const opt = (key, title, desc, on) => `<label class="check"><input type="checkbox" data-opt="${key}" ${on ? "checked" : ""}>
    <span><b>${esc(title)}</b><span>${esc(desc)}</span></span></label>`;
  body().innerHTML = `<div class="stances">${t.stance.map((o) => `<label class="${S.stance === o.id ? "on" : ""}">
      <input type="radio" name="stance" value="${o.id}" ${S.stance === o.id ? "checked" : ""}>
      <span class="t">${esc(o.t)}</span><span class="d">${esc(o.d)}</span></label>`).join("")}</div>
    ${opt("research_enabled", t.optResearch, t.optResearchD, S.research_enabled !== false)}
    ${opt("apps_enabled", t.optApps, t.optApps_D, S.apps_enabled !== false)}
    ${opt("guard_output", t.optGuard, t.optGuardD, !!S.guard_output)}
    ${opt("escalate_uncertain", t.optEscalate, t.optEscalateD, !!S.escalate_uncertain)}
    ${opt("tools_enabled", t.optTools, t.optToolsD, S.tools_enabled !== false)}

    ${opt("allow_downgrade", t.dp.downgradePref, t.dp.downgradeNote, !!S.allow_downgrade)}
    <label class="field" style="margin-top:14px"><span class="lbl">${esc(t.dp.motion)}</span><button id="motion-pick"></button>
      <span class="note">${esc(t.dp.motionNote)}</span></label>
    <div class="field" style="margin-top:14px"><span class="lbl">${esc(t.dp.demoTitle)}</span>
      <button class="btn" id="demo-open">${esc(t.dp.demoPick)}</button>
      <span class="note">${esc(t.dp.demoNote)}</span></div>
    <details class="ledger" style="margin-top:22px"><summary>${esc(t.advanced)}</summary>
      ${opt("agent_loop", t.dp.loopPref, t.dp.loopNote, S.agent_loop !== false)}
      <label class="field"><span class="lbl">${esc(t.dp.maxSteps)}</span>
        <input class="input num" type="number" min="1" max="24" step="1" id="max-steps" value="${S.max_steps || 8}">
        <span class="note">${esc(t.dp.maxStepsNote)}</span></label>
      ${opt("routing_extra", t.dp.extraRouting, t.dp.extraRoutingNote, !!S.routing_extra)}
      <div class="three" style="margin-top:8px">
        <label class="field"><span class="lbl">${esc(t.thAbstain)}</span><input class="input num" type="number" step="0.05" min="0" max="1" data-th="decision_abstain_below" value="${S.thresholds.decision_abstain_below}"></label>
        <label class="field"><span class="lbl">${esc(t.thLow)}</span><input class="input num" type="number" step="0.05" min="0" max="1" data-th="noul_uncertain_low" value="${S.thresholds.noul_uncertain_low}"></label>
        <label class="field"><span class="lbl">${esc(t.thHigh)}</span><input class="input num" type="number" step="0.05" min="0" max="1" data-th="noul_uncertain_high" value="${S.thresholds.noul_uncertain_high}"></label>
      </div>
      <label class="field"><span class="lbl">${esc(t.jevModel)}</span><input class="input" id="jev-model" placeholder="${esc(env.jevModel)}" value="${esc(S.jev_model || "")}"></label>
    </details>`;
  body().querySelectorAll("input[name=stance]").forEach((el) => (el.onchange = () => {
    S.stance = el.value; S.thresholds = { ...STANCES[el.value] }; saved(); paneBehaviour();
  }));
  body().querySelectorAll("[data-opt]").forEach((el) => (el.onchange = () => { S[el.dataset.opt] = el.checked; saved(); }));
  body().querySelectorAll("[data-th]").forEach((el) => (el.onchange = () => {
    const v = parseFloat(el.value);
    if (!Number.isNaN(v)) { S.thresholds[el.dataset.th] = Math.min(1, Math.max(0, v)); S.stance = ""; saved(); }
  }));
  $("jev-model").onchange = (e) => { S.jev_model = e.target.value.trim(); saved(); };
  $("max-steps").onchange = (e) => {
    S.max_steps = Math.max(1, Math.min(24, parseInt(e.target.value, 10) || 8));
    e.target.value = S.max_steps; saved();
  };
  $("demo-open").onclick = () => { closePrefs(); hooks.demo($("btn-settings")); };
  dropdown($("motion-pick"), ["system", "reduced", "off"].map((v) => ({ value: v, label: t.dp.motionModes[v] })),
    S.motion || "system", (v) => { S.motion = v; setPreference(v); saved(); paneBehaviour(); });
}

export function wirePrefs() {
  $("prefs-close").onclick = closePrefs;
  $("btn-settings").onclick = () => openPrefs();
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && $("prefs").classList.contains("on") && !$("ask").classList.contains("on")) closePrefs(); });
}
