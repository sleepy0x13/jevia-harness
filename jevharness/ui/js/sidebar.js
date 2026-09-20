// The sidebar: tasks, folders, drag to reorder or file, and the resizable edges.
import { $, esc, uid, S, persist, hooks, T, ask, popMenu } from "./core.js";
import { current, setCurrent, isRunning, render, toBottom, fail, newChat } from "./chat.js";
import { closeCanvas } from "./canvas.js";

let dragging = null;

function row(x) {
  const busy = isRunning(x.id);
  return `<div class="thread ${x.id === current ? "on" : ""} ${busy ? "busy" : ""}" draggable="true" data-chat="${x.id}">
    ${busy ? `<span class="live" title="${esc(T().working)}"></span>` : ""}
    <span class="tt">${esc(x.title)}</span><button class="more" data-menu="${x.id}">⋯</button></div>`;
}

export function renderSidebar() {
  const t = T(), live = S.chats.filter((x) => !x.archived), parts = [];
  for (const f of S.folders) {
    const kids = live.filter((x) => x.folder === f.id);
    const busy = kids.some((x) => isRunning(x.id));
    parts.push(`<div class="folder" data-folder="${f.id}">
      <div class="fh" data-fold="${f.id}"><span>${f.open === false ? "+" : "–"}</span><span class="fn">${esc(f.name)}</span>
        ${busy ? '<span class="live"></span>' : ""}<span class="fc num">${kids.length}</span>
        <button class="more" data-fmenu="${f.id}">⋯</button></div>
      ${f.open === false ? "" : `<div class="kids">${kids.map(row).join("")}</div>`}</div>`);
  }
  const loose = live.filter((x) => !x.folder || !S.folders.some((f) => f.id === x.folder));
  if (loose.length) parts.push(`<div class="sh">${esc(t.chats)}</div>` + loose.map(row).join(""));
  const archived = S.chats.filter((x) => x.archived);
  if (archived.length && S.showArchived) parts.push(`<div class="sh">${esc(t.archived)} · ${archived.length}</div>` + archived.map(row).join(""));
  if (!S.chats.length) parts.push(`<div class="sh" style="text-transform:none;letter-spacing:0;font-weight:400">${esc(t.emptySide)}</div>`);
  $("threads").innerHTML = parts.join("");
  $("btn-archived").classList.toggle("on", !!S.showArchived);
  $("btn-archived").textContent = `${t.archived}${archived.length ? " · " + archived.length : ""}`;
  wire();
}

function open(id) {
  setCurrent(id); fail(""); closeCanvas(); render(); hooks.paint();
  // Open an old conversation where it left off, not at its beginning.
  requestAnimationFrame(() => toBottom(true));
  $("side").classList.remove("on");
}

function wire() {
  const box = $("threads");
  box.querySelectorAll("[data-chat]").forEach((el) => {
    el.onclick = (e) => { if (!e.target.closest(".more")) open(el.dataset.chat); };
    el.ondragstart = (e) => {
      dragging = el.dataset.chat; el.classList.add("dragging");
      e.dataTransfer.effectAllowed = "move";
      try { e.dataTransfer.setData("text/plain", dragging); } catch (x) { /* some browsers refuse */ }
    };
    el.ondragend = () => { dragging = null; renderSidebar(); };
    el.ondragover = (e) => {
      if (!dragging || dragging === el.dataset.chat) return;
      e.preventDefault();
      const r = el.getBoundingClientRect(), above = e.clientY < r.top + r.height / 2;
      el.classList.toggle("above", above); el.classList.toggle("below", !above);
    };
    el.ondragleave = () => el.classList.remove("above", "below");
    el.ondrop = (e) => {
      e.preventDefault(); e.stopPropagation();
      const below = el.classList.contains("below");
      el.classList.remove("above", "below");
      move(dragging, el.dataset.chat, below);
    };
  });
  box.querySelectorAll("[data-menu]").forEach((b) => (b.onclick = (e) => { e.stopPropagation(); chatMenu(b.dataset.menu, b); }));
  box.querySelectorAll("[data-fold]").forEach((el) => (el.onclick = (e) => {
    if (e.target.closest(".more")) return;
    const f = S.folders.find((x) => x.id === el.dataset.fold);
    if (f) { f.open = f.open === false; persist(); renderSidebar(); }
  }));
  box.querySelectorAll("[data-fmenu]").forEach((b) => (b.onclick = (e) => { e.stopPropagation(); folderMenu(b.dataset.fmenu, b); }));
  box.querySelectorAll("[data-folder]").forEach((el) => {
    el.ondragover = (e) => { if (!dragging) return; e.preventDefault(); el.classList.add("into"); };
    el.ondragleave = (e) => { if (!el.contains(e.relatedTarget)) el.classList.remove("into"); };
    el.ondrop = (e) => {
      e.preventDefault(); el.classList.remove("into");
      const c = S.chats.find((x) => x.id === dragging);
      if (c) { c.folder = el.dataset.folder; c.archived = false; persist(); renderSidebar(); }
    };
  });
}

