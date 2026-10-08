// Gedeelde helpers voor teller en beheer.
"use strict";

const App = {
  // Verschil tussen serverklok en lokale klok; gemeten bij elke poll, de meting met de kortste
  // round-trip is de betrouwbaarste.
  offset: 0,
  samples: [],
  lastContact: 0,

  serverNow() {
    return Date.now() + this.offset;
  },

  async api(path, body) {
    const t0 = Date.now();
    let res;
    try {
      res = await fetch(path, {
        method: body === undefined ? "GET" : "POST",
        headers: body === undefined ? {} : { "Content-Type": "application/json" },
        body: body === undefined ? undefined : JSON.stringify(body),
        cache: "no-store",
        credentials: "same-origin",
      });
    } catch (e) {
      const err = new Error("Geen verbinding met de server");
      err.status = 0;
      throw err;
    }
    const t1 = Date.now();
    let data = {};
    try { data = await res.json(); } catch (e) { /* lege body */ }
    if (res.status === 401 && path !== "/api/login") {
      location.href = "/login?next=" + encodeURIComponent(location.pathname);
    }
    if (!res.ok) {
      const err = new Error(data.error || "Fout " + res.status);
      err.status = res.status;
      throw err;
    }
    this.lastContact = t1;
    const st = data.now !== undefined ? data : data.state;
    if (st && typeof st.now === "number") this.clockSample(t0, t1, st.now);
    return data;
  },

  clockSample(t0, t1, serverNow) {
    this.samples.push({ rtt: t1 - t0, offset: serverNow - (t0 + t1) / 2 });
    if (this.samples.length > 15) this.samples.shift();
    let best = this.samples[0];
    for (const s of this.samples) if (s.rtt < best.rtt) best = s;
    this.offset = best.offset;
  },

  connected() {
    return Date.now() - this.lastContact < 7000;
  },
};

function pad(n) { return String(n).padStart(2, "0"); }

// Altijd u:mm:ss (voor de eventklok).
function fmtClock(ms) {
  if (ms == null || ms < 0) ms = 0;
  const s = Math.floor(ms / 1000);
  return Math.floor(s / 3600) + ":" + pad(Math.floor(s / 60) % 60) + ":" + pad(s % 60);
}

// m:ss, of u:mm:ss vanaf een uur (voor rondetijden).
function fmtDur(ms) {
  if (ms == null) return "–";
  if (ms < 0) ms = 0;
  const s = Math.floor(ms / 1000);
  if (s >= 3600) return fmtClock(ms);
  return Math.floor(s / 60) + ":" + pad(s % 60);
}

function fmtTime(ms) {
  if (!ms) return "";
  const d = new Date(ms);
  return pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds());
}

function uid() {
  if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
  return Date.now().toString(36) + "-" + Math.random().toString(36).slice(2) + Math.random().toString(36).slice(2);
}

// Kleine DOM-builder; tekst gaat altijd via textContent (geen HTML-injectie).
function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "class") el.className = v;
    else if (k === "value") el.value = v;
    else if (v === true) el.setAttribute(k, "");
    else el.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c == null || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

let errorTimer = null;
function showError(msg) {
  const bar = document.getElementById("error");
  if (!bar) return;
  bar.textContent = msg;
  bar.classList.add("show");
  clearTimeout(errorTimer);
  errorTimer = setTimeout(() => bar.classList.remove("show"), 5000);
}

// Knop die je twee keer moet indrukken (in plaats van een blokkerend confirm()-venster).
function armedButton(label, armedLabel, onConfirm, cls) {
  let timer = null;
  const btn = h("button", { class: cls || "", type: "button" }, label);
  btn.addEventListener("click", () => {
    if (btn.classList.contains("armed")) {
      clearTimeout(timer);
      btn.classList.remove("armed");
      btn.textContent = label;
      onConfirm();
      return;
    }
    btn.classList.add("armed");
    btn.textContent = armedLabel;
    timer = setTimeout(() => { btn.classList.remove("armed"); btn.textContent = label; }, 3000);
  });
  return btn;
}

async function logout() {
  try { await App.api("/api/logout", {}); } catch (e) { /* negeren */ }
  location.href = "/login";
}
