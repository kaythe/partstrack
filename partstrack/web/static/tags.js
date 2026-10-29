// Suggests existing tags for the tag being typed in a comma-separated tag field
// (<input data-tags='["smd", ...]'>). The form works without this script.
//
// The suggestions go into a <datalist>, so the browser shows its own list.
// A datalist can only offer whole values, so each option is the field's text
// with the tag being typed completed ("smd, ti" -> "smd, timer").

"use strict";

// Lower-case ASCII letters only, like SQLite's NOCASE, which makes tags unique.
function tagKey(text) {
  return text.replace(/[A-Z]/g, (c) => c.toLowerCase());
}

// The options to offer for a field containing `value`, given the known tags.
function tagSuggestions(value, known) {
  const cut = value.lastIndexOf(",") + 1;
  const prefix = tagKey(value.slice(cut).trim());
  if (!prefix) {
    return [];
  }
  const head = value.slice(0, cut);
  const used = new Set(head.split(",").map((t) => tagKey(t.trim())));
  const start = head ? head.trimEnd() + " " : "";
  return known
    .filter((tag) => tagKey(tag).startsWith(prefix) && !used.has(tagKey(tag)))
    .map((tag) => start + tag);
}

if (typeof document !== "undefined") {
  document.querySelectorAll("input[data-tags]").forEach((input, number) => {
    const known = JSON.parse(input.dataset.tags);
    const list = document.createElement("datalist");
    list.id = "tag-suggestions-" + number;
    input.after(list);
    input.setAttribute("list", list.id);
    input.setAttribute("autocomplete", "off");
    let offered = [];

    input.addEventListener("input", (event) => {
      // Picking from the list gives no inputType (Firefox) or
      // "insertReplacementText" (Chrome); then start the next tag.
      const picked = !event.inputType || event.inputType === "insertReplacementText";
      if (picked && offered.includes(input.value)) {
        input.value += ", ";
      }
      offered = tagSuggestions(input.value, known);
      list.replaceChildren(...offered.map((value) => new Option(value)));
    });
  });
}
