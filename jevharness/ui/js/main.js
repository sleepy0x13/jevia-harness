// Boot: wire every module, ask the server what it knows, paint.
import { $, S, T, persist, hooks, env, cat, api, settingsPayload, wireAsk, closeMenu } from "./core.js";
import { render, paintComposer, wireComposer, grow, send } from "./chat.js";
import { renderSidebar, wireSidebar } from "./sidebar.js";
import { wireWorkspace, maybeOnboard, openWorkspace } from "./workspace.js";
import { wirePrefs, paintPrefs, closePrefs } from "./prefs.js";
import { wireCanvas, refreshCanvas } from "./canvas.js";
import { wireMotion, setPreference } from "./motion.js";

function paint() {
  const t = T();
  document.documentElement.lang = S.lang === "zh" ? "zh-CN" : "en";
  document.querySelectorAll("[data-t]").forEach((el) => {
    const v = t[el.dataset.t];
    if (typeof v === "string") el.textContent = v;
  });
  document.querySelectorAll("[data-tp]").forEach((el) => {
    const v = t[el.dataset.tp];
    if (typeof v === "string") el.placeholder = v;
  });
  $("lang").querySelectorAll("button").forEach((b) => b.classList.toggle("on", b.dataset.lang === S.lang));
  const ws = S.workspace;
  // Truncated from the left so the folder name stays visible; the marks keep
  // the slashes where they belong in a right-to-left box.
  $("ws-label").textContent = ws ? `\u200e${ws}\u200e` : t.chooseWs;
  $("ws-dot").classList.toggle("off", !ws);
  paintComposer();
  renderSidebar();
  paintPrefs();
  refreshCanvas();
}

async function loadCatalogue() {
  try {
    const d = await api(`/api/catalogue?lang=${S.lang}`);
    cat.vendors = d.vendors || []; cat.skills = d.skills || []; cat.roles = d.roles || [];
  } catch (e) { /* the server will say what is wrong when a task runs */ }
  // Skills in the workspace join the built-in ones.
  if (S.workspace) {
    try {
      cat.skills = (await api("/api/skills", { settings: settingsPayload(), action: "list" })).skills || cat.skills;
    } catch (e) { /* built-ins only */ }
  }
}

(async function boot() {
  hooks.paint = paint;
  hooks.render = render;
  setPreference(S.motion || "system");
  wireMotion(() => render());
  wireAsk(); wireSidebar(); wireComposer(); wireWorkspace(); wirePrefs(); wireCanvas();
  $("lang").querySelectorAll("button").forEach((b) => (b.onclick = async () => {
    S.lang = b.dataset.lang; persist(); await loadCatalogue(); paint(); render();
  }));
  $("btn-workspace").onclick = () => openWorkspace(S.workspace || "");
  $("scrim").onclick = () => {
    document.querySelectorAll(".sheet.on").forEach((s) => { if (s.id !== "onboard") s.classList.remove("on"); });
    if ($("prefs").classList.contains("on")) closePrefs();
    if (!document.querySelector(".sheet.on, .prefs.on")) $("scrim").classList.remove("on");
  };
  document.addEventListener("click", closeMenu);
  document.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") send();
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") { e.preventDefault(); $("btn-new").click(); }
    if (e.key === "Escape") closeMenu();
  });

  try {
    const h = await api("/api/health");
    env.key = !!h.env_key_present;
    env.jevModel = h.jev_model || env.jevModel;
  } catch (e) { /* offline: the composer will report it */ }
  await loadCatalogue();
  paint(); render(); grow();
  maybeOnboard();
  $("prompt").focus();
})();
