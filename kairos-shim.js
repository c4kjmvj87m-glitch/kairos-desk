/* KAIROS op GitHub
   De pagina is exact dezelfde als de desk in Claude. Dit bestand vervangt alleen de onderdelen
   die Claude zelf levert: de database (de desk komt uit data/desk.json en data/runs.json),
   de gebruiker (naam en logboek op dit toestel), voorlezen (stem van het toestel) en
   Vraag de desk (je eigen Claude-sleutel, alleen op dit toestel bewaard). */
(function () {
  "use strict";
  if (window.claude) return;

  var POLL_MS = 180000;
  var ASK_MODEL = "claude-haiku-5-5";
  var LS = {
    get: function (k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
    set: function (k, v) { try { localStorage.setItem(k, v); return true; } catch (e) { return false; } },
    del: function (k) { try { localStorage.removeItem(k); } catch (e) {} }
  };

  // Eenmalig je naam zetten: open de app met ?naam=JouwNaam (leeg laten wist hem).
  try {
    var qp = new URLSearchParams(location.search);
    if (qp.has("naam")) {
      var nm = (qp.get("naam") || "").trim().slice(0, 24);
      if (nm) LS.set("kairos.naam", nm); else LS.del("kairos.naam");
      qp.delete("naam");
      var rest = qp.toString();
      history.replaceState(null, "", location.pathname + (rest ? "?" + rest : "") + location.hash);
    }
  } catch (e) {}

  function err(code, message, text) {
    var e = new Error(message || code);
    e.code = code;
    if (text) e.text = text;
    return e;
  }

  /* ---------- database: desk/latest en runs uit GitHub, logboek op dit toestel ---------- */
  var feed = {
    desk: {sig: null, data: undefined, cache: false, failed: false, subs: []},
    runs: {sig: null, list: undefined, cache: false, subs: []},
    busy: false, timer: null, started: false
  };

  function getJSON(path) {
    return fetch(path + "?t=" + Date.now(), {cache: "no-store"}).then(function (r) {
      if (r.status === 404) return {missing: true};
      if (!r.ok) throw err("http_" + r.status, "HTTP " + r.status);
      var cache = r.headers.get("X-Kairos-Cache") === "1";
      return r.json().then(function (j) { return {j: j, cache: cache}; });
    });
  }

  function deskSnap() {
    var d = feed.desk.data;
    return {exists: !!d, id: "latest", data: function () { return d || undefined; },
            metadata: {fromCache: feed.desk.cache, hasPendingWrites: false}};
  }

  function runsSnap(sub) {
    var list = (feed.runs.list || []).slice();
    if (sub.order) {
      var f = sub.order[0], desc = sub.order[1] === "desc";
      list.sort(function (a, b) {
        var x = a && a[f], y = b && b[f];
        if (x === y) return 0;
        if (x == null) return 1;
        if (y == null) return -1;
        return (x < y ? -1 : 1) * (desc ? -1 : 1);
      });
    }
    if (sub.lim) list = list.slice(0, sub.lim);
    var docs = list.map(function (r) { return {id: String((r && r.runId) || ""), exists: true, data: function () { return r; }}; });
    return {docs: docs, size: docs.length, empty: !docs.length, metadata: {fromCache: feed.runs.cache}};
  }

  function emitDesk() { feed.desk.subs.forEach(function (s) { try { s.ok(deskSnap()); } catch (e) { console.error(e); } }); }
  function emitRuns() { feed.runs.subs.forEach(function (s) { try { s.ok(runsSnap(s)); } catch (e) { console.error(e); } }); }

  function refresh() {
    if (feed.busy) return;
    feed.busy = true;
    var pd = getJSON("data/desk.json").then(function (r) {
      var d = r.missing ? null : r.j;
      var sig = d ? (d.runId || "") + "|" + (d.asof || "") + "|" + JSON.stringify(d).length : "none";
      var changed = sig !== feed.desk.sig || feed.desk.failed || (feed.desk.cache && !r.cache);
      feed.desk.sig = sig; feed.desk.data = d; feed.desk.cache = !!r.cache; feed.desk.failed = false;
      if (changed) emitDesk();
    }, function (e) {
      if (feed.desk.data === undefined) {
        feed.desk.failed = true;
        feed.desk.subs.forEach(function (s) { if (s.bad) try { s.bad(e); } catch (x) {} });
      }
    });
    var pr = getJSON("data/runs.json").then(function (r) {
      var list = r.missing ? [] : (Array.isArray(r.j) ? r.j : []);
      var last = list.length ? list[list.length - 1] : null;
      var sig = list.length + "|" + (last ? last.runId + last.asof : "");
      var changed = sig !== feed.runs.sig;
      feed.runs.sig = sig; feed.runs.list = list; feed.runs.cache = !!r.cache;
      if (changed) emitRuns();
    }, function () {});
    Promise.all([pd, pr]).then(function () { feed.busy = false; }, function () { feed.busy = false; });
  }

  function startFeed() {
    if (feed.started) return;
    feed.started = true;
    refresh();
    feed.timer = setInterval(function () { if (!document.hidden) refresh(); }, POLL_MS);
    document.addEventListener("visibilitychange", function () { if (!document.hidden) refresh(); });
    window.addEventListener("online", refresh);
  }

  var JKEY = "kairos.journal";
  var journalSubs = [];
  function journalSnap() {
    var raw = LS.get(JKEY), d = null;
    try { d = raw ? JSON.parse(raw) : null; } catch (e) { d = null; }
    return {exists: !!d, id: "journal", data: function () { return d || undefined; }, metadata: {fromCache: false, hasPendingWrites: false}};
  }
  window.addEventListener("storage", function (ev) {
    if (ev.key === JKEY) journalSubs.forEach(function (s) { try { s(journalSnap()); } catch (e) {} });
  });

  function docRef(path) {
    var p = String(path || "").replace(/^\/+|\/+$/g, "");
    if (p === "desk/latest") {
      return {
        id: "latest", path: p,
        onSnapshot: function (ok, bad) {
          var sub = {ok: ok, bad: bad};
          feed.desk.subs.push(sub);
          if (feed.desk.data !== undefined) setTimeout(function () { ok(deskSnap()); }, 0);
          startFeed();
          return function () { feed.desk.subs = feed.desk.subs.filter(function (x) { return x !== sub; }); };
        },
        get: function () { return Promise.resolve(deskSnap()); },
        set: function () { return Promise.reject(err("permission_denied", "alleen lezen")); }
      };
    }
    if (/\/journal$/.test(p)) {
      return {
        id: "journal", path: p,
        onSnapshot: function (ok) {
          journalSubs.push(ok);
          setTimeout(function () { ok(journalSnap()); }, 0);
          return function () { journalSubs = journalSubs.filter(function (x) { return x !== ok; }); };
        },
        get: function () { return Promise.resolve(journalSnap()); },
        set: function (body) {
          var txt;
          try { txt = JSON.stringify(body); } catch (e) { return Promise.reject(err("invalid", "ongeldig")); }
          if (!LS.set(JKEY, txt)) return Promise.reject(err("quota", "geen ruimte op dit toestel"));
          setTimeout(function () { journalSubs.forEach(function (s) { try { s(journalSnap()); } catch (e) {} }); }, 0);
          return Promise.resolve();
        }
      };
    }
    return {
      id: p.split("/").pop(), path: p,
      onSnapshot: function (ok) { setTimeout(function () { ok({exists: false, id: p, data: function () { return undefined; }, metadata: {fromCache: false}}); }, 0); return function () {}; },
      get: function () { return Promise.resolve({exists: false, data: function () { return undefined; }}); },
      set: function () { return Promise.reject(err("permission_denied", "alleen lezen")); }
    };
  }

  function query(path, order, lim) {
    return {
      orderBy: function (f, dir) { return query(path, [f, dir || "asc"], lim); },
      limit: function (n) { return query(path, order, n); },
      onSnapshot: function (ok, bad) {
        if (String(path).replace(/^\/+|\/+$/g, "") !== "runs") {
          setTimeout(function () { ok({docs: [], size: 0, empty: true, metadata: {fromCache: false}}); }, 0);
          return function () {};
        }
        var sub = {ok: ok, bad: bad, order: order, lim: lim};
        feed.runs.subs.push(sub);
        if (feed.runs.list !== undefined) setTimeout(function () { ok(runsSnap(sub)); }, 0);
        startFeed();
        return function () { feed.runs.subs = feed.runs.subs.filter(function (x) { return x !== sub; }); };
      },
      get: function () { return Promise.resolve(runsSnap({order: order, lim: lim})); }
    };
  }

  var db = {doc: docRef, collection: function (path) { return query(path, null, null); }};

  /* ---------- gebruiker: naam en id op dit toestel ---------- */
  function installed() {
    try { return window.navigator.standalone === true || window.matchMedia("(display-mode: standalone)").matches; } catch (e) { return false; }
  }
  var user = {
    id: function () { return Promise.resolve("toestel"); },
    me: function () {
      var n = LS.get("kairos.naam") || "";
      // Als app op het beginscherm heeft de app een eigen opslag (zeker op iPhone): vraag de naam daar één keer.
      if (n || !installed() || LS.get("kairos.naamGevraagd")) return Promise.resolve({name: n});
      return new Promise(function (resolve) {
        setTimeout(function () {
          var v = "";
          try { v = (window.prompt("Hoe mag de desk je noemen? Laat leeg om over te slaan.") || "").trim().slice(0, 24); } catch (e) {}
          LS.set("kairos.naamGevraagd", "1");
          if (v) LS.set("kairos.naam", v);
          resolve({name: v});
        }, 900);
      });
    }
  };

  /* ---------- voorlezen met de stem van het toestel ---------- */
  function makeTts() {
    var ss = window.speechSynthesis;
    if (!ss || typeof window.SpeechSynthesisUtterance === "undefined") return null;
    var subs = [], token = null;
    function emit(s) { subs.forEach(function (f) { try { f({state: s}); } catch (e) {} }); }
    function voice() {
      var vs = ss.getVoices() || [];
      var pick = function (re) { return vs.filter(function (v) { return re.test(v.lang || ""); }); };
      var nl = pick(/^nl[-_]BE/i).concat(pick(/^nl[-_]NL/i), pick(/^nl/i));
      return nl.filter(function (v) { return /premium|enhanced|natural|neural/i.test(v.name); })[0] || nl[0] || null;
    }
    try { ss.getVoices(); ss.addEventListener && ss.addEventListener("voiceschanged", function () {}); } catch (e) {}
    function chunks(text) {
      var parts = String(text || "").replace(/\s+/g, " ").trim().replace(/([.!?])\s+/g, "$1\u0001").split("\u0001"), out = [], cur = "";
      parts.forEach(function (s) {
        if (cur && (cur + " " + s).length > 220) { out.push(cur); cur = s; } else { cur = cur ? cur + " " + s : s; }
      });
      if (cur) out.push(cur);
      return out;
    }
    return {
      speak: function (lines) {
        try { ss.cancel(); } catch (e) {}
        var list = [];
        (Array.isArray(lines) ? lines : [lines]).forEach(function (x) { list = list.concat(chunks(typeof x === "string" ? x : (x && x.text))); });
        if (!list.length) return Promise.resolve();
        var me = token = {};
        return new Promise(function (resolve, reject) {
          var i = 0;
          function next() {
            if (token !== me) return resolve();
            if (i >= list.length) { token = null; emit("idle"); return resolve(); }
            var u = new SpeechSynthesisUtterance(list[i++]);
            var v = voice();
            u.lang = v ? v.lang : "nl-NL";
            if (v) u.voice = v;
            u.onend = next;
            u.onerror = function (ev) {
              if (token !== me) return resolve();
              token = null; emit("idle");
              if (ev && (ev.error === "interrupted" || ev.error === "canceled")) resolve();
              else reject(err("failed", "voorlezen mislukt"));
            };
            ss.speak(u);
          }
          emit("speaking");
          next();
        });
      },
      stop: function () { token = null; try { ss.cancel(); } catch (e) {} emit("idle"); },
      onState: function (f) { subs.push(f); return function () { subs = subs.filter(function (x) { return x !== f; }); }; }
    };
  }

  /* ---------- Vraag de desk: Claude via je eigen sleutel ---------- */
  function askKey() {
    var k = (LS.get("kairos.claudeKey") || "").trim();
    if (/^sk-ant-/.test(k)) return k;
    var v = window.prompt("Vraag de desk gebruikt je eigen Claude-sleutel (begint met sk-ant-). Plak hem hier. Hij blijft alleen op dit toestel en gaat alleen naar Claude.");
    v = (v || "").trim();
    if (!/^sk-ant-/.test(v)) return null;
    LS.set("kairos.claudeKey", v);
    return v;
  }

  function sample(turns, opts) {
    opts = opts || {};
    var key = askKey();
    if (!key) return Promise.reject(err("not_granted", "geen sleutel"));
    var msgs = [];
    (turns || []).forEach(function (t) {
      var role = t && t.role === "assistant" ? "assistant" : "user";
      var text = typeof (t && t.content) === "string" ? t.content : JSON.stringify(t && t.content);
      if (msgs.length && msgs[msgs.length - 1].role === role) msgs[msgs.length - 1].content += "\n\n" + text;
      else msgs.push({role: role, content: text});
    });
    if (!msgs.length || msgs[0].role !== "user") msgs.unshift({role: "user", content: "Vraag van de trader:"});
    var text = "", stop = null;
    var model = LS.get("kairos.askModel") || ASK_MODEL;
    return fetch("https://api.anthropic.com/v1/messages", {
      method: "POST", signal: opts.signal,
      headers: {"content-type": "application/json", "x-api-key": key, "anthropic-version": "2023-06-01",
                "anthropic-dangerous-direct-browser-access": "true"},
      body: JSON.stringify({model: model, max_tokens: 1500, stream: true, messages: msgs})
    }).then(function (r) {
      if (!r.ok) {
        return r.text().then(function (t) {
          if (r.status === 401 || r.status === 403) { LS.del("kairos.claudeKey"); throw err("session_expired", t); }
          if (r.status === 429 || r.status === 529) throw err("rate_limited", t);
          if (r.status === 413 || /prompt is too long/i.test(t)) throw err("prompt_too_large", t);
          if (r.status === 400 && /credit balance/i.test(t)) throw err("rate_limited", t);
          throw err("failed", t);
        });
      }
      if (!r.body || !r.body.getReader) {
        return r.text().then(function (all) {
          all.split("\n").forEach(function (l) {
            if (l.indexOf("data:") !== 0) return;
            try { var j = JSON.parse(l.slice(5)); if (j.delta && j.delta.type === "text_delta") text += j.delta.text; if (j.delta && j.delta.stop_reason) stop = j.delta.stop_reason; } catch (e) {}
          });
          return {text: text, truncated: stop === "max_tokens"};
        });
      }
      var rd = r.body.getReader(), dec = new TextDecoder(), buf = "";
      function handle(ev) {
        var data = ev.split("\n").filter(function (l) { return l.indexOf("data:") === 0; }).map(function (l) { return l.slice(5).trim(); }).join("");
        if (!data) return;
        var j;
        try { j = JSON.parse(data); } catch (e) { return; }
        if (j.type === "content_block_delta" && j.delta && j.delta.type === "text_delta") {
          text += j.delta.text;
          if (opts.onText) try { opts.onText({text: text}); } catch (e) {}
        } else if (j.type === "message_delta" && j.delta) {
          stop = j.delta.stop_reason || stop;
          if (stop === "refusal") throw err("refused", "geweigerd", text);
        } else if (j.type === "error") {
          var t = (j.error && j.error.type) || "";
          throw err(t === "overloaded_error" || t === "rate_limit_error" ? "rate_limited" : "failed", (j.error && j.error.message) || "fout", text);
        }
      }
      function pump() {
        return rd.read().then(function (x) {
          if (x.done) { if (buf.trim()) handle(buf); return; }
          buf += dec.decode(x.value, {stream: true});
          var parts = buf.split(/\r?\n\r?\n/);
          buf = parts.pop();
          parts.forEach(handle);
          return pump();
        });
      }
      return pump().then(function () { return {text: text, truncated: stop === "max_tokens"}; });
    }).catch(function (e) {
      if (e && (e.name === "AbortError" || (opts.signal && opts.signal.aborted))) throw err("cancelled", "gestopt", text);
      if (e && e.code) { if (text && !e.text) e.text = text; throw e; }
      throw err("failed", (e && e.message) || "netwerkfout", text);
    });
  }

  /* ---------- de runtime zoals de pagina hem gebruikt ---------- */
  var TTS = makeTts();
  window.claude = {
    use: function (name) {
      if (name === "db") return Promise.resolve(db);
      if (name === "user") return Promise.resolve(user);
      if (name === "tts") return Promise.resolve(TTS);
      if (name === "sample") return Promise.resolve(sample);
      return Promise.resolve(null);
    }
  };

  /* ---------- offline: de laatste desk blijft zichtbaar ---------- */
  if ("serviceWorker" in navigator && location.protocol === "https:") {
    window.addEventListener("load", function () { navigator.serviceWorker.register("sw.js").catch(function () {}); });
  }
})();
