// The panel of Protocallist: the list of conversations and one conversation.
// Everything shown is put into the page as text, never as markup, and no style is written into markup:
// the page is served with a policy that lets it load and run only its own files.
"use strict";

const S = {
  texts: {}, lang: "ru", status: null, calls: [], found: null, lost: false,
  sort: { key: "when", dir: "desc" }, program: "", period: "all", from: "", to: "", q: "",
  hide: localStorage.getItem("hide") !== "no", limit: Number(localStorage.getItem("limit") || 30),
  call: null, sure: "", within: "", who: "both", audio: null, graph: null, searching: 0,
  busy: false,          // a request that changes something is on its way
  sumOpen: true,        // the summary of the conversation on the screen is unfolded
  models: [],           // the models of the gateway: what a conversation can be recognised anew with
  anew: false,          // the choice of one of them is open on the page of a conversation
  picked: new Set(),    // the conversations ticked in the list, to be sent for their summaries
  sending: null,        // those sent just now, until the program says where they stand
  asking: null,         // what waits for the user's word on a line that stands still: { ids } or { one, anew }
  player: null,     // the player on the screen, or what stands in its place while there is no sound
  top: "",          // what the top line was drawn from
};
const $ = (id) => document.getElementById(id);
const N = (key) => key;        // a text that is put into words later, by t(): named here so that it is known

// ---------- small things

function el(tag, props, ...kids) {
  const node = document.createElement(tag);
  for (const [name, value] of Object.entries(props || {})) {
    if (name === "pressed") { node.setAttribute("aria-pressed", String(Boolean(value))); continue; }
    if (value === undefined || value === null || value === false) continue;
    if (name === "class") node.className = value;
    else if (name === "text") node.textContent = value;
    else if (name === "spellcheck" || name === "writingsuggestions") node.setAttribute(name, value);     // as a word: the property takes "false" for a yes
    else if (name.startsWith("on")) node.addEventListener(name.slice(2), value);
    else if (name in node) node[name] = value;
    else node.setAttribute(name, value === true ? "" : value);
  }
  return add(node, kids);
}

// what is absent (null, undefined, false) is left out: a browser would write it on the page as a word
function add(node, ...kids) {
  for (const kid of kids.flat(2)) if (kid !== null && kid !== undefined && kid !== false) node.append(kid);
  return node;
}

function fill(node, ...kids) {
  node.replaceChildren();
  return add(node, kids);
}

function t(key, values) {
  const text = (S.texts[key] || {})[S.lang] || (S.texts[key] || {}).en || key;
  return text.replace(/\{(\w+)\}/g, (_, name) => (values && name in values ? values[name] : `{${name}}`));
}

async function ask(method, path, body) {
  const how = { method, headers: {} };
  if (method !== "GET") how.headers["X-Protocallist"] = "1";
  if (body !== undefined) { how.headers["Content-Type"] = "application/json"; how.body = JSON.stringify(body); }
  try {
    const answer = await fetch(path, how);
    let data = {};
    try { data = await answer.json(); } catch (e) { /* an answer without a body */ }
    return { ok: answer.ok, status: answer.status, data };
  } catch (e) {
    return { ok: false, status: 0, data: {} };
  }
}

