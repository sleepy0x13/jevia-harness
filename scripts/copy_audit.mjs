// Fails if the two languages drift apart, or if the UI asks for a word that
// neither has. Run: node scripts/copy_audit.mjs
import { readFileSync, readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const ui = join(dirname(fileURLToPath(import.meta.url)), "..", "jevharness", "ui");
const { COPY } = await import(join(ui, "js", "copy.js"));
const problems = [];

function walk(a, b, path) {
  for (const k of new Set([...Object.keys(a), ...Object.keys(b)])) {
    const p = path ? `${path}.${k}` : k;
    if (!(k in a)) problems.push(`en is missing ${p}`);
    else if (!(k in b)) problems.push(`zh is missing ${p}`);
    else if (typeof a[k] !== typeof b[k]) problems.push(`${p} differs in type`);
    else if (a[k] && typeof a[k] === "object" && !Array.isArray(a[k])) walk(a[k], b[k], p);
    else if (Array.isArray(a[k]) && a[k].length !== b[k].length) problems.push(`${p} differs in length`);
  }
}
walk(COPY.en, COPY.zh, "");

const sources = readdirSync(join(ui, "js")).filter((f) => f.endsWith(".js") && f !== "copy.js")
  .map((f) => readFileSync(join(ui, "js", f), "utf8"));
const html = readFileSync(join(ui, "index.html"), "utf8");
const used = new Set();
for (const src of sources) for (const m of src.matchAll(/\bt\.([a-zA-Z_]+)/g)) used.add(m[1]);
for (const m of html.matchAll(/data-tp?="([a-zA-Z_]+)"/g)) used.add(m[1]);
for (const key of used) if (!(key in COPY.en)) problems.push(`the UI uses "${key}" but no language defines it`);

if (problems.length) { console.error(problems.join("\n")); process.exit(1); }
console.log(`copy ok: ${Object.keys(COPY.en).length} keys, ${used.size} in use`);
