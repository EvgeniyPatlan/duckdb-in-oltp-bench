/* Explore page: two systems query by query, one query across systems, the probe table.
   Builds on charts.js (window.DBENCH); every choice is kept in the page address. */
(function () {
  "use strict";
  var D = window.DBENCH;
  if (!D) return;
  var DATA = D.DATA, state = D.state, esc = D.esc, fmt = D.fmt;

  function $(id) { return document.getElementById(id); }
  /* A select marked data-all starts with an "All" choice (value ""). The value it ends up showing
     is written back to the view, so a choice that does not exist here (another scale factor, an
     edited link) never filters what the select no longer shows. */
  function fill(sel, options, value) {
    if (!sel) return;
    sel.innerHTML = (sel.hasAttribute("data-all") ? '<option value="">All</option>' : "") +
      options.map(function (o) { return '<option value="' + esc(o.v) + '">' + esc(o.l) + "</option>"; }).join("");
    sel.value = options.some(function (o) { return o.v === value; }) ? value : sel.options.length ? sel.options[0].value : "";
    state.extra[sel.getAttribute("data-key")] = sel.value;
  }
  function power() { return D.bySf("power"); }
  function cellOf(sys, q) { var p = power(); return p && p.cells[sys] && p.cells[sys][q]; }
  function label(n) { var s = D.sysOf(n); return s ? s.label : n; }
  function okv(c) { return c && c.s === "ok" && c.v != null && c.v > 0; }
  function timeOf(c) { return state.metric === "cold" ? c.cold : c.v; }

  /* ---- pickers ---- */
  function defaults() {
    var plain = DATA.systems.filter(function (s) { return s.kind === "plain"; });
    var engines = DATA.systems.filter(function (s) { return s.kind === "integration"; });
    var a = plain.length ? plain[plain.length - 1] : DATA.systems[0];
    var b = engines.length ? engines[0] : DATA.systems[1] || DATA.systems[0];
    return {a: a && a.name, b: b && b.name};
  }
  function bindPickers() {
    var timer = null;
    Array.prototype.forEach.call(document.querySelectorAll("[data-key]"), function (el) {
      var handler = function () { state.extra[el.getAttribute("data-key")] = el.value; D.drawAll(); };
      if (el.tagName === "INPUT") {
        el.addEventListener("input", function () { clearTimeout(timer); timer = setTimeout(handler, 200); });
      } else {
        el.addEventListener("change", handler);
      }
    });
    var swap = $("pair-swap");
    if (swap) swap.onclick = function () {
      var a = state.extra.a; state.extra.a = state.extra.b; state.extra.b = a; D.drawAll();
    };
  }
  function syncPickers() {
    var p = power(), d = defaults();
    var sysOpts = DATA.systems.filter(function (s) { return p && p.cells[s.name]; })
      .map(function (s) { return {v: s.name, l: s.label}; });
    var search = $("probe-q");
    if (search && document.activeElement !== search) search.value = state.extra.pq || "";
    var known = function (v) { return sysOpts.some(function (o) { return o.v === v; }); };
    if (!known(state.extra.a)) state.extra.a = known(d.a) ? d.a : sysOpts.length ? sysOpts[0].v : "";
    if (!known(state.extra.b)) state.extra.b = "";
    if (!state.extra.b) {
      var other = sysOpts.filter(function (o) { return o.v !== state.extra.a; })[0];
      state.extra.b = d.b !== state.extra.a ? d.b : other && other.v;
    }
    fill($("pair-a"), sysOpts, state.extra.a);
    fill($("pair-b"), sysOpts, state.extra.b);
    var qs = p ? p.queries : [];
    fill($("query-pick"), qs.map(function (q) { return {v: q, l: q}; }), state.extra.q || qs[0]);
    var sc = D.bySf("sqlcompat");
    var probeSys = sc ? DATA.systems.filter(function (s) { return sc.cells[s.name]; }) : [];
    fill($("probe-sys"), probeSys.map(function (s) { return {v: s.name, l: s.label}; }), state.extra.ps || "");
    var cats = sc ? Array.from(new Set(sc.probes.map(function (x) { return x.category; }))).sort() : [];
    fill($("probe-cat"), cats.map(function (c) { return {v: c, l: c}; }), state.extra.pc || "");
    fill($("probe-cls"), Object.keys(D.CLASS_COLORS).map(function (c) { return {v: c, l: c}; }), state.extra.po || "");
  }

  /* ---- two systems ---- */
  function pairRows() {
    var p = power(), a = state.extra.a, b = state.extra.b;
    if (!p) return [];
    return p.queries.map(function (q) {
      var ca = cellOf(a, q), cb = cellOf(b, q);
      var ta = okv(ca) ? timeOf(ca) : null, tb = okv(cb) ? timeOf(cb) : null;
      return {q: q, ca: ca, cb: cb, ta: ta, tb: tb, ratio: ta && tb ? tb / ta : null};
    });
  }
  D.B.pair_ratio = function () {
    var rows = pairRows().filter(function (r) { return r.ratio; });
    if (!rows.length) return null;
    var sa = D.sysOf(state.extra.a), sb = D.sysOf(state.extra.b);
    var data = rows.map(function (r) {
      var v = Math.log10(r.ratio);
      return {value: v, itemStyle: {color: v >= 0 ? sb.color : sa.color}, q: r.q, r: r};
    });
    var opt = D.wrap("Time of " + sb.label + " relative to " + sa.label, {
      legend: {show: false},
      tooltip: {trigger: "item", confine: true, formatter: function (x) {
        var r = x.data.r;
        return "<b>" + esc(r.q) + "</b><br>" + esc(sa.label) + ": " + fmt(r.ta) + " s<br>" + esc(sb.label) + ": " + fmt(r.tb) +
          " s<br><b>" + fmt(r.ratio) + "x</b> (" + esc(r.ratio > 1 ? sb.label + " slower" : r.ratio < 1 ? sb.label + " faster" : "same time") + ")";
      }},
      xAxis: {type: "category", data: rows.map(function (r) { return r.q; }), axisLabel: {interval: 0, rotate: 45}},
      yAxis: {type: "value", name: "B / A", axisLabel: {formatter: function (v) { return fmt(Math.pow(10, v)) + "x"; }}},
      series: [{type: "bar", data: data, barMaxWidth: 28, markLine: {silent: true, symbol: "none", data: [{yAxis: 0}],
        lineStyle: {color: "#888", type: "dotted"}, label: {formatter: "1x"}}}]
    });
    return opt;
  };
  function geomean(xs) {
    return xs.length ? Math.pow(10, xs.reduce(function (s, x) { return s + Math.log10(x); }, 0) / xs.length) : null;
  }
  function pairSummary() {
    var host = $("pair-summary");
    if (!host) return;
    var rows = pairRows(), cmp = rows.filter(function (r) { return r.ratio; });
    var a = label(state.extra.a), b = label(state.extra.b);
    if (state.extra.a === state.extra.b) { host.textContent = "Pick two different systems."; return; }
    if (!cmp.length) { host.textContent = "No query was answered correctly by both " + a + " and " + b + "."; return; }
    var g = geomean(cmp.map(function (r) { return r.ratio; }));
    var slower = cmp.filter(function (r) { return r.ratio > 1; }).length;
    var faster = cmp.filter(function (r) { return r.ratio < 1; }).length;
    var why = function (name, c, t) {
      return !c ? name + ": no record" : !okv(c) ? name + ": " + c.s : t == null ? name + ": no cold time" : null;
    };
    var skipped = rows.filter(function (r) { return !r.ratio; }).map(function (r) {
      return r.q + " (" + [why(a, r.ca, r.ta), why(b, r.cb, r.tb)].filter(Boolean).join(", ") + ")";
    });
    host.textContent = "Over " + cmp.length + " queries (" + (state.metric === "cold" ? "cold" : "warm") + "), " + b +
      " took " + fmt(g) + "x the time of " + a + " (geometric mean); " + b + " was slower on " + slower + " and faster on " +
      faster + (cmp.length - slower - faster ? " (equal on " + (cmp.length - slower - faster) + ")" : "") + "." + (skipped.length ? " Not compared: " + skipped.join(", ") + "." : "");
  }
  function cellText(c) { return !c ? "no record" : c.s === "ok" ? fmt(c.v) : c.s; }
  function pairTable() {
    var t = $("pair-table");
    if (!t) return;
    var a = label(state.extra.a), b = label(state.extra.b);
    var head = ["Query", a + " warm (s)", a + " cold (s)", b + " warm (s)", b + " cold (s)", "B / A"];
    var body = pairRows().map(function (r) {
      return [r.q, cellText(r.ca), r.ca && r.ca.cold != null ? fmt(r.ca.cold) : "", cellText(r.cb),
        r.cb && r.cb.cold != null ? fmt(r.cb.cold) : "", r.ratio ? fmt(r.ratio) : ""];
    });
    table(t, head, body);
  }

  /* ---- one query ---- */
  D.B.query_detail = function () {
    var p = power(), q = state.extra.q || (p && p.queries[0]);
    if (!p || !q) return null;
    var cats = [{id: "warm", label: "warm median"}, {id: "cold", label: "cold first run"}];
    return D.grouped("Query " + q, cats, function (n, kind) {
      var c = cellOf(n, q);
      if (!c) return null;
      if (kind === "cold" && !c.mark) {
        return c.cold == null ? {v: null, s: "n/a", r: "no cold time recorded", mark: "na"} : Object.assign({}, c, {v: c.cold});
      }
      return D.lines(c, [c.pushed ? "pushed: " + c.pushed : null]);
    }, {log: true, unit: "s", shownFmt: function (v) { return fmt(v) + " s"; }});
  };

  /* ---- probes ---- */
  function probeTable() {
    var t = $("probe-table"), sc = D.bySf("sqlcompat");
    if (!t) return;
    if (!sc) { $("probe-count").textContent = "No probe results at this scale factor"; table(t, ["System"], []); return; }
    var f = {sys: state.extra.ps || "", cat: state.extra.pc || "", cls: state.extra.po || "", q: (state.extra.pq || "").toLowerCase()};
    var rows = [];
    DATA.systems.forEach(function (s) {
      if (!sc.cells[s.name] || (f.sys && f.sys !== s.name)) return;
      sc.probes.forEach(function (p) {
        var c = sc.cells[s.name][p.id];
        if (!c || (f.cat && p.category !== f.cat) || (f.cls && c.c !== f.cls)) return;
        var row = [s.label, p.category, p.id, p.title, c.c, c.r || ""];
        if (f.q && row.join(" ").toLowerCase().indexOf(f.q) < 0) return;
        rows.push(row);
      });
    });
    $("probe-count").textContent = rows.length + " probe results";
    table(t, ["System", "Category", "Probe", "What it checks", "Outcome", "Message"], rows);
  }

  /* ---- tables: sort by a header click, download as CSV ---- */
  function table(t, head, body) {
    t._head = head; t._body = body;
    var sort = t._sort || {col: -1, dir: 1};
    var rows = body.slice();
    if (sort.col >= 0) {
      /* numbers in order, then text (statuses) after them, so the order is consistent */
      var num = function (v) { var n = parseFloat(String(v).replace(/,/g, "")); return isNaN(n) ? null : n; };
      rows.sort(function (x, y) {
        var a = num(x[sort.col]), b = num(y[sort.col]);
        var r = a != null && b != null ? a - b : a != null ? -1 : b != null ? 1 : String(x[sort.col]).localeCompare(String(y[sort.col]));
        return r * sort.dir;
      });
    }
    t.innerHTML = "<thead><tr>" + head.map(function (h, i) {
      var mark = i === sort.col ? (sort.dir > 0 ? " ▲" : " ▼") : "";
      return '<th scope="col"><button type="button" data-col="' + i + '">' + esc(h) + mark + "</button></th>";
    }).join("") + "</tr></thead><tbody>" + rows.map(function (r) {
      return "<tr>" + r.map(function (v, i) {
        var num = i > 0 && v !== "" && !isNaN(parseFloat(String(v).replace(/,/g, "")));
        return "<td" + (num ? ' class="num"' : "") + ">" + esc(v) + "</td>";
      }).join("") + "</tr>";
    }).join("") + "</tbody>";
    Array.prototype.forEach.call(t.querySelectorAll("th button"), function (btn) {
      btn.onclick = function () {
        var col = Number(btn.getAttribute("data-col"));
        t._sort = {col: col, dir: sort.col === col ? -sort.dir : 1};
        table(t, t._head, t._body);
      };
    });
  }
  function csvCell(v) {
    var s = String(v == null ? "" : v);
    if (/^[\s=+\-@]/.test(s) && isNaN(Number(s))) s = "'" + s; // keep spreadsheets from running a formula
    return /[",\r\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
  }
  function download(t) {
    var lines = [t._head].concat(t._body || []).map(function (r) { return r.map(csvCell).join(","); });
    var a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([lines.join("\n") + "\n"], {type: "text/csv"}));
    a.download = t.id + (state.sf ? "-sf" + state.sf : "") + ".csv";
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
  }
  Array.prototype.forEach.call(document.querySelectorAll("[data-csv]"), function (btn) {
    btn.onclick = function () { var t = $(btn.getAttribute("data-csv")); if (t && t._head) download(t); };
  });

  D.beforeDraw.push(syncPickers);
  D.afterDraw.push(function () { pairSummary(); pairTable(); probeTable(); });
  bindPickers();
})();