const pad = (n) => String(n).padStart(2, "0");
const clock = (s) => `${pad(Math.floor(s / 3600))}:${pad(Math.floor(s / 60) % 60)}:${pad(Math.floor(s) % 60)}`;
const length = (s) => (s < 60 ? t("{n} s", { n: s }) : t("{n} min", { n: Math.round(s / 60) }));
const day = (date) => `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
const short = (iso) => `${iso.slice(8, 10)}.${iso.slice(5, 7)}`;
const making = (id) => Boolean(S.status && S.status.making.includes(id));
const queued = (id) => Boolean((S.status && S.status.queued.includes(id)) || (S.sending && S.sending.has(id)));
const failure = (id) => (S.status && S.status.failures[String(id)]) || "";
const stood = () => (S.status && S.status.stopped) || null;      // the line of summaries that stands still, if one does
// A conversation that can be ticked: recognised, with something said, without a summary and not on its way to one.
const free = (c) => c.state === "done" && c.remarks > 0 && !c.summary && !making(c.id) && !queued(c.id);

function dayTitle(iso) {
  const now = new Date(), yesterday = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1), date = new Date(iso);
  const said = new Intl.DateTimeFormat(S.lang, { day: "numeric", month: "long", year: date.getFullYear() === now.getFullYear() ? undefined : "numeric" }).format(date);
  if (iso.slice(0, 10) === day(now)) return t("Today, {date}", { date: said });
  if (iso.slice(0, 10) === day(yesterday)) return t("Yesterday, {date}", { date: said });
  return said;
}

function soundText(sound) {
  if (sound.state === "deleted") return t("deleted {date}", { date: short(sound.at) });
  if (sound.state === "raw") return t("not made small yet");
  if (sound.state !== "kept") return "—";
  if (!sound.until) return t("for ever");
  return t("{n} more days", { n: Math.max(0, Math.ceil((new Date(sound.until) - new Date()) / 864e5)) });
}

function stateNode(call) {
  if (call.state === "done") return el("span", { class: call.redo ? "state" : "state plain", text: t(call.redo ? N("being recognised anew") : N("recognised")) });
  if (call.state === "failed") {
    return [el("span", { class: "state failed", text: t("not recognised") }), " ",
      el("button", { type: "button", class: "linkbtn", text: t("try again"), onclick: (e) => { e.stopPropagation(); act("POST", `/api/calls/${call.id}/retry`); } })];
  }
  const words = { recording: N("being recorded"), transcribing: N("being recognised"), recorded: S.status && S.status.gateway ? N("waits for the gateway") : N("waits to be recognised") };
  return el("span", { class: "state", text: t(words[call.state] || call.state) });
}

// Ask for something to be done. One such request at a time: the second click of a double one asks for nothing.
async function act(method, path, body) {
  if (S.busy) return false;
  S.busy = true;
  try {
    const answer = await ask(method, path, body);
    trouble(answer.ok ? "" : answer.data.error || t("Protocallist does not answer"));
    await tick();
    return answer.ok;
  } finally { S.busy = false; }
}

function trouble(text) {
  $("trouble").hidden = !text;
  $("trouble").textContent = text;
}

// ---------- the top: what the program is doing now

function drawTop() {
  const s = S.status;
  // Drawn anew only when it would say something else: a button must not be replaced under the pointer every two seconds.
  const says = JSON.stringify([s, S.lang, S.sure, document.documentElement.dataset.theme]);
  if (says === S.top) return;
  S.top = says;
  $("brand").textContent = t("Protocallist");
  $("theme").textContent = document.documentElement.dataset.theme === "dark" ? t("light theme") : t("dark theme");
  if (!s) return;
  $("version").textContent = s.version;
  const now = $("now");
  fill(now);
  now.className = "now";
  const dot = el("span", { class: "dot" });
  if (s.state === "recording") {
    now.classList.add("rec");
    add(now, dot, el("span", { text: t("recording: {program}, {minutes} min", s) }), sure(`discard ${s.recording}`, t("Stop and delete this recording"),
      () => act("POST", "/api/recording/discard", { id: s.recording }), null));
  } else if (s.state === "error") {
    now.classList.add("bad");
    add(now, dot, el("span", { text: t("error: {problem}", s) }));
  } else {
    const words = { off: N("recording is off"), "no microphone": N("no microphone is chosen"), missing: N("the microphone is not found"), waiting: N("waits for a conversation") };
    if (s.state !== "waiting") now.classList.add("off");
    add(now, dot, el("span", { text: t(words[s.state] || s.state) }), s.microphone ? el("span", { class: "dim", text: `· ${s.microphone}` }) : null);
  }
  const queue = [];
  if (s.recognising) queue.push(t("being recognised: 1, waiting: {waiting}", s));
  else if (s.waiting) queue.push(t(s.gateway === "old" ? N("a newer Tolmach is needed, waiting: {waiting}") : s.gateway ? N("waiting for the gateway: {waiting}") : N("waiting to be recognised: {waiting}"), s));
  if (s.failed) queue.push(t("not recognised: {failed}", s));
  $("queue").textContent = queue.join(" · ");
}

// A thing that cannot be undone takes two clicks: the button turns into a question - whether to delete
// for good, unless another `question` is given. `look` is how the button is dressed.
function sure(what, label, done, look, question) {
  if (S.sure !== what) {
    return el("button", { type: "button", class: look, text: label, onclick: (e) => { e.stopPropagation(); S.sure = what; redraw(); } });
  }
  return el("span", { class: question ? "sure calm" : "sure" }, question || t("Delete for good?"), " ",
    el("button", { type: "button", class: "linkbtn", text: t("Yes"), onclick: (e) => { e.stopPropagation(); S.sure = ""; redraw(); done(); } }), " / ",
    el("button", { type: "button", class: "linkbtn", text: t("No"), onclick: (e) => { e.stopPropagation(); S.sure = ""; redraw(); } }));
}

function redraw() {
  drawTop();
  if (S.call) { drawHead(); drawSummary(); drawText(); } else drawRows();
}

// ---------- the list

function visible() {
  const q = S.q.trim().toLowerCase(), today = new Date();
  const since = S.period === "all" || S.period === "custom" ? "" : day(new Date(today.getFullYear(), today.getMonth(), today.getDate() - Number(S.period) + (S.period === "0" ? 0 : 1)));
  return S.calls.filter((c) => {
    const date = c.started.slice(0, 10);
    if (S.program && c.program !== S.program) return false;
    if (S.hide && c.state !== "recording" && c.seconds < S.limit) return false;
    if (since && date < since) return false;
    if (S.period === "custom" && ((S.from && date < S.from) || (S.to && date > S.to))) return false;
    return !q || (S.found && S.found.has(c.id)) || `${c.topic} ${c.program}`.toLowerCase().includes(q);
  });
}

function sorted(rows) {
  const keys = { when: (c) => c.started, program: (c) => c.program.toLowerCase(), seconds: (c) => c.seconds, remarks: (c) => c.remarks };
  const key = keys[S.sort.key], way = S.sort.dir === "asc" ? 1 : -1;
  return [...rows].sort((a, b) => (key(a) > key(b) ? way : key(a) < key(b) ? -way : a.started < b.started ? 1 : -1));
}

function showList() {
  S.call = null;
  stopSound();
  const programs = programsNow();
  S.programs = programs.join("\n");
  const number = el("input", { type: "number", min: 0, max: 3600, value: S.limit, "aria-label": t("seconds"), oninput: (e) => { S.limit = Number(e.target.value) || 0; localStorage.setItem("limit", S.limit); drawRows(); } });
  const range = el("span", { class: "pick", id: "range", hidden: S.period !== "custom" },
    el("input", { type: "date", value: S.from, "aria-label": t("from"), oninput: (e) => { S.from = e.target.value; drawRows(); } }), "—",
    el("input", { type: "date", value: S.to, "aria-label": t("to"), oninput: (e) => { S.to = e.target.value; drawRows(); } }));
  fill($("screen"),
    el("section", { class: "filters" },
      el("label", { class: "search" }, el("input", { type: "search", spellcheck: "false", writingsuggestions: "false", value: S.q, placeholder: t("Search in what was said"), "aria-label": t("Search in what was said"), oninput: search })),
      el("div", { class: "chips", id: "programs" }, [["", t("All")], ...programs.map((p) => [p, p])].map(([value, label]) =>
        el("button", { type: "button", class: "chipbtn", text: label, pressed: S.program === value, onclick: () => { S.program = value; showList(); } }))),
      el("label", { class: "pick" }, t("Period"), el("select", { onchange: (e) => { S.period = e.target.value; range.hidden = S.period !== "custom"; drawRows(); } },
        [["all", N("all time")], ["0", N("today")], ["7", N("7 days")], ["30", N("30 days")], ["custom", N("from … to …")]].map(([value, label]) =>
          el("option", { value, text: t(label), selected: S.period === value })))),
      range,
      el("label", { class: "pick" }, el("input", { type: "checkbox", checked: S.hide, onchange: (e) => { S.hide = e.target.checked; localStorage.setItem("hide", S.hide ? "yes" : "no"); drawRows(); } }),
        t("Hide shorter than"), number, t("s"))),
    el("div", { id: "bars" }),
    el("div", { class: "sheet" }, el("table", {}, el("thead", { id: "heads" }), el("tbody", { id: "rows" }))),
    el("div", { class: "foot", id: "foot" }));
  drawRows();
  look(0);
}

const programsNow = () => [...new Set(S.calls.map((c) => c.program))].sort();

function search(e) {
  S.q = e.target.value;
  if (!S.q.trim()) S.found = null;
  drawRows();
  look(300);                                     // not at every letter: a look through years of talk takes a moment
}

// Ask in which conversations the words were said - after a typed letter, and again when the list has changed.
function look(wait) {
  const mine = ++S.searching, q = S.q.trim();
  if (!q) return;
  setTimeout(async () => {
    if (mine !== S.searching) return;
    const answer = await ask("GET", `/api/calls?q=${encodeURIComponent(q)}`);
    if (mine === S.searching) { S.found = answer.ok ? new Set(answer.data.ids) : null; drawRows(); }
  }, wait);
}

function drawRows() {
  if (S.call || !$("rows")) return;
  const head = (key, label, right) => el("th", { class: right ? "right" : null }, el("button", { type: "button", pressed: S.sort.key === key,
    text: t(label) + (S.sort.key === key ? (S.sort.dir === "asc" ? " ↑" : " ↓") : ""),
    onclick: () => { S.sort = { key, dir: S.sort.key === key ? (S.sort.dir === "asc" ? "desc" : "asc") : key === "program" ? "asc" : "desc" }; drawRows(); } }));
  const rows = sorted(visible()), open = rows.filter(free), flat = S.sort.key !== "when", out = [];
  // Chosen is only what is on the screen with a tick: the button sends nothing the user does not see.
  for (const id of [...S.picked]) if (!open.some((c) => c.id === id)) S.picked.delete(id);
  const pick = (c, yes) => { if (yes) S.picked.add(c.id); else S.picked.delete(c.id); };
  fill($("heads"), el("tr", {},
    el("th", { class: "tick" }, el("input", { type: "checkbox", title: t("choose all that have no summary"), "aria-label": t("choose all that have no summary"), disabled: !open.length,
      checked: open.length > 0 && open.every((c) => S.picked.has(c.id)), onchange: (e) => { for (const c of open) pick(c, e.target.checked); drawRows(); refocus(0); } })),
    head("when", N("Time")), head("program", N("Program")), el("th", { text: t("With whom") }), el("th", { text: t("About") }),
    head("seconds", N("Length"), true), head("remarks", N("Remarks"), true), el("th", { text: t("State") }), el("th", { text: t("Summary") }), el("th", { text: t("Sound") })));
  let last = "";
  for (const c of rows) {
    const date = c.started.slice(0, 10);
    if (!flat && date !== last) { last = date; out.push(el("tr", { class: "day" }, el("th", { colSpan: 10, text: dayTitle(c.started) }))); }
    const about = c.state !== "done" ? el("span", { class: "dimmer", text: t("no text yet") })
      : el("a", { href: `#/call/${c.id}` }, c.about === "start" ? el("span", { class: "lead", text: t("beginning: ") }) : null, c.topic || t("(nothing was said)"));
    out.push(el("tr", { class: S.picked.has(c.id) ? "row chosen" : "row", "data-id": c.id, onclick: () => { location.hash = `#/call/${c.id}`; } },
      el("td", { class: "tick", onclick: (e) => e.stopPropagation() }, free(c) ? el("input", { type: "checkbox", "aria-label": t("choose for a summary"), checked: S.picked.has(c.id),
        onchange: (e) => { pick(c, e.target.checked); drawRows(); refocus(c.id); } }) : null),
      el("td", { class: "num nowrap", text: (flat ? `${short(c.started)} ` : "") + c.started.slice(11, 16) }), el("td", { text: c.program }), el("td", { class: "dim", text: "—" }),
      el("td", { class: c.about === "summary" ? "about" : "about start" }, about),
      el("td", { class: "right num", text: length(c.seconds) }), el("td", { class: "right num dim", text: c.remarks || "" }),
      el("td", {}, stateNode(c)), el("td", {}, summaryNode(c)), el("td", { class: "dim nowrap", text: soundText(c.sound) })));
  }
  if (!out.length) out.push(el("tr", {}, el("td", { colSpan: 10, class: "nothing", text: S.calls.length ? t("Nothing is found. Take off a filter or the tick that hides the short ones.") : S.status && !S.status.archive ? t("There is no archive yet.") : t("No conversations yet.") })));
  fill($("rows"), out);
  drawBars();
  const seconds = S.calls.reduce((n, c) => n + c.seconds, 0), bytes = S.calls.reduce((n, c) => n + (c.sound.bytes || 0), 0), days = S.status ? S.status.keep_audio_days : 30;
  fill($("foot"),
    el("span", { text: t("Shown {shown} of {all}", { shown: rows.length, all: S.calls.length }) }),
    el("span", { text: t("{hours} h {minutes} min of talk in all", { hours: Math.floor(seconds / 3600), minutes: Math.round(seconds % 3600 / 60) }) }),
    el("span", { text: t("the sound takes {mb} MB", { mb: (bytes / 1e6).toFixed(0) }) }),
    el("span", { text: days < 0 ? t("the sound is kept for ever") : days === 0 ? t("the sound is not kept") : t("the sound is kept {days} days, the text for ever", { days }) }));
}

