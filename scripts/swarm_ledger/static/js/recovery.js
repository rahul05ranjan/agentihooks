import { $, h, span } from "./dom.js";
import { cells, emptyRow, idCell, label, pending, seatText, withId } from "./swarm.js";

export function recoveryCards(sw) {
  return (sw && sw.recovery && sw.recovery.cards) || [];
}

function lines(texts) {
  return h("div", { class: "sw-lines" }, ...texts.map((text) => h("span", { text })));
}

function choiceButton(card, c) {
  return h("button", { class: "sw-btn", type: "button", "data-recovery-choice": c.send, "data-agent": card.agent,
    "aria-label": `${c.label} for ${card.agent}`, title: c.unavailable || c.label,
    disabled: !!(c.unavailable || pending || !c.send), text: c.label });
}

function decision(card) {
  if (card.accepted) return h("span", { class: "sw-acts" }, label(card.accepted.text, "confirmed"), card.accepted.replacement ? idCell(card.accepted.replacement) : null);
  if (card.requested) return label(card.requested_text, "pending");
  return h("div", { class: "sw-ctl" }, ...card.choices.map((c) => choiceButton(card, c)));
}

export function renderRecovery(sw) {
  const cards = recoveryCards(sw);
  $("recovery-count").textContent = cards.length ? `${cards.length} ${cards.length === 1 ? "seat" : "seats"}` : "none waiting";
  $("swarm-recovery").replaceChildren(...(cards.length
    ? cards.map((card) => withId(cells(idCell(seatText(card.seat)), card.reason, label(card.badge), lines(card.material),
      lines(card.diagnosis), span(card.wait_seconds * 1000), decision(card)), `recovery-${card.seat}`))
    : [emptyRow(7, "No seat is waiting on a recovery decision.")]));
}
