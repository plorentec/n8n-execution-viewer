// Geometry of the n8n-style diagram. Pure: no DOM, so it is unit tested.
// Input is the execution returned by GET /api/executions/:id (nodes keep the
// position they have in the n8n editor). `positions` overrides node positions
// (nodes moved by the user); edges are always derived from node positions.

export const NODE_W = 100;
export const NODE_H = 100;
const LABEL_SPACE = 40;
const PADDING = 60;
const LOOP_GAP = 50;

const isSticky = (type) => /stickyNote$/i.test(type ?? "");

function nodeStatus(n) {
  if (!n.executed) return "idle";
  if (n.runs.some((r) => r.status === "error")) return "error";
  return n.runs[n.runs.length - 1]?.status ?? "success";
}

// Vertical position of handle `index` among `count` handles on a node side.
const handleY = (y, index, count) => Math.round(y + (NODE_H * (index + 1)) / (count + 1));

function forwardPath([x1, y1], [x2, y2]) {
  const dx = Math.max(40, Math.abs(x2 - x1) / 2);
  return `M ${x1} ${y1} C ${x1 + dx} ${y1} ${x2 - dx} ${y2} ${x2} ${y2}`;
}

function verticalPath([x1, y1], [x2, y2]) {
  const dy = Math.max(40, Math.abs(y2 - y1) / 2);
  return `M ${x1} ${y1} C ${x1} ${y1 - dy} ${x2} ${y2 + dy} ${x2} ${y2}`;
}

// Target behind the source: leave right, run below both nodes, enter from the left.
function loopPath([x1, y1], [x2, y2], bottom) {
  const r = 60;
  return `M ${x1} ${y1} C ${x1 + r} ${y1} ${x1 + r} ${bottom} ${x1} ${bottom} ` +
    `L ${x2} ${bottom} C ${x2 - r} ${bottom} ${x2 - r} ${y2} ${x2} ${y2}`;
}

// Recomputes the geometry of `edge` for source and target nodes at {x, y}.
export function routeEdge(edge, src, dst) {
  let start;
  let end;
  let path;
  let mid;
  if (edge.type === "main") {
    start = [src.x + NODE_W, handleY(src.y, edge.output, edge.outCount)];
    end = [dst.x, handleY(dst.y, edge.input, edge.inCount)];
    if (end[0] < start[0] + 30) {
      const bottom = Math.max(src.y, dst.y) + NODE_H + LOOP_GAP;
      path = loopPath(start, end, bottom);
      mid = [Math.round((start[0] + end[0]) / 2), bottom];
    } else {
      path = forwardPath(start, end);
    }
  } else {
    start = [src.x + NODE_W / 2, src.y];
    end = [dst.x + NODE_W / 2, dst.y + NODE_H];
    path = verticalPath(start, end);
  }
  mid ??= [Math.round((start[0] + end[0]) / 2), Math.round((start[1] + end[1]) / 2)];
  return { ...edge, start, end, path, mid };
}

export function buildDiagram(execution, positions = {}) {
  const nodes = (execution?.nodes ?? [])
    .filter((n) => Array.isArray(n.position) && !isSticky(n.type))
    .map((n) => {
      const failed = n.runs.find((r) => r.error);
      const [x, y] = positions[n.name] ?? n.position;
      return {
        name: n.name,
        type: n.type,
        x: Math.round(x),
        y: Math.round(y),
        w: NODE_W,
        h: NODE_H,
        status: nodeStatus(n),
        executed: n.executed,
        runs: n.runs.length,
        itemsOut: n.runs.reduce((s, r) => s + (r.itemsOut ?? []).reduce((a, b) => a + b, 0), 0),
        error: failed?.error ?? null,
        source: n,
      };
    });
  const byName = new Map(nodes.map((n) => [n.name, n]));
  const connections = execution?.connections ?? {};

  // Handle counts: outputs of each source, main inputs of each target.
  const outputs = new Map();
  const inputs = new Map();
  for (const [from, byType] of Object.entries(connections)) {
    const main = byType?.main ?? [];
    outputs.set(from, main.length);
    main.forEach((targets) => (targets ?? []).forEach((t) => {
      inputs.set(t.node, Math.max(inputs.get(t.node) ?? 1, (t.index ?? 0) + 1));
    }));
  }

  const edges = [];
  for (const [from, byType] of Object.entries(connections)) {
    const src = byName.get(from);
    if (!src) continue;
    for (const [type, branches] of Object.entries(byType ?? {})) {
      (branches ?? []).forEach((targets, output) => {
        for (const t of targets ?? []) {
          const dst = byName.get(t.node);
          if (!dst) continue;
          const main = type === "main";
          const items = main && src.executed
            ? src.source.runs.reduce((s, r) => s + (r.itemsOut?.[output] ?? 0), 0)
            : null;
          edges.push(routeEdge({
            from, to: dst.name, type, output, input: t.index ?? 0,
            outCount: Math.max(outputs.get(from) ?? 1, src.source.runs[0]?.itemsOut?.length ?? 0, 1),
            inCount: inputs.get(dst.name) ?? 1,
            items,
            active: main ? items > 0 : src.executed && dst.executed,
          }, src, dst));
        }
      });
    }
  }

  let bounds = { x: 0, y: 0, w: 400, h: 300 };
  if (nodes.length) {
    const minX = Math.min(...nodes.map((n) => n.x)) - PADDING;
    const minY = Math.min(...nodes.map((n) => n.y)) - PADDING;
    const maxX = Math.max(...nodes.map((n) => n.x + n.w)) + PADDING;
    const maxY = Math.max(...nodes.map((n) => n.y + n.h + LABEL_SPACE + LOOP_GAP)) + PADDING;
    bounds = { x: minX, y: minY, w: maxX - minX, h: maxY - minY };
  }

  return { nodes: nodes.map(({ source, ...n }) => n), edges, bounds };
}