// The list is drawn anew at every tick: the keyboard goes back to the box it was on - of a conversation, or the one in the head.
function refocus(id) {
  const box = document.querySelector(id ? `tr.row[data-id="${id}"] .tick input` : "thead .tick input");
  if (box) box.focus();
}

// What the list says of the summary of a conversation: there is one, or where the conversation stands on its way to one.
function summaryNode(c) {
  const still = stood();
  if (c.state !== "done") return el("span", { class: "dimmer", text: "—" });
  if (c.summary) return el("span", { class: "has", text: t("yes") });
  if (making(c.id)) return el("span", { class: "dim nowrap", text: t("being made") });
  if (queued(c.id)) return el("span", { class: "dim nowrap", text: t("in the line") });
  if (still && still.failed === c.id) return el("span", { class: "state failed", text: t("did not work") });
  if (still && still.waiting.includes(c.id)) return el("span", { class: "dim nowrap", text: t("not sent") });
  return el("span", { class: "dimmer", text: c.remarks ? t("none") : "—" });
}

// Above the list: the line of summaries that stands still, the one that goes, and - always - how many
// conversations are ticked, with the one button that sends their text away and says where to.
function drawBars() {
  const s = S.status, still = stood(), n = S.picked.size, where = s ? s.summary : { kind: "", name: "" };
  const going = s ? s.making.length + s.queued.length : 0;
  const to = where.kind === "command" ? t("The text of the chosen conversations goes to the command: {name}.", where)
    : where.kind ? t("The text of the chosen conversations goes to Claude (model {name}).", where) : "";
  fill($("bars"),
    still ? el("div", { class: "bar bad", id: "stood" }, el("b", { text: t("The line stands still") }), el("span", { text: still.reason }), el("span", { class: "grow" }),
      el("button", { type: "button", class: "btn primary", text: t("Start again: {n}", { n: (still.failed ? 1 : 0) + still.waiting.length }), onclick: () => act("POST", "/api/summaries/restart") }),
      el("button", { type: "button", class: "linkbtn", text: t("forget it"), onclick: () => act("POST", "/api/summaries/reset") })) : null,
    going ? el("div", { class: "bar", id: "going" }, el("b", { text: t("Summaries: being made {making}, in the line {queued}", { making: s.making.length, queued: s.queued.length }) }),
      el("span", { class: "dim", text: t("The page may be closed: the line goes on in the program itself.") }), el("span", { class: "grow" }),
      s.queued.length ? el("button", { type: "button", class: "linkbtn", text: t("stop the line"), onclick: () => act("POST", "/api/summaries/halt") }) : null) : null,
    el("div", { class: "bar", id: "chosen" }, el("b", { text: t("Chosen: {n}", { n }) }),
      el("span", { class: "dim", text: to ? `${to} ${t("Summaries are made one after another, about a minute each.")}` : "" }), el("span", { class: "grow" }),
      el("button", { type: "button", class: "btn primary", disabled: !n, text: t("Make summaries: {n}", { n }),
        onclick: () => send({ ids: sorted(S.calls.filter((c) => S.picked.has(c.id))).map((c) => c.id) }) }),      // in the order of the list
      n ? el("button", { type: "button", class: "linkbtn", text: t("take the ticks off"), onclick: () => { S.picked.clear(); drawRows(); } }) : null));
}

