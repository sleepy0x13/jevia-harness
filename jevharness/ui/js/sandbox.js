// What a generated app is allowed to do while it runs in the side panel.
//
// The frame already has no same-origin access: it cannot read this page, its
// storage, or the keys kept there. What it could still do is talk to the
// network — and an app is written by a model from material that may itself
// contain instructions. So the page is served with a policy that blocks
// outbound requests and form posts; everything it needs is inline already.

export const APP_POLICY = (origin) => [
  "default-src 'none'",
  "script-src 'unsafe-inline'",
  "style-src 'unsafe-inline'",
  "img-src data: blob:",
  // The interface's own typefaces, served by this harness, and nothing else.
  `font-src ${origin} data:`,
  "media-src data: blob:",
  "connect-src 'none'",
  "form-action 'none'",
  "base-uri 'none'",
  "frame-src 'none'",
  "object-src 'none'",
].join("; ");

const META = (origin) =>
  `<meta http-equiv="Content-Security-Policy" content="${APP_POLICY(origin)}">`;

// The page with the policy as its first header. A policy added after a script
// has already run would be too late, so it goes above everything.
export function withPolicy(html, origin = "'self'") {
  const page = String(html == null ? "" : html);
  const head = page.match(/<head[^>]*>/i);
  if (head) return page.slice(0, head.index + head[0].length) + META(origin) + page.slice(head.index + head[0].length);
  const doctype = page.match(/^\s*<!doctype[^>]*>/i);
  const at = doctype ? doctype[0].length : 0;
  return page.slice(0, at) + META(origin) + page.slice(at);
}
