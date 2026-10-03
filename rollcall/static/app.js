/* Roll Call - vanilla JS, no framework, no CDN.
   The server renders the whole state into #boot, this file only draws it. */
(function () {
  "use strict";
  var boot = JSON.parse(document.getElementById("boot").textContent);
  var STATE = boot;
  var VIEW = boot.view || "session";
  var app = document.getElementById("app");
  var skewMs = 0;
  var lastResult = null;
  var previewKind = boot.preview || "attendance.csv";
  var REASONS = {
    accepted: { cls: "ok", title: "Check-in recorded" },
    ok: { cls: "ok", title: "Check-in recorded" },
    duplicate: { cls: "warn", title: "Already checked in" },
    rejected: { cls: "bad", title: "Check-in refused" }
  };

  /* ---------------------------------------------------------------- helpers */
  function esc(v) {
    return String(v === null || v === undefined ? "" : v).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function p2(n) { return (n < 10 ? "0" : "") + n; }
  function clockTime(iso) {
    if (!iso) { return "—"; }
    var d = new Date(iso);
    return p2(d.getHours()) + ":" + p2(d.getMinutes()) + ":" + p2(d.getSeconds());
  }
  function shortStamp(iso) {
    if (!iso) { return "—"; }
    var d = new Date(iso);
    return (d.getMonth() + 1) + "/" + d.getDate() + " " + p2(d.getHours()) + ":" + p2(d.getMinutes()) + ":" + p2(d.getSeconds());
  }
  function secsLeft() {
    if (!STATE.code) { return 0; }
    return Math.max(0, Math.round((STATE.code.expires_at_ms - (Date.now() - skewMs)) / 1000));
  }
  function tile(k, v, small) {
    return '<div class="tile"><span class="k">' + esc(k) + '</span><span class="v' + (small ? " small" : "") + '">' + esc(v) + "</span></div>";
  }
  function badge(text, kind) { return '<span class="badge ' + kind + '">' + esc(text) + "</span>"; }
  function sessionTitle() {
    var s = STATE.session;
    return s ? (s.label + (s.location ? " · " + s.location : "")) : "no session open yet";
  }
  function api(path, options) {
    return fetch(path, options).then(function (r) {
      return r.json().then(function (body) {
        if (!r.ok) { throw new Error(body.message || body.error || ("HTTP " + r.status)); }
        return body;
      });
    });
  }
  function receiptFromState() {
    /* /check-in?just=<ref> renders the flagged participant's newest entry from
       the server state, so a bookmark or a screenshot shows a real receipt. */
    if (!boot.just) { return null; }
    var row = (STATE.recent || []).filter(function (r) {
      return r.participant_ref === boot.just;
    })[0];
    if (!row) { return null; }
    return {
      status: row.kind === "duplicate" ? "duplicate" : "accepted",
      attendance_id: row.id, seq: row.seq, kind: row.kind,
      entry_hash: row.entry_hash, prev_hash: row.prev_hash,
      participant: { ref: row.participant_ref, name: row.participant_name },
      message: "check-in recorded at " + clockTime(row.checked_in_at) + " with code " +
        row.code_used + " (rotation step " + row.code_step + ")"
    };
  }
  function applyState(blob) {
    STATE = blob;
    skewMs = Date.now() - STATE.server_ms;
  }

  /* ---------------------------------------------------------------- views */
  var views = {};

  views.session = function () {
    var s = STATE.session, code = STATE.code;
    var html = '<div class="grid two"><section class="card">';
    if (s && code) {
      html += '<div class="cardhead"><h2>Live check-in code</h2>' +
        '<span class="pill ok">rotating every ' + code.step_seconds + "s</span></div>" +
        '<div class="codebox"><div class="code" id="bigcode">' + esc(code.current) + "</div>" +
        '<div class="code-meta" id="codemeta">rotation step ' + code.step + " · rotates in <b>" +
        secsLeft() + "</b>s · session #" + s.id + "</div>" +
        '<div class="bar"><i id="barfill" style="width:' + barWidth() + '%"></i></div>' +
        '<p class="hint">Read this out, or show this screen. A code is only valid for its own rotation step, ' +
        "plus one step of grace for slow typists - older codes are refused.</p></div>" +
        '<table><tbody>' +
        "<tr><th>Session</th><td>" + esc(s.label) + "</td></tr>" +
        "<tr><th>Location</th><td>" + esc(s.location || "—") + "</td></tr>" +
        "<tr><th>Window</th><td>" + esc(shortStamp(s.opens_at)) + " &rarr; " + esc(shortStamp(s.closes_at)) + "</td></tr>" +
        "<tr><th>Accepted entries</th><td>" + esc(countEntries(s.id)) + "</td></tr>" +
        "<tr><th>Previous code</th><td class=\"mono muted\">" + esc(code.previous) + " (grace window)</td></tr>" +
        "</tbody></table>" +
        '<div class="row" style="margin-top:14px"><button class="ghost" data-action="close-session">Close this session</button>' +
        '<a class="mono" style="align-self:center" href="/check-in">open the participant page &rarr;</a></div>';
    } else {
      html += "<h2>No session yet</h2><p class=\"hint\">Open a session on the right, then hand the participant page " +
        'to the room: <a href="/check-in">/check-in</a>.</p>';
    }
    html += "</section><div class=\"grid\">";
    html += '<section class="card"><h2>At a glance</h2><div class="grid three">' +
      tile("people", STATE.totals.participants) +
      tile("entries", STATE.totals.entries) +
      tile("sessions", STATE.totals.sessions) +
      "</div><div class=\"grid three\" style=\"margin-top:12px\">" +
      tile("flags", STATE.anomaly_counts.total) +
      tile("chain links", STATE.chain.checked) +
      tile("chain", STATE.chain.ok ? "OK" : "BROKEN", true) +
      "</div></section>";
    html += '<section class="card"><h2>Open a session</h2>' +
      '<form data-action="new-session">' +
      '<label for="f-label">Label</label><input id="f-label" type="text" name="label" value="Week 6 - Soldering Clinic" required>' +
      '<label for="f-loc">Location</label><input id="f-loc" type="text" name="location" value="Room 204 (Main Lab)">' +
      '<div class="row"><div><label for="f-min">Minutes</label><input id="f-min" type="number" name="minutes" value="60" min="1" max="1440"></div>' +
      '<div><label for="f-step">Code rotates every (s)</label><input id="f-step" type="number" name="step_seconds" value="15" min="5" max="3600"></div></div>' +
      '<p class="hint">Everything is stored in one local sqlite file. Nothing leaves this machine.</p>' +
      '<div class="row" style="margin-top:12px"><button class="primary" type="submit">Start session</button></div>' +
      "</form></section>";
    html += '<section class="card"><h2>Sessions on file</h2><div class="scroll"><table><thead><tr>' +
      '<th class="num">#</th><th>Label</th><th>Location</th><th>Opened</th><th class="num">Entries</th><th>State</th>' +
      "</tr></thead><tbody>" + STATE.sessions.map(function (row) {
        return "<tr" + (s && row.id === s.id ? ' class="target"' : "") + '><td class="num mono">' + row.id + "</td><td>" +
          esc(row.label) + "</td><td>" + esc(row.location || "—") + "</td><td class=\"mono\">" + esc(shortStamp(row.opens_at)) +
          '</td><td class="num">' + row.entries + "</td><td>" +
          (row.closed_at ? badge("closed", "info") : badge("open", "ok")) + "</td></tr>";
      }).join("") + "</tbody></table></div></section>";
    html += "</div></div>";
    return html;
  };

  views.checkin = function () {
    var s = STATE.session;
    var result = lastResult || receiptFromState();
    var html = '<div class="grid two"><section class="card">';
    html += "<h2>Check in" + (s ? " to " + esc(s.label) + (s.location ? " · " + esc(s.location) : "") : "") + "</h2>";
    if (result) {
      var style = REASONS[result.status] || REASONS.rejected;
      html += '<div class="banner ' + style.cls + '"><strong>' + esc(style.title) + "</strong>" +
        esc(result.message || result.reason || "") +
        (result.seq ? ' <span class="mono">· chain slot #' + result.seq + "</span>" : "") + "</div>";
    } else {
      html += '<p class="hint">Type the code from the facilitator screen. No account, no e-mail, no app install.</p>';
    }
    html += '<form data-action="checkin">' +
      '<label for="c-code">Code from the screen</label>' +
      '<input class="code-input" id="c-code" name="code" type="text" inputmode="latin" autocomplete="off" ' +
      'autocapitalize="characters" spellcheck="false" maxlength="6" placeholder="XXXXXX" required>' +
      '<div class="row"><div><label for="c-ref">Your reference</label>' +
      '<input id="c-ref" name="ref" type="text" value="' + esc(boot.just || "P-301") + '" required></div>' +
      '<div><label for="c-name">Your name (first time only)</label>' +
      '<input id="c-name" name="name" type="text" placeholder="e.g. Amina Belkacem"></div></div>' +
      '<div class="row" style="margin-top:14px"><button class="primary" type="submit">Check in</button>' +
      '<button class="ghost" type="reset">Clear</button></div></form>';
    html += '<p class="hint">Every accepted check-in is sealed into the hash chain with the code you used and the ' +
      "rotation step it belonged to.</p>";
    html += "</section><div class=\"grid\">";
    html += '<section class="card"><h2>Just checked in</h2>';
    if (result && result.attendance_id) {
      html += '<div class="grid three">' + tile("entry", "#" + result.seq) + tile("person", (result.participant || {}).ref || "") +
        tile("kind", result.kind) + "</div>" +
        '<p class="hash" style="margin-top:12px">entry hash<br><b>' + esc(result.entry_hash) + "</b><br>previous link<br>" +
        esc(result.prev_hash) + "</p>";
    } else {
      html += '<p class="empty">The result of the next check-in appears here, with the hashes the server sealed.</p>';
    }
    html += "</section>";
    html += '<section class="card"><h2>Latest entries</h2>';
    if (!STATE.recent.length) {
      html += '<p class="empty">No check-ins yet.</p>';
    } else {
      html += '<div class="scroll"><table><thead><tr><th class="num">seq</th><th>ref</th><th>name</th>' +
        "<th>time</th><th>code</th><th>kind</th></tr></thead><tbody>" +
        STATE.recent.map(function (r) {
          var target = boot.just && r.participant_ref === boot.just;
          return "<tr" + (target ? ' class="target"' : "") + '><td class="num mono">' + r.seq + '</td><td class="mono">' +
            esc(r.participant_ref) + "</td><td>" + esc(r.participant_name) + '</td><td class="mono">' +
            esc(clockTime(r.checked_in_at)) + '</td><td class="mono">' + esc(r.code_used) + "</td><td>" +
            badge(r.kind === "duplicate" ? "duplicate" : "ok", r.kind === "duplicate" ? "warn" : "ok") + "</td></tr>";
        }).join("") + "</tbody></table></div>";
    }
    html += "</section></div></div>";
    return html;
  };

  views.roster = function () {
    var rows = STATE.roster || [];
    var present = rows.filter(function (r) { return r.present; }).length;
    var flagged = rows.filter(function (r) { return r.flags.length; }).length;
    var html = "<section class=\"card\"><div class=\"cardhead\"><h2>Roster · " + esc(sessionTitle()) + "</h2>" +
      '<span class="pill">' + present + " of " + rows.length + " present</span></div>";
    html += '<div class="grid three" style="margin-bottom:14px">' + tile("present", present) + tile("absent", rows.length - present) +
      tile("flagged", flagged) + "</div>";
    if (!rows.length) { html += '<p class="empty">Nobody has checked in yet.</p>'; }
    else {
      html += '<div class="scroll"><table><thead><tr><th>ref</th><th>name</th><th>status</th>' +
        "<th>first check-in</th><th class=\"num\">entries</th><th>flags</th></tr></thead><tbody>" +
        rows.map(function (r) {
          return '<tr><td class="mono">' + esc(r.ref) + "</td><td>" + esc(r.name) + "</td><td>" +
            (r.present ? badge("present", "ok") : badge("absent", "info")) + '</td><td class="mono">' +
            esc(r.first_checkin ? clockTime(r.first_checkin) : "—") + '</td><td class="num">' + r.entries + "</td><td>" +
            (r.flags.length ? r.flags.map(function (f) { return badge(f.replace(/_/g, " "), "warn"); }).join(" ") : '<span class="muted">—</span>') +
            "</td></tr>";
        }).join("") + "</tbody></table></div>";
    }
    html += "</section>";
    html += '<section class="card" style="margin-top:16px"><h2>Per-participant summary (this session)</h2>' +
      '<div class="scroll"><table><thead><tr><th>ref</th><th>name</th><th class="num">sessions</th>' +
      '<th class="num">entries</th><th>first</th><th>last</th><th>flags</th></tr></thead><tbody>' +
      (STATE.summary || []).map(function (r) {
        return '<tr><td class="mono">' + esc(r.ref) + "</td><td>" + esc(r.name) + '</td><td class="num">' +
          r.sessions_attended + " / " + r.sessions_total + '</td><td class="num">' + r.entries + '</td><td class="mono">' +
          esc(r.first_checkin ? clockTime(r.first_checkin) : "—") + '</td><td class="mono">' +
          esc(r.last_checkin ? clockTime(r.last_checkin) : "—") + "</td><td>" +
          (r.flags.length ? r.flags.map(function (f) { return badge(f.replace(/_/g, " "), "warn"); }).join(" ") : '<span class="muted">—</span>') +
          "</td></tr>";
      }).join("") + "</tbody></table></div></section>";
    return html;
  };

  views.anomalies = function () {
    var list = STATE.anomalies || [];
    var counts = STATE.anomaly_counts || { total: 0 };
    var html = "<section><div class=\"cardhead\"><h2>Anomaly flags - every flag carries its reason and the rows it came from</h2>" +
      '<span class="pill ' + (counts.total ? "warn" : "ok") + '">' + counts.total + " flag(s)</span></div>";
    html += '<div class="grid three" style="margin:12px 0 16px">' +
      tile("duplicate check-ins", counts.duplicate_check_in || 0) +
      tile("outside session window", counts.outside_session_window || 0) +
      tile("code reuse after rotation", counts.code_reuse_after_rotation || 0) +
      "</div></section>";
    html += '<div class="grid three" style="margin-bottom:16px">' +
      tile("impossible travel", counts.impossible_travel || 0) +
      tile("flagged rows", list.reduce(function (n, f) { return n + f.evidence.length; }, 0)) +
      tile("rules", 4) + "</div>";
    if (!list.length) { return html + '<section class="card"><p class="empty">No anomalies. The log is clean.</p></section>'; }
    html += list.map(function (f) {
      var ev = f.evidence.map(function (r) {
        return "<tr><td class=\"mono\">#" + r.id + "</td><td class=\"num mono\">" + r.seq + '</td><td class="mono">' +
          esc(r.participant_ref) + "</td><td>" + esc(r.participant_name) + "</td><td>session #" + r.session_id +
          "</td><td>" + esc(r.session_location || "—") + '</td><td class="mono">' + esc(shortStamp(r.checked_in_at)) +
          '</td><td class="mono">' + esc(r.code_used) + '</td><td class="mono">' + r.code_step + "/" + r.session_step +
          '</td><td class="mono">' + esc(String(r.entry_hash).slice(0, 12)) + "…</td></tr>";
      }).join("");
      return '<section class="card" style="margin-bottom:14px"><div class="cardhead"><h2><span class="sev-dot ' +
        f.severity + '"></span><span class="rule">' + esc(f.rule) + "</span></h2>" + badge(f.severity, f.severity === "high" ? "high" : "warn") +
        '</div><p class="reason">' + esc(f.reason) + '</p><div class="scroll evidence"><table><thead><tr>' +
        '<th>row</th><th class="num">seq</th><th>ref</th><th>name</th><th>session</th><th>location</th><th>checked in</th>' +
        "<th>code</th><th>code/session step</th><th>entry hash</th></tr></thead><tbody>" + ev + "</tbody></table></div>" +
        '<p class="hint">rule: ' + esc(f.rule_description) + "</p></section>";
    }).join("");
    return html;
  };

  views.verify = function () {
    var v = STATE.chain || { ok: true, checked: 0, first_broken: null };
    var html = '<section class="card">';
    if (v.ok) {
      html += '<div class="banner ok"><strong>Hash chain intact</strong>' + v.checked + " links verified from the genesis " +
        "block to the head. No attendance row has been edited, inserted or removed.</div>";
    } else {
      html += '<div class="banner bad"><strong>Chain broken at link #' + esc(v.first_broken.seq) + "</strong>" +
        esc(v.first_broken.reason) + " - " + esc(v.first_broken.detail) + "</div>";
    }
    html += '<div class="grid three">' + tile("links verified", v.checked) + tile("rows in log", v.total_rows) +
      tile("status", v.ok ? "VERIFIED" : "TAMPERED", true) + "</div>";
    html += '<h3>Head of chain</h3><p class="hash">' + esc(v.head) + "</p>";
    if (!v.ok) {
      var b = v.first_broken;
      html += '<h3>First broken link</h3><table><tbody>' +
        "<tr><th>attendance row</th><td class=\"mono\">" + esc(b.attendance_id === null ? "(head pointer)" : "#" + b.attendance_id) + "</td></tr>" +
        "<tr><th>sequence</th><td class=\"mono\">" + esc(b.seq) + "</td></tr>" +
        "<tr><th>reason</th><td class=\"mono\">" + esc(b.reason) + "</td></tr>" +
        "<tr><th>detail</th><td>" + esc(b.detail) + "</td></tr>" +
        "<tr><th>stored hash</th><td class=\"hash\">" + esc(b.stored_hash) + "</td></tr>" +
        "<tr><th>recomputed from the row</th><td class=\"hash\">" + esc(b.expected_hash || "—") + "</td></tr></tbody></table>";
    }
    html += '<div class="row" style="margin-top:14px"><button data-action="reverify">Verify again</button>' +
      '<a style="align-self:center" href="/api/verify">/api/verify (raw JSON)</a>' +
      '<a style="align-self:center" href="/api/export/all.json">/api/export/all.json</a></div>';
    html += "</section>";
    html += '<section class="card" style="margin-top:16px"><h2>Latest links</h2><div class="scroll"><table><thead><tr>' +
      '<th class="num">seq</th><th>row</th><th>time</th><th>code</th><th>prev_hash &rarr; entry_hash</th></tr></thead><tbody>' +
      (STATE.chain_tail || []).map(function (r) {
        return '<tr><td class="num mono">' + r.seq + '</td><td class="mono">#' + r.id + '</td><td class="mono">' +
          esc(clockTime(r.checked_in_at)) + '</td><td class="mono">' + esc(r.code_used) + '</td><td class="hash">' +
          esc(r.prev_short) + "… &rarr; <b>" + esc(r.hash_short) + "</b>…</td></tr>";
      }).join("") + "</tbody></table></div>";
    html += '<p class="hint">each link = sha256(&quot;rollcall/v1/entry&quot; + prev_hash + canonical_json(payload)) over rows: ' +
      "seq, session_id, participant_id, kind, code_used, code_step, session_step, checked_in_at, recorded_at, source.</p></section>";
    return html;
  };

  views.export = function () {
    var html = '<section class="card"><h2>Export the register</h2>' +
      '<p class="hint">Plain files, no vendor. CSV opens in Excel / Google Sheets; the JSON bundle carries the whole ' +
      "log including the chain hashes and the anomaly evidence.</p>" +
      '<div class="row" style="margin-top:12px">' +
      '<a href="/api/export/attendance.csv"><button class="primary">attendance.csv</button></a>' +
      '<a href="/api/export/summary.csv"><button>summary.csv</button></a>' +
      '<a href="/api/export/anomalies.csv"><button>anomalies.csv</button></a>' +
      '<a href="/api/export/all.json"><button>all.json</button></a></div>' +
      '<h3>Preview</h3><div class="row"><a class="mono" href="/?view=export&preview=attendance.csv">attendance.csv</a>' +
      '<a class="mono" href="/?view=export&preview=summary.csv">summary.csv</a>' +
      '<a class="mono" href="/?view=export&preview=anomalies.csv">anomalies.csv</a>' +
      '<a class="mono" href="/?view=export&preview=all.json">all.json</a></div>' +
      '<pre id="preview">loading ' + esc(previewKind) + "…</pre></section>";
    html += '<section class="card" style="margin-top:16px"><h2>Per-participant attendance summary</h2>' +
      '<div class="scroll"><table><thead><tr><th>ref</th><th>name</th><th class="num">sessions attended</th>' +
      '<th class="num">entries</th><th class="num">duplicates</th><th>first</th><th>last</th></tr></thead><tbody>' +
      (STATE.summary || []).map(function (r) {
        return '<tr><td class="mono">' + esc(r.ref) + "</td><td>" + esc(r.name) + '</td><td class="num">' +
          r.sessions_attended + " / " + r.sessions_total + '</td><td class="num">' + r.entries + '</td><td class="num">' +
          r.duplicates + '</td><td class="mono">' + esc(r.first_checkin ? shortStamp(r.first_checkin) : "—") +
          '</td><td class="mono">' + esc(r.last_checkin ? shortStamp(r.last_checkin) : "—") + "</td></tr>";
      }).join("") + "</tbody></table></div></section>";
    return html;
  };

  /* ---------------------------------------------------------------- drawing */
  function barWidth() {
    if (!STATE.code) { return 0; }
    var total = STATE.code.step_seconds;
    return Math.max(0, Math.min(100, Math.round((secsLeft() / total) * 100)));
  }
  function countEntries(id) {
    var found = (STATE.sessions || []).filter(function (s) { return s.id === id; })[0];
    return found ? found.entries : 0;
  }
  function paintChrome() {
    Array.prototype.forEach.call(document.querySelectorAll("#tabs a"), function (a) {
      a.className = a.getAttribute("data-view") === VIEW ? "active" : "";
    });
    var pill = document.getElementById("chain-pill");
    var v = STATE.chain || { ok: true, checked: 0 };
    pill.className = "pill " + (v.ok ? (STATE.anomaly_counts.total ? "warn" : "ok") : "bad");
    pill.textContent = v.ok ? ("chain ok · " + v.checked + " links") : "CHAIN BROKEN";
    document.getElementById("clock").textContent = shortStamp(STATE.generated_at);
    document.getElementById("foot-db").textContent =
      STATE.totals.participants + " people · " + STATE.totals.entries + " entries · " +
      STATE.totals.sessions + " sessions · " + STATE.anomaly_counts.total + " flags";
  }
  function tick() {
    var code = document.getElementById("bigcode");
    if (code && STATE.code) {
      var left = secsLeft();
      var meta = document.getElementById("codemeta");
      var fill = document.getElementById("barfill");
      if (meta) {
        meta.innerHTML = "rotation step " + STATE.code.step + " · rotates in <b>" + left + "</b>s · session #" +
          STATE.session.id;
      }
      if (fill) { fill.style.width = barWidth() + "%"; }
      if (left <= 0) { refresh(); }
    }
  }
  function render() {
    paintChrome();
    app.innerHTML = (views[VIEW] || views.session)();
    if (VIEW === "export") { loadPreview(); }
  }
  function loadPreview() {
    fetch("/api/export/" + previewKind).then(function (r) { return r.text(); }).then(function (text) {
      var pre = document.getElementById("preview");
      if (!pre) { return; }
      var lines = text.split("\n");
      pre.textContent = previewKind === "all.json"
        ? lines.slice(0, 60).join("\n") + "\n…"
        : lines.slice(0, 21).join("\n") + (lines.length > 21 ? "\n… " + (lines.length - 22) + " more rows" : "");
    });
  }
  function refresh() {
    var active = document.activeElement;
    if (active && active.tagName === "INPUT" && active.value) { return; }
    var url = "/api/state?view=" + encodeURIComponent(VIEW) +
      (STATE.session ? "&session=" + STATE.session.id : "");
    fetch(url).then(function (r) { return r.json(); }).then(function (blob) {
      applyState(blob);
      render();
    }).catch(function () { /* offline: the page keeps showing the last known state */ });
  }

  /* ---------------------------------------------------------------- actions */
  app.addEventListener("submit", function (event) {
    var form = event.target;
    var action = form.getAttribute("data-action");
    if (!action) { return; }
    event.preventDefault();
    var data = {};
    new FormData(form).forEach(function (value, key) { data[key] = value; });
    if (action === "new-session") {
      api("/api/sessions", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          label: data.label, location: data.location,
          minutes: parseInt(data.minutes, 10) || 60,
          step_seconds: parseInt(data.step_seconds, 10) || 15
        })
      }).then(function (out) {
        window.location.href = "/?view=session&session=" + out.session.id;
      }).catch(function (err) { window.alert("Could not open the session: " + err.message); });
      return;
    }
    if (action === "checkin") {
      api("/api/checkin", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          session_id: STATE.session ? STATE.session.id : null,
          ref: data.ref, name: data.name, code: data.code
        })
      }).then(function (out) {
        lastResult = out;
        if (out.participant) { boot.just = out.participant.ref; }
        render();
        return api("/api/state?view=checkin" + (STATE.session ? "&session=" + STATE.session.id : ""));
      }).then(function (blob) {
        applyState(blob);
        render();
      }).catch(function (err) {
        lastResult = { status: "rejected", reason: "rejected", message: err.message };
        render();
      });
    }
  });

  app.addEventListener("click", function (event) {
    var target = event.target.closest ? event.target.closest("[data-action]") : null;
    if (!target) { return; }
    var action = target.getAttribute("data-action");
    if (action === "close-session" && STATE.session) {
      if (!window.confirm("Close session " + STATE.session.id + "? The code stops working; the log stays.")) { return; }
      api("/api/sessions/" + STATE.session.id + "/close", { method: "POST" })
        .then(refresh).catch(function (err) { window.alert(err.message); });
    }
    if (action === "reverify") {
      api("/api/verify").then(function () { refresh(); });
    }
  });

  applyState(STATE);
  render();
  setInterval(tick, 250);
  setInterval(refresh, 3000);
})();