// Send conversations to be summarised: those ticked in the list ({ ids }) or the one on the screen
// ({ one, anew }). This is the click that sends their text away. While a line stands still the user says
// first what becomes of it: `old` is "keep" - it starts again, with these - or "drop" - it is forgotten.
async function send(what, old) {
  if (stood() && !old) { S.asking = what; drawAsk(); return; }
  if (S.busy) return;                            // the second click of a double one sends nothing
  S.busy = true;
  try {
    S.asking = null;
    drawAsk();
    const ids = what.ids || [what.one], body = old ? { old } : {};
    if (what.ids) {
      body.ids = ids;
      S.sending = new Set(ids);                  // on their way from this moment: their ticks go
      drawRows();
    } else if (what.anew) body.anew = true;
    const answer = await ask("POST", what.ids ? "/api/summaries" : `/api/calls/${what.one}/summary`, body);
    const asks = !answer.ok && Boolean(answer.data.stopped) && !old;     // the line stopped, and the page had not heard of it yet
    trouble(answer.ok || asks ? "" : answer.data.error || t("Protocallist does not answer"));
    await tick();
    S.sending = null;
    if (!answer.ok && what.ids) for (const id of ids) S.picked.add(id);  // not taken: their ticks are back
    if (asks && stood()) S.asking = what;
  } finally { S.busy = false; }
  redraw();
  drawAsk();
}

// The question about a line that stands still, over the page.
function drawAsk() {
  const box = $("asking"), what = S.asking, still = stood();
  if (!what || !still) {
    S.asking = null;
    if (!box.hidden) { box.hidden = true; fill(box); }
    return;
  }
  box.hidden = false;
  fill(box, el("div", { class: "ask", role: "dialog", "aria-modal": "true", "aria-labelledby": "ask-title" },
    el("h3", { id: "ask-title", text: t("There is a line that stands still") }),
    el("p", { text: t("Conversations in it: {n}. Why it stopped: {reason}. What is to become of it, now that more is sent?", { n: (still.failed ? 1 : 0) + still.waiting.length, reason: still.reason }) }),
    el("div", { class: "choice" },
      el("button", { type: "button", class: "btn primary", id: "ask-first", text: t("Start anew"), onclick: () => send(what, "drop") }),
      el("button", { type: "button", class: "btn", text: t("Add to the line"), onclick: () => send(what, "keep") }),
      el("span", { class: "grow" }),
      el("button", { type: "button", class: "linkbtn", text: t("cancel"), onclick: () => { S.asking = null; drawAsk(); } })),
    el("p", { class: "hint", text: what.ids ? t("“Start anew”: the line that stood is forgotten, only what is chosen goes. “Add to the line”: the line that stood starts again, what is chosen joins its end.")
      : t("“Start anew”: the line that stood is forgotten, only this conversation goes. “Add to the line”: the line that stood starts again, this conversation goes first.") })));
  $("ask-first").focus();
}

