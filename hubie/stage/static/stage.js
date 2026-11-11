(function () {
  const $ = (id) => document.getElementById(id);
  Avatar.mount($("avatar")); Avatar.setMood("idle");
  let ws = null, raf = 0, speak = null, captionTimer = null;
  let frozen = false; // test hook only

  function esc(el, text) { el.textContent = text == null ? "" : String(text); }

  function setSegment(s) {
    if (!s) return;
    esc($("seg-title"), s.title || "");
    esc($("seg-sub"), s.game || s.topic || "");
    if (s.game) esc($("game-name"), s.game);
    if (s.kind && s.kind !== "play") { esc($("game-name"), s.title); esc($("big-mult"), "..."); $("big-mult").className = ""; esc($("round-note"), s.topic || "demo chips only"); }
  }
  function setRound(r) {
    if (!r) return;
    esc($("game-name"), r.game);
    const big = $("big-mult");
    esc(big, r.multiplier.toFixed(2) + "x");
    big.className = r.multiplier >= 10 ? "big" : (r.won ? "win" : "loss");
    esc($("round-note"), `bet ${r.bet} demo, paid ${r.payout}, demo balance ${r.balance}`);
  }
  function setHistory(h) {
    const box = $("history"); box.textContent = "";
    (h || []).slice().reverse().forEach((x) => {
      const d = document.createElement("span");
      d.className = "chip" + (x.won ? " win" : "");
      d.textContent = (x.multiplier == null ? 0 : x.multiplier).toFixed(1) + "x";
      box.appendChild(d);
    });
  }
  function setChat(list) {
    const ul = $("chat-list"); ul.textContent = "";
    (list || []).slice(-9).forEach((m) => {
      const li = document.createElement("li"), b = document.createElement("b");
      b.textContent = m.name + " "; li.appendChild(b); li.appendChild(document.createTextNode(m.text));
      ul.appendChild(li);
    });
  }
  let historyBuf = [], chatBuf = [];
  function onEvent(ev) {
    if (ev.type === "snapshot") {
      setSegment(ev.segment); setRound(ev.round);
      historyBuf = ev.history || []; chatBuf = ev.chat || [];
      setHistory(historyBuf); setChat(chatBuf);
    } else if (ev.type === "segment") setSegment(ev);
    else if (ev.type === "round") {
      setRound(ev); historyBuf.push(ev); historyBuf = historyBuf.slice(-14); setHistory(historyBuf);
    } else if (ev.type === "chat") { chatBuf.push(ev); chatBuf = chatBuf.slice(-9); setChat(chatBuf); }
    else if (ev.type === "speak") {
      speak = { t0: performance.now(), env: ev.env || [], dur: ev.duration || 1 };
      Avatar.setMood(ev.mood || "idle");
      esc($("caption"), ev.text);
      clearTimeout(captionTimer);
      captionTimer = setTimeout(() => esc($("caption"), ""), (ev.duration + 1.2) * 1000);
    } else if (ev.type === "test_blank" && window.__hooks) { document.body.style.display = "none"; }
    else if (ev.type === "test_freeze" && window.__hooks) { frozen = true; }
  }
  function connect() {
    try { ws = new WebSocket(`ws://${location.host}/ws`); } catch (e) { setTimeout(connect, 1000); return; }
    ws.onmessage = (m) => { try { onEvent(JSON.parse(m.data)); } catch (e) {} };
    ws.onclose = () => setTimeout(connect, 1000);
    ws.onerror = () => { try { ws.close(); } catch (e) {} };
  }
  fetch("/config").then((r) => r.json()).then((c) => { window.__hooks = !!c.test_hooks; }).catch(() => {});

  // Frame loop. The corner marker changes every second from inside rAF: the
  // watchdog watches for it in the encoded picture, so a hung renderer shows up
  // as a frozen probe even when other parts of the page look alive.
  let lastBlink = 0, lastBeat = 0, last = 0, lidsHold = 0;
  function frame(t) {
    requestAnimationFrame(frame);
    if (frozen) return;
    raf++;
    if (t - last < 40) return; // about 25 updates a second is plenty
    last = t;
    if (Math.floor(t / 1000) !== lastBeat) {
      lastBeat = Math.floor(t / 1000);
      $("beat").style.background = lastBeat % 2 ? "#2a2438" : "#14111d";
    }
    Avatar.sway(t);
    if (t > lastBlink) { Avatar.setLids(20); lidsHold = t + 130; lastBlink = t + 3000 + Math.random() * 4000; }
    else if (lidsHold && t > lidsHold) { Avatar.setLids(9); lidsHold = 0; }
    let m = 0;
    if (speak) {
      const pos = (performance.now() - speak.t0) / 50;
      if (pos >= speak.env.length) speak = null; else m = speak.env[Math.floor(pos)] || 0;
    }
    Avatar.setMouth(m);
  }
  requestAnimationFrame(frame);
  setInterval(() => {
    if (ws && ws.readyState === 1) ws.send(JSON.stringify({ type: "hb", raf: frozen ? -1 : raf }));
  }, 2000);
  connect();
})();
