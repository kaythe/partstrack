/*
 * partstrack - a small inventory for electronic parts
 * Copyright (C) 2026 Kayla Theil (accounts@kaylatheil.de)
 *
 *     This program is free software: you can redistribute it and/or modify
 *     it under the terms of the GNU General Public License as published by
 *     the Free Software Foundation, either version 3 of the License, or
 *     (at your option) any later version.
 *
 *     This program is distributed in the hope that it will be useful,
 *     but WITHOUT ANY WARRANTY; without even the implied warranty of
 *     MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 *     GNU General Public License for more details.
 *
 *     You should have received a copy of the GNU General Public License
 *     along with this program.  If not, see <https://www.gnu.org/licenses/>.
 */

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