function target() {
  const where = S.status ? S.status.summary : { kind: "", name: "" };
  return where.kind === "command" ? t("A click sends the text of this conversation to the command: {name}", where) : t("A click sends the text of this conversation to Claude (model {name})", where);
}

// ---------- one conversation

async function showCall(id) {
  stopSound();
  const answer = await ask("GET", `/api/calls/${id}`);
  if (location.hash !== `#/call/${id}`) return;       // the user has gone elsewhere meanwhile
  if (!answer.ok) {
    S.call = null;
    fill($("screen"), el("div", { class: "notice calm" }, el("span", { text: answer.data.error || t("There is no such conversation.") }), el("a", { href: "#/", text: t("All conversations") })));
    return;
  }
  const c = S.call = answer.data;
  S.within = ""; S.sure = ""; S.who = "both"; S.anew = false;
  S.sumOpen = true;       // a summary, when there is one, is what the page is opened for: the text waits folded under it
  fill($("screen"),
    el("a", { class: "back", href: "#/", text: `‹ ${t("All conversations")}` }),
    el("div", { id: "head" }), (S.player = player(c)),
    el("details", { class: "panel fold slim", id: "facts-fold" }, el("summary", {}, el("h2", { text: t("Details") })), el("div", { class: "factline", id: "facts" })),
    el("div", { class: "stack" },
      el("div", { id: "sum-box" }),
      el("details", { class: "panel fold", id: "text-fold", open: !c.summary },
        el("summary", {}, el("h2", { text: t("Text") }), el("span", { class: "count", id: "text-count" })),
        el("div", { class: "tools" },
          el("span", { class: "acts", id: "text-acts" }),
          el("label", { class: "search" }, el("input", { type: "search", spellcheck: "false", writingsuggestions: "false", placeholder: t("Find in this conversation"), "aria-label": t("Find in this conversation"), oninput: (e) => { S.within = e.target.value; drawRemarks(); } }))),
        el("div", { id: "anew-note" }), el("div", { id: "remarks" }))));
  drawHead(); drawRemarks(); drawSummary(); drawText();
  models();       // not waited for: the page is there before the gateway has answered
  window.scrollTo(0, 0);
}

async function refreshCall() {
  if (!S.call) return;
  const id = S.call.id;
  const answer = await ask("GET", `/api/calls/${id}`);
  if (!S.call || S.call.id !== id) return;            // another conversation, or the list, is on the screen by now
  if (!answer.ok) { if (answer.status === 404) location.hash = "#/"; return; }
  const had = S.call.remarks_list.length, sound = S.call.sound.state, summarised = Boolean(S.call.summary), written = S.call.text_at;
  S.call = answer.data;
  if (!summarised && S.call.summary) S.sumOpen = false;       // it came while the page was open: it stands folded where the button was, and nothing moves under one who reads
  if (S.call.sound.state !== sound) {            // the sound was made small meanwhile, or deleted: another player
    stopSound();
    const fresh = player(S.call);
    S.player.replaceWith(fresh);
    S.player = fresh;
  }
  drawHead(); drawSummary(); drawText();
  if (S.call.remarks_list.length !== had || S.call.text_at !== written) drawRemarks();       // another text, be it of as many remarks
  if (!S.models.length) models();       // the gateway may have come since the conversation was opened
}

function drawHead() {
  const c = S.call;
  if (!c || !$("head")) return;
  const notice = c.state === "failed" ? el("div", { class: "notice" }, el("span", { text: t("Not recognised after {attempts} attempts: {error}. The sound is there.", c) }),
    el("button", { type: "button", class: "btn", text: t("Try to recognise again"), onclick: () => act("POST", `/api/calls/${c.id}/retry`) }))
    : c.state !== "done" ? el("div", { class: "notice calm", text: t(c.state === "recording" ? N("This conversation is being recorded now.") : c.state === "transcribing" ? N("This conversation is being recognised now.") : S.status && S.status.gateway ? (S.status.own ? N("It waits for the recognition service and will be recognised by itself when the service answers.") : N("It waits for the gateway of Tolmach and will be recognised by itself when the gateway is there.")) : N("It waits to be recognised.")) }) : null;
  fill($("head"),
    el("div", { class: "rhead" },
      el("div", {}, el("h1", { text: c.about === "summary" ? c.topic : `${c.program}, ${length(c.seconds)}` }),
        el("div", { class: "facts" }, el("span", { text: dayTitle(c.started) }), el("span", { class: "num", text: `${c.started.slice(11, 16)}${c.ended ? `–${c.ended.slice(11, 16)}` : ""}` }),
          el("span", { text: c.program }), el("span", { text: length(c.seconds) }))),
      el("div", { class: "acts" },
        c.sound.state === "kept" ? el("button", { type: "button", class: "btn", text: t("Show the file of the sound"), onclick: () => act("POST", `/api/calls/${c.id}/reveal`) }) : null,
        sure("forget", t("Delete the conversation"), async () => { if (await act("DELETE", `/api/calls/${c.id}`)) location.hash = "#/"; }, "btn danger"))),
    notice);
  fill($("facts"), ...[
    [t("Sound"), c.sound.state === "kept" ? `${(c.sound.bytes / 1e6).toFixed(1)} ${t("MB")} · ${soundText(c.sound)}` : soundText(c.sound)],
    [t("Text"), c.state === "done" ? `${t("{n} remarks", { n: c.remarks_list.length })} · ${c.model}` : t("no text yet")],
    [t("Summary"), c.summary ? t("yes") : t("none yet")],
    [t("Files"), c.sound.state === "kept" ? `${c.name}.opus${c.summary ? `, ${c.name}.md` : ""}` : c.summary ? `${c.name}.md` : "—"],
  ].map(([name, value]) => el("span", {}, el("b", { text: name }), value)));       // one line, not a table; the program is in the head already
}

