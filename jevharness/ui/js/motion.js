// The animation controller. It decides *whether* a state change is shown with
// motion; it never decides *when* the state changes. The reducer has already
// committed the real state before anything here runs, and nothing here waits
// for an animation to end.
//
// Play-once is keyed by run id + event key + phase — not by object identity —
// so a re-render, a reload or opening a record never replays anything.

// key -> { cls, at }. The class is handed out for one short window, so the
// same element rebuilt twice in a frame animates once, and after that the
// markup is static for good.
const played = new Map();
const WINDOW_MS = 700;
const now = () => (typeof performance !== "undefined" ? performance.now() : Date.now());
const staticRuns = new Set();
let preference = "system";         // system | reduced | off
let hidden = false;

const media = typeof matchMedia === "function" ? matchMedia("(prefers-reduced-motion: reduce)") : null;

export function setPreference(value) {
  preference = ["system", "reduced", "off"].includes(value) ? value : "system";
  if (typeof document !== "undefined") document.documentElement.dataset.motion = level();
}
export const getPreference = () => preference;

// What the page may do right now: full, reduced (fades only, no movement, no
// loops) or off. The system setting wins over the app's own "normal".
export function level() {
  if (preference === "off") return "off";
  if (preference === "reduced" || (media && media.matches)) return "reduced";
  return "full";
}

// History and demo records opened as static never animate.
export function markStatic(runId) { staticRuns.add(runId); }
export function isStatic(runId) { return staticRuns.has(runId); }

// Returns the class to put on a fresh element, or "" for a static update. The
// key is spent either way: coming back to a background tab shows the final
// state, it does not replay the queue.
export function claim(runId, key, phase) {
  const id = `${runId}|${key}|${phase}`;
  const seen = played.get(id);
  if (seen) return seen.cls && now() - seen.at < WINDOW_MS ? seen.cls : "";
  const lv = level();
  const cls = hidden || staticRuns.has(runId) || lv === "off" ? "" : lv === "reduced" ? "m-fade" : "m-play";
  played.set(id, { cls, at: now() });
  return cls;
}
export const hasPlayed = (runId, key, phase) => played.has(`${runId}|${key}|${phase}`);

// At most this many rows move at once; the rest update in place.
export const MAX_ANIMATED = 12;

export function wireMotion(onChange) {
  if (typeof document === "undefined") return;
  hidden = document.hidden;
  document.addEventListener("visibilitychange", () => { hidden = document.hidden; });
  if (media && media.addEventListener) media.addEventListener("change", () => { setPreference(preference); if (onChange) onChange(); });
  setPreference(preference);
}

// For tests.
export function _reset() { played.clear(); staticRuns.clear(); hidden = false; preference = "system"; }
export function _setHidden(value) { hidden = !!value; }
