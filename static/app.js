/* Kavita Age-Rater: review table, filters, commit/skip and rescan.
   Theme control and the "Updated" line come from gomez-ui.js. */
(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };
  var STATE = { series: [], ratings: [], counts: {}, scan: {} };
  var statusFilter = "pending", searchVal = "", minConf = 0;
  var scanPoll = null, pendingCommit = null;

  function api(url, body) {
    return fetch(url, body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : undefined)
      .then(function (r) { return r.json(); });
  }
  function flash(msg) {
    var f = $("flash"); f.textContent = msg; f.classList.add("show");
    clearTimeout(f._t); f._t = setTimeout(function () { f.classList.remove("show"); }, 2600);
  }
  function say(msg, bad) {
    var n = $("notice"); n.textContent = msg || ""; n.hidden = !msg; n.classList.toggle("bad", !!bad);
  }
  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"]/g, function (m) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[m]; }); }
  function confClass(c) { return c >= 0.8 ? "hi" : (c >= 0.6 ? "mid" : "lo"); }

  function load() {
    var qs = new URLSearchParams({ status: statusFilter, search: searchVal });
    return fetch("/api/state?" + qs).then(function (r) { return r.json(); }).then(function (s) { STATE = s; render(); });
  }

  function render() {
    var c = STATE.counts || {};
    $("counts").innerHTML =
      '<span>Total <b>' + (c.total || 0) + '</b></span>' +
      '<span class="c-pending">Pending <b>' + (c.pending || 0) + '</b></span>' +
      '<span class="c-committed">Committed <b>' + (c.committed || 0) + '</b></span>' +
      '<span>Skipped <b>' + (c.skipped || 0) + '</b></span>';

    // Scan state: progress and Kavita problems go in the notice; the Updated line shows the last good scan.
    var s = STATE.scan || {};
    if (s.running) say("Scanning the library… " + (s.progress || ""));
    else if (STATE.kavita_error) say("Can't reach Kavita: " + STATE.kavita_error + ". Check KAVITA_URL and KAVITA_API_KEY.", true);
    else say("");
    $("rescanBtn").disabled = !!s.running;
    $("rescanBtn").textContent = s.running ? "Scanning…" : "Rescan now";
    var upd = $("updated");
    if (upd && window.gomezUI) {
      window.gomezUI.setUpdated(upd, STATE.last_success_iso || "",
        STATE.last_scan_ok === false && s.last_error ? "The last scan didn't work: " + s.last_error : null);
    }

    var rows = (STATE.series || []).filter(function (r) { return (r.confidence || 0) >= minConf || r.status !== "pending"; });
    var tb = $("rows"); tb.innerHTML = "";
    $("empty").hidden = rows.length > 0;
    function opts(sel) {
      return [""].concat(STATE.ratings || []).map(function (r) {
        return '<option ' + (r === sel ? "selected " : "") + 'value="' + esc(r) + '">' + (r ? esc(r) : "Use proposed") + "</option>";
      }).join("");
    }
    rows.forEach(function (r) {
      var eff = r.override_rating || r.proposed_rating || "";
      var conf = r.confidence || 0;
      var tr = document.createElement("tr");
      tr.innerHTML =
        '<td class="pick"><input type="checkbox" class="sel" data-id="' + r.series_id + '" aria-label="Select ' + esc(r.name) + '"></td>' +
        '<td class="book"><b>' + esc(r.name) + "</b><small>" + (esc(r.authors) || "–") + "</small></td>" +
        '<td class="prop" data-label="Proposed">' + (esc(r.proposed_rating) || "–") + (r.override_rating && r.override_rating !== r.proposed_rating ? '<small class="sub">overridden</small>' : "") + "</td>" +
        '<td class="num cf"><span class="conf ' + confClass(conf) + '">' + conf.toFixed(2) + "</span></td>" +
        '<td class="apply"><select data-id="' + r.series_id + '" aria-label="Rating to apply for ' + esc(r.name) + '">' + opts(eff) + "</select></td>" +
        '<td class="st"><span class="badge b-' + esc(r.status) + '">' + esc(r.status) + "</span>" +
          (r.status === "committed" && r.committed_rating ? '<small class="sub">' + esc(r.committed_rating) + "</small>" : "") + "</td>" +
        '<td class="why">' + esc(r.reasoning) + "</td>" +
        "<td class=\"act\">" + (r.status === "committed" ? "" : '<button class="btn btn-small" type="button" data-commit="' + r.series_id + '">Commit</button>') + "</td>";
      tb.appendChild(tr);
    });
    $("selAll").checked = false;
  }

  function selectedIds() { return Array.prototype.map.call(document.querySelectorAll(".sel:checked"), function (x) { return +x.dataset.id; }); }

  // ---- commit, with an in-page confirm step (no browser pop-up)
  function askCommit(ids, allPending) {
    if (!allPending && ids.length === 0) { flash("Select some rows first."); return; }
    var n = allPending ? (STATE.counts.pending || 0) : ids.length;
    if (n === 0) { flash("Nothing pending to commit."); return; }
    pendingCommit = { ids: ids, allPending: allPending };
    $("confirmText").textContent = "Commit " + n + " rating" + (n === 1 ? "" : "s") + " to Kavita? This locks them, so a library scan won't change them.";
    $("confirmYes").textContent = "Commit " + n;
    $("confirm").hidden = false;
    $("confirmYes").focus();
  }
  function closeConfirm() { $("confirm").hidden = true; pendingCommit = null; }
  $("confirmNo").addEventListener("click", closeConfirm);
  $("confirmYes").addEventListener("click", function () {
    var p = pendingCommit; closeConfirm(); if (!p) return;
    flash("Committing…");
    api("/api/commit", p.allPending ? { all_pending: true } : { ids: p.ids }).then(function (res) {
      if (!res.ok) { flash("Couldn't commit: " + res.error); return; }
      flash(res.committed + " committed, " + res.skipped + " skipped, " + res.failed + " failed");
      return load();
    });
  });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape" && !$("confirm").hidden) closeConfirm(); });

  $("commitSelBtn").addEventListener("click", function () { askCommit(selectedIds(), false); });
  $("commitAllBtn").addEventListener("click", function () { askCommit([], true); });
  $("rows").addEventListener("click", function (e) {
    var b = e.target.closest("[data-commit]"); if (b) askCommit([+b.dataset.commit], false);
  });
  $("rows").addEventListener("change", function (e) {
    if (e.target.tagName === "SELECT") api("/api/override", { series_id: +e.target.dataset.id, override: e.target.value });
  });
  $("skipBtn").addEventListener("click", function () {
    var ids = selectedIds(); if (!ids.length) { flash("Select some rows first."); return; }
    api("/api/skip", { ids: ids }).then(function () { flash("Skipped " + ids.length); return load(); });
  });
  $("unskipBtn").addEventListener("click", function () {
    var ids = selectedIds(); if (!ids.length) { flash("Select some rows first."); return; }
    api("/api/unskip", { ids: ids }).then(function () { flash("Reset " + ids.length + " to pending"); return load(); });
  });
  $("selAll").addEventListener("change", function (e) {
    document.querySelectorAll(".sel").forEach(function (x) { x.checked = e.target.checked; });
  });

  // ---- rescan
  $("rescanBtn").addEventListener("click", function () {
    api("/api/scan", {}).then(function (res) {
      if (!res.ok) { flash(res.error || "Couldn't start a scan."); return; }
      flash("Scan started");
      if (scanPoll) clearInterval(scanPoll);
      scanPoll = setInterval(function () {
        load().then(function () {
          if (!STATE.scan.running) { clearInterval(scanPoll); scanPoll = null; flash("Scan finished"); }
        });
      }, 2500);
      load();
    });
  });

  // ---- filters
  $("statusSeg").addEventListener("click", function (e) {
    var b = e.target.closest("button"); if (!b) return;
    statusFilter = b.dataset.status;
    this.querySelectorAll("button").forEach(function (x) { x.setAttribute("aria-checked", x === b ? "true" : "false"); });
    load();
  });
  var searchTimer;
  $("search").addEventListener("input", function (e) {
    clearTimeout(searchTimer); searchVal = e.target.value; searchTimer = setTimeout(load, 250);
  });
  $("conf").addEventListener("input", function (e) {
    minConf = +e.target.value; $("confVal").textContent = minConf.toFixed(2); render();
  });

  load();
  setInterval(function () { if (!document.querySelector("select:focus") && !document.querySelector(".sel:checked") && $("confirm").hidden) load(); }, 15000);
})();