function drawRemarks() {
  const c = S.call, q = S.within.trim().toLowerCase();
  if (!c.remarks_list.length) { fill($("remarks"), el("div", { class: "more", text: c.state === "done" ? t("Nothing was said in this conversation.") : t("No text yet: the conversation is not recognised.") })); return; }
  const shown = c.remarks_list.filter((r) => !q || r.text.toLowerCase().includes(q));
  fill($("remarks"), ...shown.map((r) => el("div", { class: `remark ${r.who}`, "data-at": r.at },
    el("button", { type: "button", class: "t", text: clock(r.at), onclick: () => seek(r.at) }),
    el("div", {}, el("span", { class: "sp", text: r.who === "me" ? t("Me") : t("Others") }), el("p", { text: r.text })))),
  q && !shown.length ? el("div", { class: "more", text: t("These words are not in this conversation.") }) : null);
  follow();
}

// A model as it is named to the user: the language it hears, and its own name.
function modelName(name) {
  const model = S.models.find((known) => known.name === name);
  let language = "";
  try { language = model && model.language ? new Intl.DisplayNames([S.lang], { type: "language" }).of(model.language) : ""; } catch (e) { /* not a language the browser knows */ }
  return language ? `${language} (${name})` : name;
}

// What is done with the text of the conversation on the screen: it is saved as a file, and recognised anew with
// another model of the gateway - the link, the choice of a model, the question. Over the text, how that stands:
// the text there is stays until the new one is whole.
function drawText() {
  const c = S.call, acts = $("text-acts");
  if (!c || !acts) return;
  // how it stands is said in the head of the part too: the part is folded when the conversation has a summary
  $("text-count").textContent = [c.remarks_list.length ? t("{n} remarks", { n: c.remarks_list.length }) : "",
    c.redo ? t("being recognised anew") : c.redo_error ? t("it could not be recognised anew") : ""].filter(Boolean).join(" · ");
  const can = c.sound.state === "kept" && !c.redo && S.models.length > 0;       // sound that is kept is that of a recognised conversation
  if (!can) { S.anew = false; if (S.sure.startsWith("anew ")) S.sure = ""; }       // asked for elsewhere meanwhile: the choice does not come back open by itself
  const choice = !can ? null : !S.anew ? el("button", { type: "button", class: "linkbtn", text: t("recognise anew…"), onclick: () => { S.anew = true; drawText(); models(); } })
    : el("span", { class: "sure calm" }, t("With which model?"), " ",
      S.models.flatMap((m) => [m.installed
        ? sure(`anew ${m.name}`, m.name === c.model ? t("{model} — now", { model: modelName(m.name) }) : modelName(m.name), () => recognise(m.name), "linkbtn", t("{model} — replace the text?", { model: modelName(m.name) }))
        : el("span", { class: "dimmer", text: t("{model} — not installed: download it in the menu of Tolmach", { model: modelName(m.name) }) }), " · "]),
      el("button", { type: "button", class: "linkbtn", text: t("cancel"), onclick: () => { S.anew = false; S.sure = ""; drawText(); } }));
  fill(acts, c.remarks_list.length ? el("a", { class: "linkbtn", href: `/api/calls/${c.id}/text.md`, download: "", text: t("save as a file") }) : null, choice);
  fill($("anew-note"), c.redo ? el("p", { class: "sumnote" }, el("b", { text: t("Being recognised anew: {model}…", { model: modelName(c.redo) }) }), " ", t("The text there is stays until the new one is ready."), " ",
    el("button", { type: "button", class: "linkbtn", text: t("take it back"), onclick: () => act("DELETE", `/api/calls/${c.id}/recognise`) }))
    : c.redo_error ? el("p", { class: "sumnote bad", text: t("It could not be recognised anew: {reason}", { reason: c.redo_error }) }) : null);
}

// What the gateway has now. Asked when a conversation is opened, and again when the choice is: a model may have
// been downloaded since. No answer is no models - and no link.
async function models() {
  S.models = (await ask("GET", "/api/models")).data.models || [];
  drawText();
}

function recognise(model) {
  S.anew = false;
  drawText();       // the choice is closed whatever the answer will be
  if (S.call) act("POST", `/api/calls/${S.call.id}/recognise`, { model });
}