function move(id, targetId, below) {
  if (!id || id === targetId) return;
  const from = S.chats.findIndex((x) => x.id === id), target = S.chats.find((x) => x.id === targetId);
  if (from < 0 || !target) return;
  const moved = S.chats.splice(from, 1)[0];
  moved.folder = target.folder || null;
  moved.archived = !!target.archived;
  S.chats.splice(S.chats.findIndex((x) => x.id === targetId) + (below ? 1 : 0), 0, moved);
  persist(); renderSidebar();
}

function chatMenu(id, anchor) {
  const t = T(), c = S.chats.find((x) => x.id === id);
  if (!c) return;
  const rows = [
    { label: t.rename, run: async () => { const v = await ask(t.rename, c.title); if (v) { c.title = v.slice(0, 46); persist(); renderSidebar(); } } },
    { label: c.archived ? t.unarchive : t.archive, run: () => { c.archived = !c.archived; persist(); renderSidebar(); } },
  ];
  if (S.folders.length) {
    rows.push("-");
    if (c.folder) rows.push({ label: t.noFolder, run: () => { c.folder = null; persist(); renderSidebar(); } });
    S.folders.filter((f) => f.id !== c.folder).forEach((f) => rows.push({
      label: `${t.moveTo} ${f.name}`, run: () => { c.folder = f.id; c.archived = false; persist(); renderSidebar(); },
    }));
  }
  rows.push("-", { label: t.del, danger: true, run: async () => {
    if (!(await ask(t.confirmDelete, "", { confirm: true, danger: true }))) return;
    S.chats = S.chats.filter((x) => x.id !== id);
    persist();
    if (current === id) newChat(); else renderSidebar();
  } });
  popMenu(anchor, rows);
}

function folderMenu(id, anchor) {
  const t = T(), f = S.folders.find((x) => x.id === id);
  if (!f) return;
  popMenu(anchor, [
    { label: t.rename, run: async () => { const v = await ask(t.folderName, f.name); if (v) { f.name = v.slice(0, 28); persist(); renderSidebar(); } } },
    { label: t.deleteFolder, danger: true, run: () => {
      S.chats.forEach((c) => { if (c.folder === id) c.folder = null; });
      S.folders = S.folders.filter((x) => x.id !== id); persist(); renderSidebar();
    } },
  ]);
}

// Both edges drag; a double-click puts one back where it started.
function resizable(grip, key, min, max, fromRight, fallback) {
  const cssVar = key === "sideWidth" ? "--side-w" : "--canvas-w";
  const apply = () => document.documentElement.style.setProperty(cssVar, (S[key] || fallback) + "px");
  apply();
  const start = (e) => {
    e.preventDefault();
    grip.classList.add("on"); document.body.classList.add("resizing");
    const frames = document.querySelectorAll("iframe");
    frames.forEach((f) => (f.style.pointerEvents = "none"));   // an iframe would swallow the drag
    const moveTo = (ev) => {
      const x = ev.touches ? ev.touches[0].clientX : ev.clientX;
      S[key] = Math.round(Math.max(min, Math.min(max, fromRight ? innerWidth - x : x)));
      apply();
    };
    const end = () => {
      grip.classList.remove("on"); document.body.classList.remove("resizing");
      frames.forEach((f) => (f.style.pointerEvents = ""));
      removeEventListener("mousemove", moveTo); removeEventListener("mouseup", end);
      removeEventListener("touchmove", moveTo); removeEventListener("touchend", end);
      persist();
    };
    addEventListener("mousemove", moveTo); addEventListener("mouseup", end);
    addEventListener("touchmove", moveTo, { passive: false }); addEventListener("touchend", end);
  };
  grip.addEventListener("mousedown", start);
  grip.addEventListener("touchstart", start, { passive: false });
  grip.addEventListener("dblclick", () => { S[key] = fallback; apply(); persist(); });
}

export function wireSidebar() {
  hooks.sidebar = renderSidebar;
  $("btn-new").onclick = newChat;
  $("btn-folder").onclick = async () => {
    const name = await ask(T().folderName, "");
    if (!name) return;
    S.folders.unshift({ id: uid(), name: name.slice(0, 28), open: true });
    persist(); renderSidebar();
  };
  $("btn-archived").onclick = () => { S.showArchived = !S.showArchived; persist(); renderSidebar(); };
  $("btn-burger").onclick = (e) => { e.stopPropagation(); $("side").classList.toggle("on"); };
  // On a phone the sidebar is a drawer: a tap anywhere else puts it away.
  document.addEventListener("click", (e) => { if (!$("side").contains(e.target)) $("side").classList.remove("on"); });
  resizable($("grip-side"), "sideWidth", 200, 460, false, 252);
  resizable($("grip-canvas"), "canvasWidth", 320, 900, true, 440);
}
