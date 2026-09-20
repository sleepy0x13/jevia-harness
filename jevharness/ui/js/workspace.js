// Choosing the folder everything is saved to, and the first-run sheet.
import { $, esc, uid, S, persist, hooks, env, T, api, ask, toast, liveKeys } from "./core.js";
import { openPrefs } from "./prefs.js";

let WS = { path: "", folders: [], is_dir: false, writable: false };
let fresh = "";

function note(kind, text) { $("ws-note").className = "wsnote" + (kind ? " " + kind : ""); $("ws-note").textContent = text; }

function crumbs() {
  const parts = (WS.path || "").split("/").filter(Boolean);
  let acc = "";
  $("ws-crumbs").innerHTML = `<button data-go="/">/</button>` + parts.map((part, i) => {
    acc += "/" + part;
    return `<span class="s">›</span><button data-go="${esc(acc)}" class="${i === parts.length - 1 ? "here" : ""}" title="${esc(acc)}">${esc(part)}</button>`;
  }).join("");
  $("ws-crumbs").querySelectorAll("[data-go]").forEach((b) => (b.onclick = () => { fresh = ""; load(b.dataset.go); }));
  $("ws-crumbs").scrollLeft = 1e6;
}

async function load(path, create) {
  const t = T();
  try { WS = await api("/api/workspace", { path: path || "", create: !!create }); }
  catch (e) { note("bad", String(e.message || e)); return; }
  crumbs();
  $("ws-list").innerHTML = (WS.folders || []).length
    ? WS.folders.map((f) => `<button data-into="${esc(f)}" class="${f === fresh ? "fresh" : ""}">
        <span class="g">▸</span><span>${esc(f)}</span><span class="g">${esc(t.enter)}</span></button>`).join("")
    : `<div class="none">${esc(WS.is_dir ? t.wsEmpty : t.wsMissing)}</div>`;
  $("ws-list").querySelectorAll("[data-into]").forEach((b) => (b.onclick = () => { fresh = ""; load(WS.path.replace(/\/$/, "") + "/" + b.dataset.into); }));
  const ok = WS.is_dir && WS.writable;
  $("ws-use").disabled = !ok;
  if (!fresh) note(ok ? "" : "bad", WS.is_dir ? (WS.writable ? t.wsReady((WS.folders || []).length) : t.wsReadonly) : t.wsMissing);
}

async function makeFolder() {
  const name = ($("ws-name").value || "").trim().replace(/[/\\]/g, "");
  if (!name) { $("ws-name").focus(); return; }
  const parent = WS.path.replace(/\/$/, "");
  await load(parent + "/" + name, true);
  // Step into what was just made: that is almost always the folder wanted.
  fresh = name;
  note("good", T().created(name));
  $("mkbar").hidden = true;
}

export function openWorkspace(path) {
  fresh = ""; $("mkbar").hidden = true;
  $("wssheet").classList.add("on"); $("scrim").classList.add("on");
  load(path);
}
function closeWorkspace() {
  $("wssheet").classList.remove("on");
  if (!document.querySelector(".sheet.on, .prefs.on")) $("scrim").classList.remove("on");
}

/* ---------- first run ---------- */
export function maybeOnboard() {
  if (S.onboarded) return;
  if (liveKeys().length || env.key) {
    S.onboarded = true; persist();
    if (!S.workspace) openWorkspace("");
    return;
  }
  $("onboard").classList.add("on"); $("scrim").classList.add("on");
  setTimeout(() => $("ob-key").focus(), 80);
}
function finishOnboarding() {
  S.onboarded = true; persist();
  $("onboard").classList.remove("on");
  if (!document.querySelector(".sheet.on, .prefs.on")) $("scrim").classList.remove("on");
}

export function wireWorkspace() {
  $("ws-new").onclick = () => { $("mkbar").hidden = false; $("ws-name").value = ""; $("ws-name").focus(); };
  $("ws-make-no").onclick = () => { $("mkbar").hidden = true; };
  $("ws-make-ok").onclick = makeFolder;
  $("ws-name").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); makeFolder(); }
    if (e.key === "Escape") { e.preventDefault(); $("mkbar").hidden = true; }
  });
  $("ws-typed").onclick = async () => {
    const v = await ask(T().typePath, WS.path);
    if (v) load(v, true);
  };
  $("ws-close").onclick = closeWorkspace;
  $("ws-use").onclick = () => {
    S.workspace = WS.path; persist();
    closeWorkspace();
    toast(`${T().wsSet} ${WS.path.split("/").filter(Boolean).pop() || WS.path}`);
    hooks.paint(); hooks.render();
  };

  $("ob-start").onclick = () => {
    const key = $("ob-key").value.trim();
    if (key) {
      const ref = "k" + uid();
      S.keys.push({ ref, vendor: "openrouter", api_key: key, label: "OpenRouter" });
      S.models.forEach((m) => { if (!m.credential_ref || m.credential_ref === "default") m.credential_ref = ref; });
    }
    finishOnboarding();
    hooks.paint(); hooks.render();
    if (!S.workspace) openWorkspace("");
  };
  $("ob-later").onclick = () => { finishOnboarding(); hooks.render(); };
  $("ob-other").onclick = (e) => { e.preventDefault(); finishOnboarding(); openPrefs("keys"); };
  $("ob-key").addEventListener("keydown", (e) => { if (e.key === "Enter") $("ob-start").click(); });
}
