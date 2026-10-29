/* Charts for the results site. Every system is drawn the same way: one fixed colour from the
   data file, the same order, the same marks for failures. Nothing here singles a system out. */
(function () {
  "use strict";
  var DATA = JSON.parse(document.getElementById("site-data").textContent);
  var PAGE = document.body.getAttribute("data-page");
  var G = DATA.glyphs;
  var FAIL = "#b00020";
  var CLASS_COLORS = {duckdb: "#2b8a6e", fallback: "#e0a030", error: "#c0392b", wrong_result: "#7b1fa2",
    unknown: "#9aa3ae", ok_unobserved: "#b7d9c8", "n/a": "#e5e7ea"};
  var CLASS_GLYPH = {duckdb: "D", fallback: "F", error: "E", wrong_result: "✗", unknown: "?",
    ok_unobserved: "✓", "n/a": "–"};
  var CLASSES = Object.keys(CLASS_COLORS);
  var state = {sf: null, hidden: {}, metric: "warm", extra: {}};
  var instances = [];
  var beforeDraw = [], afterDraw = [];
  var defaultSf = null;
  /* The data set the builders read: the page's own, or its Parquet section (DATA.parquet). */
  var CUR = DATA;

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c];
    });
  }
  function fmt(v) {
    if (v == null) return "";
    var a = Math.abs(v);
    return a >= 100 ? v.toFixed(0) : a >= 1 ? v.toFixed(2) : a >= 0.01 ? v.toFixed(3) : v.toPrecision(2);
  }
  function fmtBytes(v) {
    var u = ["B", "KB", "MB", "GB", "TB"], i = 0;
    while (v >= 1000 && i < u.length - 1) { v /= 1000; i++; }
    return fmt(v) + " " + u[i];
  }
  function narrow() { return window.innerWidth < 640; }
  function bg() { return getComputedStyle(document.body).getPropertyValue("--card").trim() || "#fff"; }
  function ink() { return getComputedStyle(document.body).color; }
  function systems() { return CUR.systems.filter(function (s) { return !state.hidden[s.name]; }); }
  function bySf(section) { return (CUR[section] || {})[state.sf] || null; }
  /* Every system the page shows: its own, then those only in the Parquet section. */
  function allSystems() {
    var names = {};
    DATA.systems.forEach(function (s) { names[s.name] = 1; });
    return DATA.systems.concat(((DATA.parquet || {}).systems || []).filter(function (s) { return !names[s.name]; }));
  }
  function sysOf(name) { return allSystems().filter(function (s) { return s.name === name; })[0]; }
  function inParquet(build) {
    return function () {
      if (!DATA.parquet) return null;
      CUR = DATA.parquet;
      try { return build(); } finally { CUR = DATA; }
    };
  }

  /* ---- option scaffolding ---- */
  function wrap(title, opt) {
    var base = {
      animation: false, backgroundColor: bg(), textStyle: {color: ink()},
      grid: {left: narrow() ? 8 : 16, right: 24, top: 36, bottom: narrow() ? 150 : 90, containLabel: true},
      legend: {type: "plain", bottom: 0, textStyle: {color: ink()}},
      toolbox: {right: 4, top: 0, feature: {
        saveAsImage: {title: "Export PNG", name: title, pixelRatio: 2, type: "png", backgroundColor: bg()},
        mySaveSvg: {title: "Export SVG", icon: "path://M4 4h16v16H4z M8 10l4 4 4-4 M12 6v8",
          onclick: null}}},
      tooltip: {trigger: "item", confine: true, formatter: tip}
    };
    return Object.assign(base, opt);
  }
  function tip(p) {
    var d = p.data || {}, c = d.cell;
    if (!c) return esc(p.seriesName) + ": " + esc(p.name) + " " + esc(p.value);
    var sys = d.system ? sysOf(d.system) : null;
    var head = "<b>" + esc(sys ? sys.label : p.seriesName) + "</b> &middot; " + esc(d.cat || p.name);
    var ver = sys && sys.duckdb_version ? "<br>DuckDB " + esc(sys.duckdb_version) : "";
    var body = c.mark ? "<br><b>" + esc(c.s) + "</b>" + (c.r ? ": " + esc(c.r) : "")
      : "<br>" + esc(d.shown != null ? d.shown : fmt(c.v));
    var more = (c.lines || []).map(function (l) { return "<br>" + esc(l); }).join("");
    return head + ver + body + more;
  }
  function exportSvg(chart, name) {
    var host = document.createElement("div");
    host.style.cssText = "position:absolute;left:-99999px;width:" + chart.getWidth() + "px;height:" + chart.getHeight() + "px";
    document.body.appendChild(host);
    var svg = echarts.init(host, null, {renderer: "svg"});
    var opt = chart.getOption();
    opt.animation = false;
    svg.setOption(opt);
    var a = document.createElement("a");
    a.href = svg.getDataURL({type: "svg"});
    a.download = name.replace(/[^\w-]+/g, "_") + ".svg";
    a.click();
    svg.dispose();
    document.body.removeChild(host);
  }

  /* ---- cells to bars ---- */
  function floorFor(values, log) {
    var pos = values.filter(function (v) { return v > 0; });
    if (log) {
      var lo = pos.length ? Math.min.apply(null, pos) : 1;
      var min = Math.pow(10, Math.floor(Math.log10(lo)) - 1);
      return {axisMin: min, mark: min * 3};
    }
    var hi = pos.length ? Math.max.apply(null, pos) : 1;
    return {axisMin: 0, mark: hi * 0.04};
  }
  function barStyle(sys) {
    var s = {color: sys.color};
    if (sys.kind === "rowstore") {
      s.decal = {symbol: "rect", dashArrayX: [1, 0], dashArrayY: [2, 3], rotation: Math.PI / 4, color: "rgba(255,255,255,0.55)"};
    }
    return s;
  }
  function pointFor(sys, cat, cell, floor, shown) {
    if (!cell) cell = {s: "n/a", r: "no record", mark: "na"};
    var d = {system: sys.name, cat: cat, cell: cell, shown: shown};
    if (!cell.mark) { d.value = cell.v; return d; }
    d.value = floor;
    d.itemStyle = {color: "rgba(0,0,0,0)", borderColor: cell.mark === "failure" ? FAIL : sys.color,
      borderWidth: 2, borderType: "dashed"};
    d.label = {show: true, position: "top", formatter: G[cell.s] || cell.s,
      color: cell.mark === "failure" ? FAIL : ink(), fontWeight: "bold", fontSize: 13};
    return d;
  }
  /* cats: [{id, label}], cellAt(sysName, catId) -> cell, opts {log, axisFmt, shownFmt, max}.
     Only systems with a record in this chart are listed. A log axis is a linear axis over
     log10 values shifted so every bar starts at the axis minimum. */
  function grouped(title, cats, cellAt, opts) {
    var list = systems().filter(function (s) {
      return cats.some(function (c) { return cellAt(s.name, c.id); });
    });
    var all = [];
    list.forEach(function (s) { cats.forEach(function (c) {
      var x = cellAt(s.name, c.id); if (x && !x.mark && x.v != null) all.push(x.v);
    }); });
    var fl = floorFor(all, opts.log);
    var lo = opts.log ? Math.log10(fl.axisMin) : 0;
    var y = function (v) { return opts.log ? Math.log10(v) - lo : v; };
    var series = list.map(function (s) {
      return {name: s.label, type: "bar", itemStyle: barStyle(s), barMaxWidth: 28, data: cats.map(function (c) {
        var cell = cellAt(s.name, c.id);
        var d = pointFor(s, c.label, cell, fl.mark, cell && !cell.mark && opts.shownFmt ? opts.shownFmt(cell.v) : null);
        if (d.value != null) d.value = y(d.value);
        if (opts.zeroLabel && cell && !cell.mark && cell.v === 0) {
          d.label = {show: true, position: "top", formatter: "0%", color: ink()};
        }
        return d;
      })};
    });
    var catAxis = {type: "category", data: cats.map(function (c) { return c.label; }),
      axisLabel: {interval: 0, rotate: opts.rotate || 0, color: ink()}};
    var valAxis = {type: "value", min: 0, max: opts.max, name: opts.unit || "", axisLabel: {color: ink(),
      formatter: opts.log ? function (v) { return fmt(Math.pow(10, v + lo)); } : opts.axisFmt || undefined}};
    if (opts.log) valAxis.interval = 1;
    return wrap(title, {series: series, xAxis: catAxis, yAxis: valAxis});
  }
  /* One bar per system (horizontal), coloured by system. */
  function perSystem(title, cellAt, opts) {
    var list = systems();
    var vals = [];
    list.forEach(function (s) { var c = cellAt(s.name); if (c && !c.mark && c.v != null) vals.push(c.v); });
    var fl = floorFor(vals, false);
    var data = list.map(function (s) {
      var cell = cellAt(s.name);
      var d = pointFor(s, opts.catLabel || title, cell, fl.mark, cell && !cell.mark ? (opts.shownFmt || fmt)(cell.v) : null);
      if (d.label) d.label.position = "right";
      else d.itemStyle = barStyle(s);
      return d;
    });
    var s0 = {type: "bar", data: data, barMaxWidth: 26, name: title};
    if (opts.refLine != null) s0.markLine = {silent: true, symbol: "none", data: [{xAxis: opts.refLine}],
      lineStyle: {color: "#888", type: "dotted"}, label: {formatter: opts.refLine + "x"}};
    return wrap(title, {legend: {show: false}, grid: {left: 8, right: 56, top: 32, bottom: 24, containLabel: true},
      xAxis: {type: "value", min: 0, axisLabel: {color: ink()}},
      yAxis: {type: "category", inverse: true, data: list.map(function (s) { return s.label; }), axisLabel: {color: ink()}},
      series: [s0]});
  }
  function lines(cell, arr) { return Object.assign({}, cell, {lines: arr.filter(Boolean)}); }

  /* ---- builders ---- */
  var B = {};
  function totalFor(section, name) {
    var t = ((CUR[section] || {})[state.sf] || {}).totals;
    t = t && t[name];
    if (!t) return null;
    var own = t.own_s == null ? "none" : fmt(t.own_s) + " s (" + t.used + "/" + t.total + " queries)";
    return lines(t.common, ["Own total: " + own]);
  }
  B.headline_power = function () {
    if (!bySf("power")) return null;
    var p = bySf("power");
    return perSystem("Total query time, common queries", function (n) { return totalFor("power", n); },
      {unit: "s", catLabel: "Common queries (" + p.common_queries.length + ")", shownFmt: function (v) { return fmt(v) + " s"; }});
  };
  B.trackb_total = inParquet(function () { return B.headline_power(); });
  B.headline_qph = function () {
    var t = bySf("throughput");
    if (!t) return null;
    return perSystem("Throughput at 16 streams", function (n) {
      var c = t[n] && t[n]["16"];
      return c && lines(c, ["p95 latency: " + fmt(c.p95) + " s"]);
    }, {unit: "queries/hour", catLabel: "16 streams", shownFmt: function (v) { return fmt(v) + " qph"; }});
  };
  B.headline_overhead = function () {
    var o = bySf("overhead");
    if (!o) return null;
    return perSystem("Overhead vs plain DuckDB (geomean, common queries)", function (n) {
      var e = o[n];
      if (!e) return null;
      var own = e.own;
      return lines(e.headline, ["Common queries used: " + e.headline.used + "/" + e.headline.total,
        "Own geomean: " + (own.v == null ? "n/a" : fmt(own.v) + "x") + " (" + own.used + "/" + own.total + " queries" + (own.partial ? ", partial" : "") + ")",
        e.headline.plain ? "Plain baseline: " + e.headline.plain : null,
        e.headline.version_gap ? "Version differs from the reference " + e.headline.version_gap : null]);
    }, {unit: "x plain DuckDB", catLabel: "geomean_common", refLine: 1, shownFmt: function (v) { return fmt(v) + "x"; }});
  };
  function queryChart(title, section, cells, queries) {
    var metric = state.metric;
    return grouped(title, queries.map(function (q) { return {id: q, label: q}; }), function (n, q) {
      var c = cells[n] && cells[n][q];
      if (!c) return null;
      if (metric === "cold" && !c.mark) {
        return c.cold == null ? {v: null, s: "n/a", r: "no cold time recorded", mark: "na"} : Object.assign({}, c, {v: c.cold});
      }
      return lines(c, [c.pushed ? "pushed: " + c.pushed : null, c.cold != null ? "cold: " + fmt(c.cold) + " s" : null]);
    }, {log: true, unit: "s", rotate: 45, shownFmt: function (v) { return fmt(v) + " s"; }});
  }
  B.power_query = function () {
    var p = bySf("power");
    return p && queryChart("Per-query time", "power", p.cells, p.queries);
  };
  B.trackb_query = inParquet(function () { return B.power_query(); });
  var LOAD_CATS = [{id: "load_bulk", label: "Bulk path"}, {id: "load_convert", label: "Convert path"}];
  B.load = function () {
    var l = bySf("load");
    if (!l) return null;
    return grouped("Load time", LOAD_CATS, function (n, i) {
      var c = l[n] && l[n][i];
      return c && lines(c, [c.staging_s != null ? "row-store staging load: " + fmt(c.staging_s) + " s" : null]);
    }, {unit: "s", shownFmt: function (v) { return fmt(v) + " s"; }});
  };
  B.storage = function () {
    var st = bySf("storage");
    if (!st) return null;
    var cats = [{id: "after_load", label: "After load"}, {id: "after_checkpoint", label: "After checkpoint"}];
    return grouped("Storage", cats, function (n, i) {
      var c = st[n] && st[n][i];
      if (!c) return null;
      var b = c.breakdown || {};
      return lines(c, Object.keys(b).map(function (k) { return k + ": " + fmtBytes(b[k]); }));
    }, {unit: "bytes", axisFmt: fmtBytes, shownFmt: fmtBytes});
  };
  function curve(title, pick, unit) {
    var t = bySf("throughput");
    if (!t) return null;
    var xs = ["1", "4", "8", "16", "32"];
    var all = [];
    var series = [];
    var list = systems().filter(function (s) { return t[s.name]; });
    list.forEach(function (s) { xs.forEach(function (x) {
      var c = t[s.name][x]; if (c && !c.mark && pick(c) != null) all.push(pick(c)); }); });
    var fl = floorFor(all, false);
    list.forEach(function (s) {
      var line = {name: s.label, type: "line", symbolSize: 7, itemStyle: {color: s.color}, lineStyle: {color: s.color,
        type: s.kind === "rowstore" ? "dashed" : "solid"}, data: xs.map(function (x) {
        var c = t[s.name][x];
        return c && !c.mark && pick(c) != null ? {value: pick(c), system: s.name, cat: x + " streams", cell: c, shown: fmt(pick(c)) + " " + unit} : null;
      })};
      var marks = {name: s.label, type: "scatter", symbol: "rect", symbolSize: 16, data: [], tooltip: {}};
      xs.forEach(function (x, i) {
        var c = t[s.name][x];
        if (c && c.mark) {
          marks.data.push({value: [i, fl.mark], system: s.name, cat: x + " streams", cell: c,
            itemStyle: {color: "rgba(0,0,0,0)", borderColor: c.mark === "failure" ? FAIL : s.color, borderWidth: 2, borderType: "dashed"},
            label: {show: true, formatter: G[c.s] || c.s, position: "top", color: c.mark === "failure" ? FAIL : ink(), fontWeight: "bold"}});
        }
      });
      series.push(line);
      if (marks.data.length) series.push(marks);
    });
    return wrap(title, {series: series, xAxis: {type: "category", data: xs, name: "streams", axisLabel: {color: ink()}},
      yAxis: {type: "value", min: 0, name: unit, axisLabel: {color: ink()}},
      legend: {type: "plain", bottom: 0, textStyle: {color: ink()}, data: list.map(function (s) { return s.label; })}});
  }
  B.curve = function () { return curve("Throughput vs streams", function (c) { return c.v; }, "qph"); };
  B.latency = function () { return curve("p95 latency vs streams", function (c) { return c.p95; }, "s"); };
  function compatSystems(sc) { return systems().filter(function (s) { return sc.cells[s.name]; }); }
  B.compat_summary = function () {
    var sc = bySf("sqlcompat");
    if (!sc) return null;
    var list = compatSystems(sc);
    var series = CLASSES.map(function (c) {
      return {name: c, type: "bar", stack: "all", itemStyle: {color: CLASS_COLORS[c]}, data: list.map(function (s) {
        var counts = sc.counts[s.name] || {}, total = Object.keys(counts).reduce(function (a, k) { return a + counts[k]; }, 0);
        return {value: total ? 100 * (counts[c] || 0) / total : 0, count: counts[c] || 0};
      })};
    });
    return wrap("Probe outcomes", {tooltip: {trigger: "item", confine: true, formatter: function (p) {
      return "<b>" + esc(p.name) + "</b><br>" + esc(p.seriesName) + ": " + p.data.count + " probes (" + fmt(p.value) + "%)"; }},
      xAxis: {type: "value", max: 100, axisLabel: {formatter: "{value}%", color: ink()}},
      yAxis: {type: "category", inverse: true, data: list.map(function (s) { return s.label; }), axisLabel: {color: ink()}},
      series: series});
  };
  B.compat_heatmap = function () {
    var sc = bySf("sqlcompat");
    if (!sc) return null;
    var list = compatSystems(sc), data = [];
    sc.probes.forEach(function (p, y) { list.forEach(function (s, x) {
      var c = sc.cells[s.name][p.id], cls = c ? c.c : "n/a";
      data.push({value: [x, y, CLASSES.indexOf(cls)], cls: cls, probe: p, cell: c, system: s.name,
        label: {show: true, formatter: CLASS_GLYPH[cls], color: cls === "wrong_result" ? "#fff" : "#111", fontSize: 10}});
    }); });
    var opt = wrap("SQL compatibility by probe", {
      grid: {left: 8, right: 16, top: 64, bottom: 40, containLabel: true},
      tooltip: {trigger: "item", confine: true, formatter: function (p) {
        var d = p.data, c = d.cell || {};
        return "<b>" + esc(sysOf(d.system).label) + "</b><br>" + esc(d.probe.category) + " / " + esc(d.probe.id) +
          "<br>" + esc(d.probe.title) + "<br><b>" + esc(d.cls) + "</b>" + (c.r ? ": " + esc(c.r) : ""); }},
      xAxis: {type: "category", position: "top", data: list.map(function (s) { return s.label; }), axisLabel: {interval: 0, rotate: narrow() ? 60 : 0, color: ink()}, splitArea: {show: true}},
      yAxis: {type: "category", inverse: true, data: sc.probes.map(function (p) { return p.category + " / " + p.id; }), axisLabel: {fontSize: 10, color: ink()}},
      visualMap: {type: "piecewise", show: false, dimension: 2, pieces: CLASSES.map(function (c, i) { return {value: i, color: CLASS_COLORS[c]}; })},
      series: [{type: "heatmap", data: data, itemStyle: {borderColor: "#fff", borderWidth: 1}}]});
    opt.legend = {show: false};
    return opt;
  };
  var FB_CATS = [{id: "fallback_share_tpch", label: "TPC-H queries"}, {id: "fallback_share_probes", label: "SQL probes"}];
  B.fallback = function () {
    var f = bySf("fallback");
    if (!f) return null;
    return grouped("Row-path fallback share", FB_CATS, function (n, i) {
      var c = f[n] && f[n][i];
      if (!c) return null;
      var vis = f[n].visibility;
      return lines(Object.assign({}, c, {v: c.v == null ? null : 100 * c.v}), [
        c.counts ? "duckdb " + c.counts.duckdb + ", fallback " + c.counts.fallback + ", unknown " + c.counts.unknown : null,
        c.unobserved ? "unobserved: " + fmt(100 * c.unobserved) + "%" : null,
        vis && vis.mechanism ? "visibility: " + vis.mechanism : null, vis && vis.note]);
    }, {unit: "% of statements", max: 100, zeroLabel: true, shownFmt: function (v) { return fmt(v) + "%"; }});
  };
  B.overhead_query = function () {
    var o = bySf("overhead");
    if (!o) return null;
    var qs = {};
    Object.keys(o).forEach(function (n) { Object.keys(o[n].ratios).forEach(function (q) { qs[q] = 1; }); });
    var cells = {};
    Object.keys(o).forEach(function (n) { cells[n] = o[n].ratios; });
    return grouped("Per-query overhead", Object.keys(qs).sort().map(function (q) { return {id: q, label: q}; }),
      function (n, q) { return cells[n] && cells[n][q]; },
      {log: true, unit: "x plain DuckDB", rotate: 45, shownFmt: function (v) { return fmt(v) + "x"; }});
  };

  /* ---- overhead table, controls, lifecycle ---- */
  function overheadTable() {
    var host = document.getElementById("overhead_table"), o = bySf("overhead");
    if (!host) return;
    if (!o) { host.innerHTML = ""; return; }
    var rows = systems().filter(function (s) { return o[s.name]; }).map(function (s) {
      var e = o[s.name], h = e.headline, w = e.own;
      var hv = h.mark ? '<td class="mark" title="' + esc(h.s + ": " + (h.r || "")) + '">' + esc(G[h.s] || h.s) + "</td>" : '<td class="num">' + fmt(h.v) + "x</td>";
      var wv = w.mark ? '<td class="mark" title="' + esc(w.s + ": " + (w.r || "")) + '">' + esc(G[w.s] || w.s) + "</td>" : '<td class="num">' + fmt(w.v) + "x</td>";
      return "<tr><th scope=\"row\">" + esc(s.label) + "</th>" + hv + "<td class=\"num\">" + h.used + "/" + h.total + "</td>" + wv +
        "<td class=\"num\">" + w.used + "/" + w.total + (w.partial ? " (partial)" : "") + "</td><td>" + esc(h.plain || "") + "</td></tr>";
    }).join("");
    host.innerHTML = "<table><thead><tr><th>System</th><th>geomean_common</th><th>queries</th><th>own geomean</th><th>queries used/total</th><th>plain baseline</th></tr></thead><tbody>" + rows + "</tbody></table>";
  }
  function draw(el) {
    var id = el.getAttribute("data-chart"), build = B[id];
    var inst = instances.filter(function (i) { return i.el === el; })[0];
    var opt = build ? build() : null;
    if (!opt) {
      if (inst) { inst.chart.dispose(); instances.splice(instances.indexOf(inst), 1); }
      el.innerHTML = '<p class="note">No data at this scale factor.</p>';
      return;
    }
    if (!inst) {
      el.innerHTML = "";
      inst = {el: el, chart: echarts.init(el)};
      instances.push(inst);
    }
    var chart = inst.chart, name = opt.toolbox.feature.saveAsImage.name;
    opt.toolbox.feature.mySaveSvg.onclick = function () { exportSvg(chart, name); };
    chart.setOption(opt, true);
  }
  function drawAll() {
    beforeDraw.forEach(function (fn) { fn(); });
    Array.prototype.forEach.call(document.querySelectorAll(".chart[data-chart]"), draw);
    overheadTable();
    Array.prototype.forEach.call(document.querySelectorAll("[data-sf]"), function (el) {
      el.hidden = el.getAttribute("data-sf") !== state.sf;
    });
    afterDraw.forEach(function (fn) { fn(); });
    saveHash();
  }
  /* ---- the view in the URL: #sf=10&hide=a,b&metric=cold&<page keys> ---- */
  function saveHash() {
    var parts = [];
    if (state.sf && state.sf !== defaultSf) parts.push("sf=" + encodeURIComponent(state.sf));
    var hide = Object.keys(state.hidden).filter(function (k) { return state.hidden[k]; });
    if (hide.length) parts.push("hide=" + hide.map(encodeURIComponent).join(","));
    if (state.metric !== "warm") parts.push("metric=" + state.metric);
    Object.keys(state.extra).sort().forEach(function (k) {
      if (state.extra[k]) parts.push(encodeURIComponent(k) + "=" + encodeURIComponent(state.extra[k]));
    });
    var h = parts.length ? "#" + parts.join("&") : "";
    // keep a plain anchor (index.html#findings) while the view is the default one
    if (!h && window.location.hash.indexOf("=") < 0) return;
    if (h !== window.location.hash && window.history.replaceState) {
      window.history.replaceState(null, "", window.location.pathname + window.location.search + h);
    }
  }
  function decode(s) { try { return decodeURIComponent(s); } catch (e) { return ""; } }
  function readHash() {
    var out = {};
    window.location.hash.replace(/^#/, "").split("&").forEach(function (kv) {
      if (kv.indexOf("=") < 0) return;
      var i = kv.indexOf("="), k = decode(kv.slice(0, i));
      if (k) out[k] = decode(kv.slice(i + 1));
    });
    return out;
  }
  function control(host, html) { var d = document.createElement("span"); d.innerHTML = html; host.appendChild(d); return d; }
  function buildControls() {
    var host = document.getElementById("controls");
    if (!host) return;
    var wants = host.getAttribute("data-controls").split(",");
    var sfs = allSfs();
    if (wants.indexOf("sf") >= 0) {
      var d = control(host, '<label>Scale factor <select id="sf">' + sfs.map(function (s) {
        return '<option value="' + s + '"' + (s === state.sf ? " selected" : "") + ">SF" + s + "</option>"; }).join("") + "</select></label>");
      d.querySelector("select").onchange = function (e) { state.sf = e.target.value; drawAll(); };
    }
    if (wants.indexOf("metric") >= 0) {
      var m = control(host, '<label>Per-query time <select><option value="warm">warm median</option><option value="cold"' +
        (state.metric === "cold" ? " selected" : "") + '>cold first run</option></select></label>');
      m.querySelector("select").onchange = function (e) { state.metric = e.target.value; drawAll(); };
    }
    if (wants.indexOf("systems") >= 0) {
      allSystems().forEach(function (s) {
        var off = !!state.hidden[s.name];
        var c = control(host, '<button type="button" class="chip' + (off ? " off" : "") + '" aria-pressed="' + String(!off) +
          '"><i style="background:' + s.color + '"></i>' + esc(s.label) + "</button>");
        var b = c.firstChild;
        b.onclick = function () {
          state.hidden[s.name] = !state.hidden[s.name];
          b.classList.toggle("off", !!state.hidden[s.name]);
          b.setAttribute("aria-pressed", String(!state.hidden[s.name]));
          drawAll();
        };
      });
    }
  }
  function allSfs() {
    var seen = {};
    Object.keys(DATA.power || {}).concat(Object.keys((DATA.parquet || {}).power || {})).forEach(function (s) { seen[s] = 1; });
    return Object.keys(seen).sort(function (a, b) { return a - b; });
  }
  function init() {
    var sfs = allSfs().map(Number);
    var h = readHash();
    defaultSf = sfs.length ? String(sfs[sfs.length - 1]) : null;
    state.sf = sfs.length ? String(sfs.indexOf(Number(h.sf)) >= 0 ? Number(h.sf) : sfs[sfs.length - 1]) : null;
    (h.hide || "").split(",").forEach(function (n) { if (n && sysOf(n)) state.hidden[n] = true; });
    if (h.metric === "cold") state.metric = "cold";
    ["sf", "hide", "metric"].forEach(function (k) { delete h[k]; });
    state.extra = h;
    buildControls();
    drawAll();
    window.addEventListener("resize", function () {
      instances.forEach(function (i) { i.chart.resize(); });
      drawAll();
    });
  }
  /* Page scripts loaded after this one (explore.js) add builders and hooks before init runs. */
  window.DBENCH = {DATA: DATA, state: state, B: B, esc: esc, fmt: fmt, wrap: wrap, grouped: grouped,
    perSystem: perSystem, lines: lines, systems: systems, sysOf: sysOf, bySf: bySf, drawAll: drawAll,
    saveHash: saveHash, beforeDraw: beforeDraw, afterDraw: afterDraw, CLASS_COLORS: CLASS_COLORS};
  if (document.readyState === "complete") init();
  else window.addEventListener("load", init);
})();
