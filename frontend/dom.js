// Element builder. Text is always set with textContent / text nodes, never HTML.

// Callers pass nested arrays (e.g. pairs from map) and optional children
// (cond ? node : null), so flatten fully and drop null, undefined and false.
// The DOM itself would turn null into the text "null".
function normalize(children) {
  return children
    .flat(Infinity)
    .filter((c) => c != null && c !== false)
    .map((c) => (c instanceof Node ? c : document.createTextNode(String(c))));
}

export function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (v == null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? "" : v);
  }
  node.append(...normalize(children));
  return node;
}

// replaceChildren with the same rules as el().
export function setChildren(node, ...children) {
  node.replaceChildren(...normalize(children));
}
