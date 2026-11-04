/*
 * Everything is built with createElement/textContent, never innerHTML, so no value that comes back
 * from the API can inject markup.
 */

/** el("td", { class: "num", title: "..." }, "text", childNode, ...) */
export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [name, value] of Object.entries(attrs)) {
    if (value === false || value == null) continue;
    if (name === "class") node.className = value;
    else node.setAttribute(name, value === true ? "" : String(value));
  }
  node.append(...children.flat().filter((child) => child != null && child !== false));
  return node;
}

export const $ = (selector, root = document) => root.querySelector(selector);

export function byId(id) {
  const node = document.getElementById(id);
  if (!node) throw new Error(`missing #${id} in the page`);
  return node;
}

/** Replace a node's children. */
export function fill(node, ...children) {
  node.replaceChildren(...children.flat().filter((child) => child != null && child !== false));
}

export function show(node, visible) {
  node.hidden = !visible;
}

let toastTimer = 0;

/** A short message in the page's status region (role=status). */
export function toast(message) {
  const node = document.getElementById("toast");
  if (!node) return;
  node.textContent = message;
  node.hidden = false;
  window.clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => {
    node.hidden = true;
  }, 4500);
}

/** Is the user typing somewhere (so single-key shortcuts must stay out of the way)? */
export function isTyping(target) {
  if (!(target instanceof HTMLElement)) return false;
  return target.isContentEditable || ["INPUT", "SELECT", "TEXTAREA"].includes(target.tagName);
}

export function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}
