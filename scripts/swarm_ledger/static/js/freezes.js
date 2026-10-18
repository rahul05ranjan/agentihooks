import { h } from "./dom.js";

const FLAKE = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 2v20M3.3 7l17.4 10M3.3 17l17.4-10"/><path d="M9 4l3 2 3-2M9 20l3-2 3 2"/></svg>';

export function snowflake(title) {
  const el = h("span", { class: "snowflake", role: "img", title, "aria-label": title });
  el.innerHTML = FLAKE;
  return el;
}

export function isSelector(target) {
  return /^(lane|kind):/.test(target);
}

function byId(list, id) {
  return list.find((i) => i.id === id);
}

export function ownFreeze(d, key) {
  return d.freezes.find((r) => r.verb === "freeze" && r.target === key);
}

export function frozen(d, key) {
  return d.marks.frozen.includes(key);
}

export function held(d, key) {
  return d.marks.held.includes(key);
}

export function setFreeze(d, op, at) {
  if (d.freezes.some((r) => r.verb === op.verb && r.target === op.target)) return;
  d.freezes.push({ id: op.id, verb: op.verb, target: op.target, by: "operator", at, reason: op.reason || "" });
}

export function clearFreeze(d, op) {
  d.freezes = d.freezes.filter((r) => r.target !== op.target);
}

export function targetText(d, target) {
  if (isSelector(target)) return target.replace(":", " ");
  const [list, id] = target.split("/");
  const item = byId(d[list] || [], id) || {};
  const names = { plans: ["plan", item.title], phases: ["phase", item.title], slices: ["slice", item.anchor], tasks: ["task", item.title] };
  const [noun, name] = names[list] || [list, id];
  return `${noun} ${name || id}`;
}
