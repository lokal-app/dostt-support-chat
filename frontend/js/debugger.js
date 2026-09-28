/* ---------------------------------------------------------------------
   In-app debug panel for embedded WebViews with no DevTools access.
   Intercepts: fetch, XMLHttpRequest, console.*, WebSocket, and exposes
   a floating panel toggled by ?debug=1 in the URL or a 5-tap gesture
   on the header.
   Load this BEFORE other scripts so the monkey-patches catch everything.
--------------------------------------------------------------------- */
(function () {
  "use strict";

  /* ---------------------- Activation gate ---------------------- */
  var params = new URLSearchParams(location.search);
  var debugEnabled = params.get("debug") === "1";
  // Also allow activating via 5 rapid taps on the header (useful when
  // you can't control the URL from the host app).
  var tapCount = 0;
  var tapTimer = null;
  document.addEventListener("click", function (e) {
    if (debugEnabled) return;
    var header = document.querySelector(".chat-header");
    if (!header || !header.contains(e.target)) { tapCount = 0; return; }
    tapCount++;
    clearTimeout(tapTimer);
    tapTimer = setTimeout(function () { tapCount = 0; }, 800);
    if (tapCount >= 5) { tapCount = 0; debugEnabled = true; buildUI(); }
  });

  /* ---------------------- Log store ---------------------- */
  var MAX_ENTRIES = 300;
  var logs = { network: [], console: [], ws: [] };

  function pushLog(category, entry) {
    entry._ts = Date.now();
    var arr = logs[category];
    arr.push(entry);
    if (arr.length > MAX_ENTRIES) arr.shift();
    renderIfOpen(category);
  }

  /* -------------------- Intercept: fetch -------------------- */
  var origFetch = window.fetch;
  window.fetch = function (input, init) {
    var method = (init && init.method) || "GET";
    var url = typeof input === "string" ? input : (input && input.url) || String(input);
    var entry = { method: method.toUpperCase(), url: url, status: "...", duration: null, reqBody: null, resBody: null, error: null };
    try { entry.reqBody = init && init.body ? truncate(String(init.body), 2000) : null; } catch (e) { /* */ }
    var t0 = performance.now();
    pushLog("network", entry);
    return origFetch.apply(this, arguments).then(function (res) {
      entry.status = res.status;
      entry.duration = Math.round(performance.now() - t0);
      res.clone().text().then(function (t) { entry.resBody = truncate(t, 4000); renderIfOpen("network"); }).catch(function () {});
      return res;
    }).catch(function (err) {
      entry.status = "ERR";
      entry.error = err.message || String(err);
      entry.duration = Math.round(performance.now() - t0);
      renderIfOpen("network");
      throw err;
    });
  };

  /* -------------------- Intercept: XMLHttpRequest -------------------- */
  var origXHROpen = XMLHttpRequest.prototype.open;
  var origXHRSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (method, url) {
    this._dbg = { method: method.toUpperCase(), url: url };
    return origXHROpen.apply(this, arguments);
  };
  XMLHttpRequest.prototype.send = function (body) {
    if (this._dbg) {
      var entry = { method: this._dbg.method, url: this._dbg.url, status: "...", duration: null, reqBody: body ? truncate(String(body), 2000) : null, resBody: null, error: null };
      var t0 = performance.now();
      var xhr = this;
      pushLog("network", entry);
      xhr.addEventListener("loadend", function () {
        entry.status = xhr.status || "ERR";
        entry.duration = Math.round(performance.now() - t0);
        try { entry.resBody = truncate(xhr.responseText, 4000); } catch (e) { /* */ }
        renderIfOpen("network");
      });
    }
    return origXHRSend.apply(this, arguments);
  };

  /* -------------------- Intercept: console -------------------- */
  var levels = ["log", "info", "warn", "error", "debug"];
  var origConsole = {};
  levels.forEach(function (lvl) {
    origConsole[lvl] = console[lvl];
    console[lvl] = function () {
      origConsole[lvl].apply(console, arguments);
      var parts = [];
      for (var i = 0; i < arguments.length; i++) {
        try { parts.push(typeof arguments[i] === "string" ? arguments[i] : JSON.stringify(arguments[i], null, 1)); }
        catch (e) { parts.push(String(arguments[i])); }
      }
      pushLog("console", { level: lvl, text: truncate(parts.join(" "), 2000) });
    };
  });

  /* -------------------- Intercept: WebSocket -------------------- */
  var OrigWS = window.WebSocket;
  if (OrigWS) {
    window.WebSocket = function (url, protocols) {
      pushLog("ws", { type: "open", url: url, data: null, dir: null });
      var ws = protocols ? new OrigWS(url, protocols) : new OrigWS(url);
      ws.addEventListener("message", function (e) { pushLog("ws", { type: "msg", url: url, dir: "recv", data: truncate(String(e.data), 2000) }); });
      ws.addEventListener("close", function (e) { pushLog("ws", { type: "close", url: url, dir: null, data: "code=" + e.code + " reason=" + (e.reason || "(none)") }); });
      ws.addEventListener("error", function () { pushLog("ws", { type: "error", url: url, dir: null, data: null }); });
      var origSend = ws.send.bind(ws);
      ws.send = function (data) { pushLog("ws", { type: "msg", url: url, dir: "send", data: truncate(String(data), 2000) }); return origSend(data); };
      return ws;
    };
    window.WebSocket.prototype = OrigWS.prototype;
    window.WebSocket.CONNECTING = OrigWS.CONNECTING;
    window.WebSocket.OPEN = OrigWS.OPEN;
    window.WebSocket.CLOSING = OrigWS.CLOSING;
    window.WebSocket.CLOSED = OrigWS.CLOSED;
  }

  /* -------------------- Helpers -------------------- */
  function truncate(s, max) { return s && s.length > max ? s.slice(0, max) + "..." : s; }
  function fmtTime(ts) { var d = new Date(ts); return pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds()) + "." + String(d.getMilliseconds()).padStart(3, "0"); }
  function pad(n) { return n < 10 ? "0" + n : String(n); }
  function esc(s) { if (!s) return ""; var d = document.createElement("div"); d.textContent = s; return d.innerHTML; }

  /* -------------------- UI -------------------- */
  var panel = null;
  var activeTab = "network";
  var expanded = {}; // keyed by entry index for network detail expand

  function buildUI() {
    if (panel) { panel.style.display = "flex"; render(); return; }

    // Inject styles
    var style = document.createElement("style");
    style.textContent = [
      "#dbgPanel{position:fixed;bottom:0;left:0;right:0;top:0;z-index:100000;display:flex;flex-direction:column;background:#1a1a2e;color:#e0e0e0;font:11px/1.45 'SF Mono',SFMono-Regular,Consolas,'Liberation Mono',Menlo,monospace;border-top:2px solid #8b6bff;box-shadow:0 -4px 20px rgba(0,0,0,.5)}",
      "#dbgPanel.half{top:40vh}",
      "#dbgPanel *{box-sizing:border-box}",
      "#dbgBar{display:flex;align-items:center;background:#16162a;border-bottom:1px solid #2a2a4a;flex-shrink:0}",
      "#dbgBar button{background:none;border:none;color:#aaa;font:inherit;padding:7px 12px;cursor:pointer;border-bottom:2px solid transparent}",
      "#dbgBar button.active{color:#c9b8ff;border-bottom-color:#8b6bff}",
      "#dbgBar button:hover{color:#fff}",
      "#dbgBar .spacer{flex:1}",
      "#dbgBar .dbg-close{font-size:16px;padding:7px 10px;color:#888}",
      "#dbgBar .dbg-close:hover{color:#ff6b6b}",
      "#dbgContent{flex:1;overflow-y:auto;padding:0}",
      "#dbgContent::-webkit-scrollbar{width:5px}",
      "#dbgContent::-webkit-scrollbar-thumb{background:#2a2a4a;border-radius:3px}",
      ".dbg-row{padding:5px 10px;border-bottom:1px solid #1f1f3a;display:flex;align-items:flex-start;gap:6px;flex-wrap:wrap;word-break:break-all}",
      ".dbg-row:hover{background:#1f1f3a}",
      ".dbg-row .ts{color:#666;flex-shrink:0;min-width:75px}",
      ".dbg-row .method{font-weight:700;min-width:36px;flex-shrink:0}",
      ".dbg-row .url{color:#9ec5ff;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis}",
      ".dbg-row .status{min-width:32px;text-align:right;font-weight:700;flex-shrink:0}",
      ".dbg-row .status.s2{color:#6bffa0}",
      ".dbg-row .status.s3{color:#ffe66b}",
      ".dbg-row .status.s4{color:#ff9e6b}",
      ".dbg-row .status.s5,.dbg-row .status.err{color:#ff6b6b}",
      ".dbg-row .dur{color:#888;min-width:44px;text-align:right;flex-shrink:0}",
      ".dbg-row .badge{display:inline-block;padding:1px 5px;border-radius:3px;font-size:10px;font-weight:700;margin-right:4px}",
      ".dbg-row .badge.log{color:#aaa;background:#2a2a3a}",
      ".dbg-row .badge.info{color:#6bc5ff;background:#162a3a}",
      ".dbg-row .badge.warn{color:#ffc56b;background:#3a2e16}",
      ".dbg-row .badge.error{color:#ff6b6b;background:#3a1616}",
      ".dbg-row .badge.debug{color:#888;background:#222}",
      ".dbg-row .msg{flex:1;white-space:pre-wrap;min-width:0}",
      ".dbg-row .ws-type{font-weight:700}",
      ".dbg-row .ws-type.open{color:#6bffa0}",
      ".dbg-row .ws-type.close{color:#ff9e6b}",
      ".dbg-row .ws-type.error{color:#ff6b6b}",
      ".dbg-row .ws-dir{font-size:10px}",
      ".dbg-row .ws-dir.send{color:#ffc56b}",
      ".dbg-row .ws-dir.recv{color:#6bc5ff}",
      ".dbg-detail{padding:4px 10px 8px 92px;font-size:10.5px;color:#999;white-space:pre-wrap;word-break:break-all;border-bottom:1px solid #1f1f3a;background:#15152a;cursor:default}",
      ".dbg-detail b{color:#ccc;font-weight:600}",
      ".dbg-row.clickable{cursor:pointer}",
      ".dbg-empty{padding:30px;text-align:center;color:#555}",
      "#dbgFab{position:fixed;bottom:70px;right:12px;z-index:99999;width:40px;height:40px;border-radius:50%;background:#8b6bff;color:#fff;border:none;font-size:16px;cursor:grab;box-shadow:0 2px 10px rgba(139,107,255,.4);display:flex;align-items:center;justify-content:center;line-height:1;touch-action:none;user-select:none;-webkit-user-select:none}",
      "#dbgFab:active{cursor:grabbing}",
      "#dbgBar .dbg-size{font-size:13px;padding:7px 10px;color:#888}",
      "#dbgBar .dbg-size:hover{color:#c9b8ff}",
    ].join("\n");
    document.head.appendChild(style);

    // FAB — draggable
    var fab = document.createElement("button");
    fab.id = "dbgFab";
    fab.innerHTML = "&#x1f41b;";
    fab.title = "Toggle debug panel";
    document.body.appendChild(fab);

    // FAB drag logic (touch + mouse)
    var fabDragging = false;
    var fabMoved = false;
    var fabStartX = 0, fabStartY = 0, fabOrigX = 0, fabOrigY = 0;

    function fabPointerDown(e) {
      e.preventDefault();
      fabDragging = true;
      fabMoved = false;
      var pt = e.touches ? e.touches[0] : e;
      fabStartX = pt.clientX;
      fabStartY = pt.clientY;
      var rect = fab.getBoundingClientRect();
      fabOrigX = rect.left;
      fabOrigY = rect.top;
      document.addEventListener("mousemove", fabPointerMove);
      document.addEventListener("mouseup", fabPointerUp);
      document.addEventListener("touchmove", fabPointerMove, { passive: false });
      document.addEventListener("touchend", fabPointerUp);
    }
    function fabPointerMove(e) {
      if (!fabDragging) return;
      e.preventDefault();
      var pt = e.touches ? e.touches[0] : e;
      var dx = pt.clientX - fabStartX;
      var dy = pt.clientY - fabStartY;
      if (Math.abs(dx) > 4 || Math.abs(dy) > 4) fabMoved = true;
      var newX = Math.max(0, Math.min(fabOrigX + dx, window.innerWidth - 44));
      var newY = Math.max(0, Math.min(fabOrigY + dy, window.innerHeight - 44));
      fab.style.left = newX + "px";
      fab.style.top = newY + "px";
      fab.style.right = "auto";
      fab.style.bottom = "auto";
    }
    function fabPointerUp() {
      fabDragging = false;
      document.removeEventListener("mousemove", fabPointerMove);
      document.removeEventListener("mouseup", fabPointerUp);
      document.removeEventListener("touchmove", fabPointerMove);
      document.removeEventListener("touchend", fabPointerUp);
      if (!fabMoved) togglePanel();
    }
    fab.addEventListener("mousedown", fabPointerDown);
    fab.addEventListener("touchstart", fabPointerDown, { passive: false });

    // Panel
    var isHalf = false;
    panel = document.createElement("div");
    panel.id = "dbgPanel";
    panel.innerHTML = [
      '<div id="dbgBar">',
        '<button class="active" data-tab="network">Network</button>',
        '<button data-tab="console">Console</button>',
        '<button data-tab="ws">WebSocket</button>',
        '<div class="spacer"></div>',
        '<button class="dbg-clear" title="Clear">Clear</button>',
        '<button class="dbg-size" title="Toggle size">&#x25BD;</button>',
        '<button class="dbg-close" title="Close">&times;</button>',
      '</div>',
      '<div id="dbgContent"></div>',
    ].join("");
    document.body.appendChild(panel);

    // Tab switching
    panel.querySelectorAll("#dbgBar button[data-tab]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        activeTab = btn.dataset.tab;
        expanded = {};
        panel.querySelectorAll("#dbgBar button[data-tab]").forEach(function (b) { b.classList.toggle("active", b === btn); });
        render();
      });
    });
    panel.querySelector(".dbg-close").addEventListener("click", function () { panel.style.display = "none"; });
    panel.querySelector(".dbg-clear").addEventListener("click", function () { logs[activeTab] = []; expanded = {}; render(); });

    // Minimize / maximize toggle
    var sizeBtn = panel.querySelector(".dbg-size");
    sizeBtn.addEventListener("click", function () {
      isHalf = !isHalf;
      panel.classList.toggle("half", isHalf);
      sizeBtn.innerHTML = isHalf ? "&#x25B3;" : "&#x25BD;";
      sizeBtn.title = isHalf ? "Full screen" : "Half screen";
    });

    render();
  }

  function togglePanel() {
    if (!panel) { buildUI(); return; }
    var show = panel.style.display === "none";
    panel.style.display = show ? "flex" : "none";
    if (show) render();
  }

  /* -------------------- Rendering -------------------- */
  function renderIfOpen(category) {
    if (!panel || panel.style.display === "none") return;
    if (category !== activeTab) return;
    render();
  }

  function render() {
    var container = document.getElementById("dbgContent");
    if (!container) return;
    var arr = logs[activeTab];
    if (!arr.length) { container.innerHTML = '<div class="dbg-empty">No ' + activeTab + ' activity yet</div>'; return; }

    var html = [];
    if (activeTab === "network") {
      for (var i = arr.length - 1; i >= 0; i--) {
        var e = arr[i];
        var sc = statusClass(e.status);
        html.push('<div class="dbg-row clickable" data-idx="' + i + '">');
        html.push('<span class="ts">' + fmtTime(e._ts) + '</span>');
        html.push('<span class="method">' + esc(e.method) + '</span>');
        html.push('<span class="url">' + esc(shortUrl(e.url)) + '</span>');
        html.push('<span class="status ' + sc + '">' + esc(String(e.status)) + '</span>');
        html.push('<span class="dur">' + (e.duration != null ? e.duration + "ms" : "") + '</span>');
        html.push('</div>');
        if (expanded[i]) {
          html.push('<div class="dbg-detail">');
          html.push('<b>URL:</b> ' + esc(e.url) + '\n');
          if (e.reqBody) html.push('<b>Request:</b>\n' + esc(tryPrettyJson(e.reqBody)) + '\n');
          if (e.resBody) html.push('<b>Response:</b>\n' + esc(tryPrettyJson(e.resBody)) + '\n');
          if (e.error) html.push('<b>Error:</b> ' + esc(e.error) + '\n');
          html.push('</div>');
        }
      }
    } else if (activeTab === "console") {
      for (var j = arr.length - 1; j >= 0; j--) {
        var c = arr[j];
        html.push('<div class="dbg-row">');
        html.push('<span class="ts">' + fmtTime(c._ts) + '</span>');
        html.push('<span class="badge ' + c.level + '">' + c.level.toUpperCase() + '</span>');
        html.push('<span class="msg">' + esc(c.text) + '</span>');
        html.push('</div>');
      }
    } else if (activeTab === "ws") {
      for (var k = arr.length - 1; k >= 0; k--) {
        var w = arr[k];
        html.push('<div class="dbg-row">');
        html.push('<span class="ts">' + fmtTime(w._ts) + '</span>');
        html.push('<span class="ws-type ' + w.type + '">' + w.type.toUpperCase() + '</span>');
        if (w.dir) html.push('<span class="ws-dir ' + w.dir + '">' + (w.dir === "send" ? "&#9650;" : "&#9660;") + '</span>');
        html.push('<span class="url">' + esc(shortUrl(w.url)) + '</span>');
        if (w.data) html.push('<span class="msg">' + esc(w.data) + '</span>');
        html.push('</div>');
      }
    }
    container.innerHTML = html.join("");

    // Click-to-expand for network rows
    if (activeTab === "network") {
      container.querySelectorAll(".dbg-row.clickable").forEach(function (row) {
        row.addEventListener("click", function () {
          var idx = Number(row.dataset.idx);
          expanded[idx] = !expanded[idx];
          render();
        });
      });
    }
  }

  function statusClass(s) {
    if (s === "ERR" || s === "...") return s === "ERR" ? "err" : "";
    var n = Number(s);
    if (n >= 200 && n < 300) return "s2";
    if (n >= 300 && n < 400) return "s3";
    if (n >= 400 && n < 500) return "s4";
    if (n >= 500) return "s5";
    return "";
  }

  function shortUrl(url) {
    try { var u = new URL(url, location.origin); return u.pathname + u.search; } catch (e) { return url; }
  }

  function tryPrettyJson(s) {
    try { return JSON.stringify(JSON.parse(s), null, 2); } catch (e) { return s; }
  }

  /* -------------------- Auto-init if ?debug=1 -------------------- */
  if (debugEnabled) {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", buildUI);
    } else {
      buildUI();
    }
  }
})();