// a summary is plain text - "# title", "## heading", "- [hh:mm:ss] point", lines - and is drawn from it, all of it:
// whoever wrote the instructions for it decides what it holds
function drawn(text) {
  const out = [];
  let list = null;
  for (const raw of text.split("\n")) {
    const line = raw.trim();
    if (!line) { list = null; continue; }
    if (line.startsWith("#")) { list = null; out.push(el("h3", { class: /^#\s/.test(line) ? "title" : null, text: line.replace(/^#+\s*/, "") })); continue; }
    const point = line.match(/^[-*]\s+(?:\[(\d\d):(\d\d):(\d\d)\]\s*)?(.*)$/);
    if (!point) { list = null; out.push(el("p", { text: line })); continue; }
    if (!list) { list = el("ul"); out.push(list); }
    const at = point[1] ? Number(point[1]) * 3600 + Number(point[2]) * 60 + Number(point[3]) : null;
    add(list, at === null ? el("li", { class: "plain" }, el("span", { text: point[4] }))
      : el("li", {}, el("button", { type: "button", class: "t", text: clock(at), onclick: () => seek(at) }), el("span", { text: point[4] })));
  }
  return out;
}

// The summary of the conversation on the screen. While there is none, one line stands where it will be,
// with the button that sends the text away; when there is one, it is a part that folds, like the text.
function drawSummary() {
  const c = S.call, box = $("sum-box");
  if (!c || !box) return;
  const shown = c.summaries[c.summary], busy = making(c.id), waits = queued(c.id), failed = failure(c.id);
  const note = failed ? t("It did not work: {reason}", { reason: failed }) : "";
  if (!shown) {
    const can = c.state === "done" && c.remarks_list.length > 0 && !busy && !waits;
    const says = c.state !== "done" ? "" : busy ? t("Being made…") : waits ? t("In the line…") : t("There is no summary yet.");
    const hint = c.state !== "done" ? t("A summary can be made when the conversation is recognised.")
      : busy ? t("About a minute, as a rule. The page need not stay open.") : waits ? t("Summaries are made one at a time. The page need not stay open.")
        : can ? `${target()}. ${t("Nothing else goes anywhere.")}` : t("Nothing was said in this conversation.");
    fill(box, el("div", { class: "nosum" }, note ? el("span", { class: "bad", text: note }) : null, says ? el("b", { text: says }) : null,
      el("span", { class: "dim", text: hint }), el("span", { class: "grow" }),
      can ? el("button", { type: "button", class: "btn primary", text: t("Make a summary"), onclick: () => send({ one: c.id }) }) : null));
    return;
  }
  if ($("sum-fold")) S.sumOpen = $("sum-fold").open;       // as the user left it
  const when = `${short(shown.saved)} ${shown.saved.slice(11, 16)}`;
  const made = (c.summary === "made" ? t("made {when} · {maker} · {seconds} s", { when, maker: shown.maker || "—", seconds: Math.round(shown.seconds || 0) }) : when)
    + (shown.stale ? ` · ${t("by the text before")}` : "");       // the text was recognised anew since: the summary is not made anew by itself
  const again = busy || waits ? null : sure("remake", t("make again"), () => send({ one: c.id, anew: true }), "linkbtn", t("Replace the summary with a new one?"));
  if (again && again.tagName === "BUTTON") again.title = target();
  fill(box, el("details", { class: "panel fold", id: "sum-fold", open: S.sumOpen },
    el("summary", {}, el("h2", { text: t("Summary") }), el("span", { class: "count", text: made })),      // who made it and when: seen when it is folded too
    note ? el("p", { class: "sumnote bad", text: note }) : null,
    busy || waits ? el("p", { class: "sumnote" }, el("b", { text: busy ? t("A new one is being made…") : t("In the line…") }), " ", t("The one before is shown meanwhile.")) : null,
    el("div", { class: "sum" }, drawn(shown.text)),
    el("div", { class: "sumfoot" }, el("span", { class: "acts" }, el("a", { href: `/api/calls/${c.id}/summary.md`, download: "", text: t("save as a file") }), again))));
}

// ---------- the sound

function player(c) {
  if (c.sound.state !== "kept") {
    return el("div", { class: "notice calm", text: c.sound.state === "deleted" ? t("The sound was deleted {date}; the text stays.", { date: short(c.sound.at) }) : t("The sound of this conversation is not ready to be listened to.") });
  }
  const audio = S.audio = el("audio", { src: `/api/calls/${c.id}/audio`, preload: "metadata" });
  const stretch = (side) => el("span", { class: "strip" }, c.speech[side].map(([from, to]) => {
    const mark = el("i");
    mark.style.left = `${(100 * from / c.seconds).toFixed(3)}%`;
    mark.style.width = `${(100 * Math.max(to - from, 0) / c.seconds).toFixed(3)}%`;
    return mark;
  }));
  const line = el("div", { class: "head-line", id: "head-line" });
  let button = null;
  const box = el("section", { class: "panel player" }, audio,
    el("div", { class: "row" },
      (button = el("button", { type: "button", class: "play", id: "play", "aria-label": t("Listen"), text: "▶", onclick: () => (audio.paused ? audio.play() : audio.pause()) })),
      el("span", { class: "time" }, el("span", { id: "t-now", text: clock(0) }), el("span", { class: "dimmer", text: ` / ${clock(c.seconds)}` })),
      el("div", { class: "seg" }, [1, 1.5, 2].map((rate) => el("button", { type: "button", text: `${String(rate).replace(".", ",")}×`, pressed: rate === 1, "data-rate": rate,
        onclick: (e) => { audio.playbackRate = rate; for (const b of e.target.parentNode.children) b.setAttribute("aria-pressed", String(b === e.target)); } }))),
      el("div", { class: "listen" }, t("listen to:"), el("div", { class: "seg", id: "who" }, [["both", N("both")], ["me", N("me")], ["them", N("the others")]].map(([value, label]) =>
        el("button", { type: "button", text: t(label), pressed: value === "both", "data-who": value, onclick: () => listen(value) }))))),
    el("div", { class: "lanes", title: t("A click on a lane goes to that place"), onclick: (e) => {
      const strip = e.currentTarget.querySelector(".strip").getBoundingClientRect();
      seek(Math.max(0, Math.min(1, (e.clientX - strip.left) / strip.width)) * c.seconds);
    } },
      el("div", { class: "lane me" }, el("span", { class: "who", text: t("Me") }), stretch("me")),
      el("div", { class: "lane them" }, el("span", { class: "who", text: t("Others") }), stretch("them")), line),
    el("div", { class: "ticks" }, [0, 0.25, 0.5, 0.75, 1].map((part) => el("span", { text: clock(c.seconds * part) }))));
  // What this sound says of itself is drawn on its own player, and only while that is the one on the screen.
  const mine = (does) => () => { if (S.audio === audio) does(); };
  audio.addEventListener("timeupdate", mine(follow));
  audio.addEventListener("play", mine(() => { button.textContent = "❚❚"; button.setAttribute("aria-label", t("Pause")); }));
  audio.addEventListener("pause", mine(() => { button.textContent = "▶"; button.setAttribute("aria-label", t("Listen")); }));
  audio.addEventListener("error", mine(() => trouble(t("The sound could not be opened."))));
  keys(audio);
  return box;
}

// The "play" and "pause" keys of a keyboard or a headset belong to the player on the screen - and to nothing
// when there is none: left to itself, a browser plays whatever it played last.
function keys(audio) {
  if (!("mediaSession" in navigator)) return;
  const does = { play: () => { audio.play().catch(() => {}); }, pause: () => audio.pause() };
  for (const key of Object.keys(does)) {
    try { navigator.mediaSession.setActionHandler(key, audio ? does[key] : null); } catch (e) { /* a browser that does not know this key */ }
  }
}

function seek(at) {
  if (S.audio) S.audio.currentTime = at;
  follow(at);
  const now = document.querySelector(".remark.current");
  if (now) now.scrollIntoView({ block: "center", behavior: "smooth" });
}

function follow(at) {
  const c = S.call, time = typeof at === "number" ? at : S.audio ? S.audio.currentTime : 0;
  if (!c) return;
  if ($("t-now")) $("t-now").textContent = clock(time);
  if ($("head-line")) $("head-line").style.left = `calc(106px + (100% - 106px) * ${c.seconds ? Math.min(1, time / c.seconds) : 0})`;
  let now = null;
  for (const node of document.querySelectorAll(".remark")) { node.classList.remove("current"); if (Number(node.dataset.at) <= time) now = node; }
  if (now && (S.audio || typeof at === "number")) now.classList.add("current");
}

// One side alone: the sound has the user on the left and the others on the right; the side that is
// asked for is sent into both ears.
function listen(who) {
  S.who = who;
  for (const b of $("who").children) b.setAttribute("aria-pressed", String(b.dataset.who === who));
  if (!S.graph) {
    if (who === "both") return;
    const ctx = new AudioContext(), source = ctx.createMediaElementSource(S.audio), split = ctx.createChannelSplitter(2), merge = ctx.createChannelMerger(2);
    const gain = { ll: ctx.createGain(), lr: ctx.createGain(), rl: ctx.createGain(), rr: ctx.createGain() };
    source.connect(split);
    split.connect(gain.ll, 0); split.connect(gain.lr, 0); split.connect(gain.rl, 1); split.connect(gain.rr, 1);
    gain.ll.connect(merge, 0, 0); gain.rl.connect(merge, 0, 0); gain.lr.connect(merge, 0, 1); gain.rr.connect(merge, 0, 1);
    merge.connect(ctx.destination);
    S.graph = { ctx, gain };
  }
  const { ctx, gain } = S.graph;
  ctx.resume();
  gain.ll.gain.value = who === "them" ? 0 : 1; gain.lr.gain.value = who === "me" ? 1 : 0;
  gain.rl.gain.value = who === "them" ? 1 : 0; gain.rr.gain.value = who === "me" ? 0 : 1;
}

// Leaving a conversation, its sound is not just paused but let go of: a paused player comes back to life at a
// press of "play" on a headset - or when the headset is put on - and would talk with nothing on the screen to stop it by.
function stopSound() {
  const audio = S.audio;
  if (S.graph) S.graph.ctx.close();
  S.audio = S.graph = null;
  if (audio) { audio.pause(); audio.removeAttribute("src"); audio.load(); }
  keys(null);
}

// ---------- what is on the screen, and keeping it true

function route() {
  const found = location.hash.match(/^#\/call\/(\d+)$/);
  S.sure = "";
  S.asking = null;                            // a question belongs to the screen it was asked on
  drawAsk();
  if (!S.lost) trouble("");                   // what went wrong on one screen is not news on the next
  if (found) showCall(Number(found[1])); else showList();
}

async function tick() {
  const answer = await ask("GET", "/api/status");
  if (!answer.ok) {
    S.lost = true;
    trouble(answer.status === 403 ? t("Open the panel from the icon of Protocallist.") : t("Protocallist does not answer"));
    return;
  }
  if (S.lost) { S.lost = false; trouble(""); }
  const before = S.status, s = S.status = answer.data;
  S.lang = document.documentElement.lang = s.language;
  document.title = t("Protocallist");
  const line = (told) => JSON.stringify([told.making, told.queued, told.stopped, told.failures]);       // how the summaries stand
  const changed = !before || before.stamp !== s.stamp || before.gateway !== s.gateway || line(before) !== line(s);
  if (changed) {
    const list = await ask("GET", "/api/calls");
    if (list.ok) S.calls = list.data.calls;
    else S.status = Object.assign({}, s, { stamp: null });       // not read: it is asked for again at the next look
    if (!S.call) look(0);
    if (S.call) await refreshCall(); else if ($("programs") && programsNow().join("\n") !== S.programs) showList(); else drawRows();
  }
  if (S.asking && !stood()) drawAsk();          // the line that stood was started again or forgotten elsewhere
  drawTop();
}

function theme(name, chosen) {
  document.documentElement.dataset.theme = name;
  if (chosen) localStorage.setItem("theme", name);        // the system's own is followed until the user picks one
}

async function main() {
  theme(localStorage.getItem("theme") || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"));
  S.texts = (await ask("GET", "/texts.json")).data;
  $("theme").addEventListener("click", () => { theme(document.documentElement.dataset.theme === "dark" ? "light" : "dark", true); drawTop(); });
  window.addEventListener("hashchange", route);
  document.addEventListener("keydown", (e) => {
    if (!S.asking) return;
    if (e.key === "Escape") { S.asking = null; drawAsk(); return; }
    if (e.key !== "Tab") return;                 // the keyboard stays inside the question while it is asked
    const inside = [...$("asking").querySelectorAll("button")], at = inside.indexOf(document.activeElement);
    e.preventDefault();
    inside[(at + (e.shiftKey ? inside.length - 1 : 1)) % inside.length].focus();
  });
  $("asking").addEventListener("click", (e) => { if (e.target === $("asking")) { S.asking = null; drawAsk(); } });
  await tick();
  route();
  setInterval(tick, 2000);
}

main();
