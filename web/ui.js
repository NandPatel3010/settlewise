"use strict";

const $ = (id) => document.getElementById(id);
const content = $("content");
const dialog = $("dialog");
const dialogBody = $("dialog-body");
const STATUS = {matched: "Reconciled", review: "Needs review", waiting: "Awaiting deposit"};
const VIEW = {overview: "Overview", review: "Review queue", imports: "Imports"};
const PAGE = 8;

let workspace = "demo";
let currency = "CAD";
let view = "overview";
let ledger;
let filter = "all";
let search = "";
let page = 0;
let toastTimer;
let lastFocus;

const encode = (value) => String(value ?? "").replace(/[&<>"']/g, (character) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
})[character]);
const money = (cents, code = currency) => new Intl.NumberFormat("en-CA", {
  style: "currency", currency: code, currencyDisplay: "narrowSymbol",
}).format(cents / 100);
const day = (value) => new Intl.DateTimeFormat("en-CA", {
  dateStyle: "medium", timeZone: "UTC",
}).format(new Date(`${value}T00:00:00Z`));
const time = (value) => new Date(value).toLocaleString("en-CA", {dateStyle: "medium", timeStyle: "short"});
const badge = (status) => `<span class="badge ${status}">${STATUS[status]}</span>`;

async function api(route, body, target = workspace) {
  const options = body === undefined ? {} : {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body),
  };
  const response = await fetch(`/api/${route}?workspace=${target}`, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "The request failed.");
  return data;
}

async function refresh() {
  const requested = workspace;
  content.setAttribute("aria-busy", "true");
  try {
    const data = await api("state", undefined, requested);
    if (workspace !== requested) return;
    ledger = data;
    render();
  } catch (error) {
    if (workspace !== requested) return;
    ledger = undefined;
    content.innerHTML = `<section class="panel empty">Could not load the ledger. ${encode(error.message)}
      <button class="button small" data-action="refresh">Retry</button></section>`;
  } finally {
    if (workspace === requested) content.setAttribute("aria-busy", "false");
  }
}

function notify(message) {
  $("toast").textContent = message;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { $("toast").textContent = ""; }, 5200);
}

function rowsForCurrency() {
  return ledger.payouts.filter((row) => row.payout.currency === currency);
}

function bankForCurrency() {
  return ledger.unmatched.filter((row) => row.currency === currency);
}

function switchView(next) {
  view = next;
  filter = "all";
  search = "";
  page = 0;
  render();
}

function hero(title, description) {
  return `<header class="hero"><h1>${title}</h1><p>${description}</p></header>`;
}

function metric(label, value, foot) {
  return `<article class="card metric"><h2>${label}</h2><strong>${value}</strong><small>${foot}</small></article>`;
}

