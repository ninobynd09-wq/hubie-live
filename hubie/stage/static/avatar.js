// Placeholder avatar: a plain drawn stand-in, to be replaced by the official art pack.
// The stage only uses three calls, so a sprite-based avatar can drop in behind them:
//   Avatar.mount(el)  Avatar.setMouth(0..1)  Avatar.setMood("idle|happy|excited|tired|serious")
window.Avatar = (function () {
  let mouth, lidL, lidR, head, brow;
  const svg = `<svg viewBox="0 0 230 240" width="230" height="240" xmlns="http://www.w3.org/2000/svg">
    <path d="M40 240 C45 180 70 150 115 150 C160 150 185 180 190 240 Z" fill="#f4f1ea"/>
    <path d="M100 160 L115 205 L130 160 Z" fill="#14111d"/>
    <path d="M115 165 L95 153 L95 178 Z M115 165 L135 153 L135 178 Z" fill="#b5322f"/>
    <g id="head">
      <ellipse cx="115" cy="100" rx="66" ry="58" fill="#9c8468"/>
      <ellipse cx="115" cy="108" rx="50" ry="42" fill="#d9c8ab"/>
      <ellipse cx="88" cy="96" rx="20" ry="15" fill="#5a4636" transform="rotate(-14 88 96)"/>
      <ellipse cx="142" cy="96" rx="20" ry="15" fill="#5a4636" transform="rotate(14 142 96)"/>
      <circle cx="90" cy="96" r="7" fill="#14111d"/><circle cx="140" cy="96" r="7" fill="#14111d"/>
      <rect id="lidL" x="78" y="86" width="24" height="9" fill="#5a4636"/>
      <rect id="lidR" x="128" y="86" width="24" height="9" fill="#5a4636"/>
      <ellipse cx="115" cy="114" rx="9" ry="6" fill="#3a2a20"/>
      <ellipse id="mouth" cx="115" cy="131" rx="15" ry="2" fill="#3a2a20"/>
      <rect x="66" y="42" width="98" height="9" rx="3" fill="#2b2740" stroke="#6d6488"/>
      <rect x="82" y="2" width="66" height="42" rx="3" fill="#2b2740" stroke="#6d6488"/>
      <rect x="82" y="34" width="66" height="7" fill="#b5322f"/>
      <path id="brow" d="M78 78 L102 82 M128 82 L152 78" stroke="#3a2a20" stroke-width="4" stroke-linecap="round"/>
    </g></svg>`;
  function mount(el) {
    el.innerHTML = svg;
    mouth = el.querySelector("#mouth"); lidL = el.querySelector("#lidL"); lidR = el.querySelector("#lidR");
    head = el.querySelector("#head"); brow = el.querySelector("#brow");
  }
  let lastRy = -1;
  function setMouth(v) {
    const ry = 2 + Math.round(v * 12);
    if (ry !== lastRy) { mouth.setAttribute("ry", ry); lastRy = ry; }
  }
  function setLids(h) { lidL.setAttribute("height", h); lidR.setAttribute("height", h); }
  function setMood(m) {
    setLids(m === "excited" ? 3 : m === "tired" ? 13 : 9);
    brow.setAttribute("d", m === "serious" ? "M78 82 L102 78 M128 78 L152 82" :
      m === "excited" ? "M78 72 L102 74 M128 74 L152 72" : "M78 78 L102 82 M128 82 L152 78");
  }
  function sway(t) {
    const a = Math.sin(t / 2600) * 2.2;
    head.setAttribute("transform", `rotate(${a.toFixed(1)} 115 130)`);
  }
  return { mount, setMouth, setMood, setLids, sway };
})();