function render() {
  document.querySelectorAll("[data-view]").forEach((button) => {
    const active = button.dataset.view === view;
    button.classList.toggle("active", active);
    if (active) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
  $("crumb").textContent = VIEW[view];
  if (!ledger) return;
  const all = rowsForCurrency();
  const review = all.filter((row) => row.status === "review");
  const pending = bankForCurrency();
  $("review-count").textContent = review.length + pending.length;

  if (view === "imports") {
    content.innerHTML = hero("Import history", "Review what came into the ledger and what was skipped on retry.") +
      importsView();
    return;
  }

  if (view === "review") {
    content.innerHTML = hero("Review queue", "Resolve payouts with more than one possible match and inspect deposits without a payout.") +
      panelForPayouts(review, false, "Payouts needing review") + bankTable(pending) + decisionTable();
    return;
  }

  const matched = all.filter((row) => row.status === "matched");
  const matchedMoney = matched.reduce((sum, row) => sum + row.payout.amount, 0);
  const waiting = all.filter((row) => row.status === "waiting");
  const lateDeposit = pending.some((row) => row.id === "BK-1031");
  content.innerHTML = hero("Payout overview", "See what reached the bank, how each payout adds up, and what still needs a closer look.") +
    `<section class="summary" aria-label="Ledger totals">
      ${metric("Reconciled deposits", money(matchedMoney), `${matched.length} matched payout${matched.length === 1 ? "" : "s"} · ${currency}`)}
      ${metric("Awaiting deposit", String(waiting.length), "Payouts without a bank match")}
      ${metric("Needs review", String(review.length), "Ambiguities or amount differences")}
      ${metric("Unmatched deposits", String(pending.length), "Bank rows without a match")}
    </section>
    <div class="insights">
      <section class="panel chart">
        <div class="section-head"><h2>Settlement activity</h2><div class="legend"><span>Matched</span><span>Open</span></div></div>
        <canvas id="chart" role="img" aria-label="Payout amounts by day for the latest seven payout dates"></canvas>
      </section>
      ${workspace === "demo" && currency === "CAD" ? `<section class="panel scenario">
        <span class="eyebrow">Sample scenario</span>
        <h2>${lateDeposit ? "A payout report arrived late." : "The late deposit is reconciled."}</h2>
        <p>${lateDeposit ? "Bank deposit BK-1031 is already in the ledger. Import the missing payout to see it match." : "The imported payout explains the $328.65 deposit. Retry the file to verify that the ledger stays the same."}</p>
        <button class="button small" data-action="late">${lateDeposit ? "Import late payout" : "Retry late payout"} <span aria-hidden="true">→</span></button>
      </section>` : `<section class="panel scenario">
        <span class="eyebrow">${encode(currency)} ledger</span><h2>Follow each deposit.</h2>
        <p>Import a statement and a payout report. Open a payout to view its sales, refunds, fees and adjustments.</p>
        <button class="button small" data-action="import">Import a file <span aria-hidden="true">→</span></button>
      </section>`}
    </div>` + panelForPayouts(all, true, "Payout ledger");

  drawChart(all);
}

function panelForPayouts(rows, tools, title) {
  const toolbar = tools ? `<div class="toolbar">
    <label class="sr-only" for="search">Find a payout or bank reference</label>
    <input id="search" type="search" placeholder="Search payout or bank ID" value="${encode(search)}">
    <label class="sr-only" for="status-filter">Filter by status</label>
    <select id="status-filter"><option value="all">All statuses</option>
    ${Object.entries(STATUS).map(([key, value]) => `<option value="${key}"${filter === key ? " selected" : ""}>${value}</option>`).join("")}</select>
  </div>` : "";
  return `<section class="panel ledger">
    <div class="section-head"><div><h2>${title}</h2><small>${rows.length} payout${rows.length === 1 ? "" : "s"} · ${currency}</small></div>
    ${tools ? '<button class="button small" data-action="export">Export CSV</button>' : ""}</div>
    ${toolbar}<div id="payout-rows">${payoutRows(rows, tools)}</div>
  </section>`;
}

function payoutRows(rows, useFilters) {
  const query = search.trim().toLowerCase();
  const filtered = useFilters ? rows.filter((row) =>
    (filter === "all" || filter === row.status) &&
    (!query || [row.payout.id, row.bank?.id, ...row.candidates.map((bank) => bank.id)]
      .some((value) => value?.toLowerCase().includes(query)))) : rows;
  if (!filtered.length) return `<p class="empty">${useFilters ? "No payouts match this view." : "No payouts need manual review."}</p>`;
  const pages = Math.ceil(filtered.length / PAGE);
  const current = Math.min(page, pages - 1);
  const start = useFilters ? current * PAGE : 0;
  const items = useFilters ? filtered.slice(start, start + PAGE) : filtered;
  return `<div class="table-wrap"><table><thead><tr><th>Payout</th><th>Expected</th><th>Net amount</th><th>Bank deposit</th><th>Status</th></tr></thead>
    <tbody>${items.map(({payout, bank, status, method}) => `<tr>
      <td><button class="link-button" data-payout="${encode(payout.id)}">${encode(payout.id)} <span aria-hidden="true">↗</span></button>
      ${method ? `<small>${{reference: "Reference match", manual: "Manually confirmed"}[method]}</small>` : ""}</td>
      <td>${day(payout.date)}</td><td class="amount">${money(payout.amount)}</td>
      <td>${bank ? `${encode(bank.id)}<small>${day(bank.date)}</small>` : "—"}</td><td>${badge(status)}</td>
    </tr>`).join("")}</tbody></table></div>
    ${useFilters ? `<div class="page-controls"><small>${start + 1}–${start + items.length} of ${filtered.length}</small>
      <div class="actions"><button class="button small" data-action="previous" ${!current ? "disabled" : ""}>Previous</button>
      <button class="button small" data-action="next" ${current >= pages - 1 ? "disabled" : ""}>Next</button></div></div>` : ""}`;
}

function bankTable(rows) {
  return `<section class="panel ledger"><header class="section-head"><h2>Unmatched bank deposits</h2></header>
    ${rows.length ? `<div class="table-wrap"><table><thead><tr><th>Bank ID</th><th>Posted</th><th>Amount</th><th>Payout reference</th></tr></thead><tbody>
      ${rows.map((bank) => `<tr><td>${encode(bank.id)}<small>${encode(bank.description)}</small></td>
      <td>${day(bank.date)}</td><td class="amount">${money(bank.amount)}</td><td>${encode(bank.reference || "—")}</td></tr>`).join("")}
    </tbody></table></div>` : '<p class="empty">No unmatched bank deposits in this currency.</p>'}</section>`;
}

function decisionTable() {
  const decisions = ledger.decisions.filter((row) =>
    ledger.payouts.some(({payout}) => payout.id === row.payout_id && payout.currency === currency));
  if (!decisions.length) return "";
  return `<section class="panel ledger"><header class="section-head"><h2>Review history</h2></header>
    <div class="table-wrap"><table><thead><tr><th>Payout</th><th>Bank ID</th><th>Action</th><th>Recorded at</th></tr></thead><tbody>
      ${decisions.map((row) => `<tr><td>${encode(row.payout_id)}</td><td>${encode(row.bank_id)}</td><td>${row.action === "reopened" ? "Reopened" : "Confirmed"}</td><td>${time(row.occurred_at)}</td></tr>`).join("")}
    </tbody></table></div></section>`;
}

function importsView() {
  return `<div class="templates">
      <a class="button small" href="/samples/demo-bank.csv" download>Bank CSV example ↓</a>
      <a class="button small" href="/samples/demo-payouts.csv" download>Payout CSV example ↓</a>
    </div>
    <section class="panel ledger">
    <header class="section-head"><div><h2>Import log</h2><small>Latest 100 committed batches</small></div></header>
    ${ledger.imports.length ? `<div class="table-wrap"><table><thead><tr><th>File</th><th>Imported at</th><th>Rows added</th><th>Duplicates skipped</th></tr></thead><tbody>
      ${ledger.imports.map((batch) => `<tr><td>${encode(batch.name)}<small>${batch.kind === "bank" ? "Bank statement" : "Payout report"} · Batch ${batch.id}</small></td>
      <td>${time(batch.created_at)}</td><td>${batch.added} of ${batch.total}</td><td>${batch.duplicates}</td></tr>`).join("")}
    </tbody></table></div>` : '<p class="empty">Import a CSV to get started.</p>'}</section>`;
}

function drawChart(rows = ledger ? rowsForCurrency() : []) {
  const canvas = $("chart");
  if (!canvas) return;
  const rect = canvas.getBoundingClientRect();
  if (!rect.width || !rect.height) return;
  const scale = window.devicePixelRatio || 1;
  canvas.width = rect.width * scale;
  canvas.height = rect.height * scale;
  const ctx = canvas.getContext("2d");
  ctx.scale(scale, scale);
  const groups = new Map();
  for (const row of rows) {
    if (!groups.has(row.payout.date)) groups.set(row.payout.date, [0, 0]);
    groups.get(row.payout.date)[row.status === "matched" ? 0 : 1] += row.payout.amount;
  }
  const recent = [...groups].slice(0, 7).reverse();
  const ceiling = Math.max(1, ...recent.flatMap((entry) => entry[1]));
  const width = rect.width;
  const height = rect.height;
  const x0 = 6;
  const base = height - 25;
  ctx.strokeStyle = "#dce6e1";
  for (const y of [base, base * .65, base * .3]) {
    ctx.beginPath();
    ctx.moveTo(x0, y);
    ctx.lineTo(width - x0, y);
    ctx.stroke();
  }
  ctx.font = "11px system-ui";
  ctx.textAlign = "center";
  if (!recent.length) {
    ctx.fillStyle = "#50645e";
    ctx.fillText("No payout activity in this currency.", width / 2, height / 2);
    return;
  }
  canvas.setAttribute("aria-label", "Recent payouts by day. " + recent.map(
    ([date, amounts]) => `${day(date)}: ${money(amounts[0])} matched, ${money(amounts[1])} open`,
  ).join(". "));
  const slot = (width - x0 * 2) / recent.length;
  const bar = Math.min(slot / 3, 21);
  recent.forEach(([date, amounts], i) => {
    const center = x0 + slot * (i + 0.5);
    amounts.forEach((value, j) => {
      const barHeight = value / ceiling * (base - 8);
      ctx.fillStyle = j === 0 ? "#155d4a" : "#a8bdb4";
      ctx.fillRect(center + (j ? 2 : -bar - 2), base - barHeight, bar, barHeight);
    });
    ctx.fillStyle = "#50645e";
    ctx.fillText(date.slice(5).replace("-", "/"), center, height - 6);
  });
}

function openDialog(eyebrow, title, html) {
  lastFocus = document.activeElement;
  $("dialog-eyebrow").textContent = eyebrow;
  $("dialog-title").textContent = title;
  dialogBody.innerHTML = html;
  if (!dialog.open) dialog.showModal();
}

function payoutDialog(row) {
  const {payout, bank, status, candidates} = row;
  const lines = [
    ["Card sales", payout.sales], ["Refunds", -payout.refunds], ["Processing fees", -payout.fees],
    ["Adjustments", payout.adjustments], ["Expected bank deposit", payout.amount],
  ];
  openDialog("Payout details", payout.id, `
    <div class="between">${badge(status)}<small>${day(payout.date)} · ${encode(payout.currency)}</small></div>
    <dl class="breakdown">${lines.map(([label, amount]) => `<dt>${label}</dt><dd>${money(amount)}</dd>`).join("")}</dl>
    <p class="notice">${encode(row.reason)}</p>
    ${bank ? `<div class="candidate"><div><b>${encode(bank.id)}</b><small>Posted ${day(bank.date)}</small></div><b>${money(bank.amount)}</b></div>` : ""}
    ${row.method === "manual" ? `<div class="actions"><button class="button small" data-reopen="${encode(bank.id)}" data-target="${encode(payout.id)}">Undo confirmation</button></div>` : ""}
    ${candidates.map((item) => `<div class="candidate"><div><b>${encode(item.id)}</b><small>${day(item.date)} · ${money(item.amount, item.currency)} ${encode(item.currency)}</small>
      ${item.description ? `<small>${encode(item.description)}</small>` : ""}</div>
      ${item.amount === payout.amount && item.currency === payout.currency ? `<button class="button small" data-confirm="${encode(item.id)}" data-target="${encode(payout.id)}">Confirm match</button>` : '<small>Amount or currency differs</small>'}</div>`).join("")}
    ${row.candidate_count > candidates.length ? `<form class="form candidate" data-review-form="${encode(payout.id)}">
      <label for="candidate-id">Showing ${candidates.length} of ${row.candidate_count} candidates. Confirm another by bank ID.</label>
      <input id="candidate-id" maxlength="64" required>
      <button class="button small" type="submit">Confirm by ID</button>
    </form>` : ""}
    <p class="notice error" id="review-error" role="alert"></p>`);
}

function openImport() {
  openDialog("CSV intake", "Import a file", `
    <form class="form" id="import-form">
      <p class="muted">Preview a file before it changes the ${workspace === "demo" ? "sample" : "local"} ledger.</p>
      <label for="kind">File type</label>
      <select id="kind"><option value="bank">Bank statement</option><option value="payouts">Payout report</option></select>
      <label for="file">CSV file</label><input type="file" id="file" accept=".csv,text/csv" required>
      <a id="template" href="/samples/demo-bank.csv" download>Download a bank CSV example</a>
      <div class="notice" id="import-summary" role="status"></div>
      <div id="import-preview"></div>
      <div class="notice error" id="import-error" role="alert"></div>
      <div class="actions"><button class="button" type="submit" id="preview">Validate CSV</button>
        <button class="button primary" type="button" id="commit" disabled>Import rows</button></div>
    </form>`);

  let prepared;
  const invalidated = () => {
    prepared = undefined;
    $("commit").disabled = true;
    $("import-summary").textContent = "";
    $("import-preview").textContent = "";
    $("import-error").textContent = "";
  };

  $("file").addEventListener("change", invalidated);
  $("kind").addEventListener("change", () => {
    invalidated();
    const bank = $("kind").value === "bank";
    $("template").href = `/samples/${bank ? "demo-bank.csv" : "demo-payouts.csv"}`;
    $("template").textContent = `Download a ${bank ? "bank" : "payout"} CSV example`;
  });

  $("import-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    invalidated();
    const input = $("file");
    const kind = $("kind");
    const preview = $("preview");
    const commit = $("commit");
    const errorBox = $("import-error");
    const summary = $("import-summary");
    const previewTable = $("import-preview");
    const file = input.files[0];
    preview.disabled = true;
    input.disabled = true;
    kind.disabled = true;
    try {
      if (file.size > 1_800_000) throw new Error("Choose a CSV smaller than 1.8 MB.");
      const request = {kind: kind.value, name: file.name, content: await file.text()};
      const result = await api("preview", request);
      prepared = request;
      summary.textContent = `${result.total} valid rows · ${result.added} new · ${result.duplicates} already in ledger or repeated`;
      previewTable.innerHTML = `<div class="table-wrap"><table><thead><tr><th>ID</th><th>Date</th><th>Currency</th><th>Amount</th></tr></thead><tbody>
        ${result.preview.map((row) => `<tr><td>${encode(row.id)}</td><td>${day(row.date)}</td><td>${encode(row.currency)}</td><td>${money(row.amount, row.currency)}</td></tr>`).join("")}
      </tbody></table></div>`;
      commit.disabled = false;
    } catch (error) {
      errorBox.textContent = error.message;
    } finally {
      preview.disabled = false;
      input.disabled = false;
      kind.disabled = false;
    }
  });

  $("commit").addEventListener("click", async () => {
    if (!prepared) return;
    const targets = ["commit", "preview", "file", "kind"].map($);
    const errorBox = $("import-error");
    targets.forEach((control) => { control.disabled = true; });
    errorBox.textContent = "";
    try {
      const result = await api("import", prepared);
      if (dialog.open && dialogBody.contains(errorBox)) dialog.close();
      notify(`Imported ${result.added} new rows. Skipped ${result.duplicates} duplicates.`);
      await refresh();
    } catch (error) {
      errorBox.textContent = error.message;
    } finally {
      targets.forEach((control) => { control.disabled = false; });
    }
  });
}

function exportReport() {
  const expected = ["payout_id,expected_on,currency,net_amount,status,bank_id,bank_amount,method"];
  for (const row of rowsForCurrency()) {
    const p = row.payout;
    expected.push([
      p.id, p.date, p.currency, (p.amount / 100).toFixed(2), row.status,
      row.bank?.id ?? "", row.bank ? (row.bank.amount / 100).toFixed(2) : "", row.method ?? "",
    ].join(","));
  }
  const url = URL.createObjectURL(new Blob([expected.join("\r\n") + "\r\n"], {type: "text/csv"}));
  const link = document.createElement("a");
  link.href = url;
  link.download = `reconciliation-${workspace}-${currency.toLowerCase()}.csv`;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

document.querySelectorAll("[data-view]").forEach((button) => {
  button.addEventListener("click", () => switchView(button.dataset.view));
});
$("workspace").addEventListener("change", (event) => {
  workspace = event.target.value;
  ledger = undefined;
  filter = "all";
  search = "";
  page = 0;
  $("review-count").textContent = "0";
  content.innerHTML = '<section class="empty">Opening the local ledger…</section>';
  refresh();
});
$("currency").addEventListener("change", (event) => {
  currency = event.target.value;
  page = 0;
  render();
});
$("open-import").addEventListener("click", openImport);
$("close-dialog").addEventListener("click", () => dialog.close());
dialog.addEventListener("close", () => {
  if (lastFocus?.isConnected) lastFocus.focus();
});
async function updateReview(button, payout, bank, reopen) {
  const errorBox = $("review-error");
  button.disabled = true;
  try {
    await api(reopen ? "reopen" : "confirm", {payout_id: payout, bank_id: bank});
    if (dialog.open && dialogBody.contains(errorBox)) dialog.close();
    notify(reopen ? "Confirmation reopened and recorded." : "Match confirmed and recorded.");
    await refresh();
  } catch (error) {
    errorBox.textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

dialogBody.addEventListener("click", (event) => {
  const button = event.target.closest("[data-confirm], [data-reopen]");
  if (button) {
    updateReview(button, button.dataset.target, button.dataset.reopen || button.dataset.confirm, !!button.dataset.reopen);
  }
});
dialogBody.addEventListener("submit", (event) => {
  const form = event.target.closest("[data-review-form]");
  if (!form) return;
  event.preventDefault();
  updateReview(form.querySelector("button"), form.dataset.reviewForm, $("candidate-id").value.trim(), false);
});

function refreshRows() {
  const table = $("payout-rows");
  if (table && ledger) table.innerHTML = payoutRows(rowsForCurrency(), true);
}

content.addEventListener("input", (event) => {
  if (event.target.id !== "search") return;
  search = event.target.value;
  page = 0;
  refreshRows();
});
content.addEventListener("change", (event) => {
  if (event.target.id !== "status-filter") return;
  filter = event.target.value;
  page = 0;
  refreshRows();
});
content.addEventListener("click", async (event) => {
  const button = event.target.closest("button");
  if (!button) return;
  if (button.dataset.payout) {
    const row = rowsForCurrency().find((item) => item.payout.id === button.dataset.payout);
    if (row) payoutDialog(row);
    return;
  }
  const action = button.dataset.action;
  if (action === "import") openImport();
  if (action === "refresh") await refresh();
  if (action === "export") exportReport();
  if (action === "next" || action === "previous") {
    page += action === "next" ? 1 : -1;
    refreshRows();
    content.querySelector(`[data-action="${action}"]:not(:disabled)`)?.focus();
  }
  if (action === "late") {
    button.disabled = true;
    try {
      const result = await api("demo-late", {});
      notify(result.added ? "Late payout imported and reconciled." : "Duplicate skipped. Ledger totals are unchanged.");
      await refresh();
    } catch (error) {
      notify(error.message);
    } finally {
      button.disabled = false;
    }
  }
});
window.addEventListener("resize", () => drawChart());
refresh();
