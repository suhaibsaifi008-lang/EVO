"use strict";

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const log = document.getElementById("log");
let selectedVoice = null;
let wakeMode = false;
let recognition = null;
let currentAudio = null;
let lastProjectsRefresh = 0;

/* ---------- PIN-gated fetch ---------- */
(function wrapFetch() {
  const original = window.fetch.bind(window);
  window.fetch = async (input, init = {}) => {
    const pin = localStorage.getItem("evo_pin");
    if (pin) {
      init.headers = new Headers(init.headers || {});
      if (!init.headers.has("X-EVO-Pin")) init.headers.set("X-EVO-Pin", pin);
    }
    let res = await original(input, init);
    if (res.status === 401 && !String(input).includes("/api/health")) {
      const entered = prompt("This console is PIN protected.\nEnter access PIN:");
      if (entered) {
        localStorage.setItem("evo_pin", entered);
        init.headers = new Headers(init.headers || {});
        init.headers.set("X-EVO-Pin", entered);
        res = await original(input, init);
      }
    }
    return res;
  };
})();

function addMsg(text, who) {
  const hero = $("hero");
  if (hero) hero.style.display = "none";
  const div = document.createElement("div");
  div.className = `msg ${who}`;
  const label = document.createElement("span");
  label.className = "who";
  label.textContent = who === "you" ? "You" : "EVO";
  const body = document.createElement("span");
  body.textContent = text;
  div.append(label, body);
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
}

function browserSpeak(text) {
  if (!("speechSynthesis" in window) || !text) return;
  speechSynthesis.cancel();
  const u = new SpeechSynthesisUtterance(text);
  if (selectedVoice) u.voice = selectedVoice;
  u.rate = 1.02;
  u.pitch = 0.92;
  speechSynthesis.speak(u);
}

async function speak(text, tone) {
  if (!$("voiceOut").checked || !text) return;
  if (localStorage.getItem("evo_neural") !== "0") {
    try {
      const voice = encodeURIComponent(localStorage.getItem("evo_tts_voice") || "");
      const toneParam = tone ? `&tone=${encodeURIComponent(tone)}` : "";
      const res = await fetch(`/api/tts?text=${encodeURIComponent(text)}&voice=${voice}${toneParam}`);
      if (res.ok) {
        const blob = await res.blob();
        if (blob.size > 512) {
          if (currentAudio) { currentAudio.pause(); }
          currentAudio = new Audio(URL.createObjectURL(blob));
          currentAudio.play().catch(() => {});
          return;
        }
      }
    } catch (e) {}
  }
  browserSpeak(text);
}

async function send(text) {
  text = (text || "").trim();
  if (!text) return;
  addMsg(text, "you");
  const think = document.createElement("div");
  think.className = "msg jarvis thinking";
  think.innerHTML = "<i></i><i></i><i></i>";
  log.appendChild(think);
  log.scrollTop = log.scrollHeight;
  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
    const data = await res.json();
    think.remove();
    addMsg(data.reply, "jarvis");
    speak(data.reply, data.tone);
    if (Array.isArray(data.refresh)) data.refresh.forEach(refreshView);
  } catch (err) {
    think.remove();
    addMsg("Connection to the server was lost.", "jarvis");
  }
}

function toast(text) {
  const box = $("toasts");
  const t = document.createElement("div");
  t.className = "toast";
  t.textContent = text;
  box.appendChild(t);
  while (box.children.length > 4) box.firstElementChild.remove();
  setTimeout(() => t.remove(), 9000);
}

function fmtTime(ts) {
  return new Date(ts * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

/* ---------- brand & views ---------- */
function applyBrand(name) {
  const upper = String(name || "EVO").toUpperCase();
  document.title = `${upper.split("").join(".")} — Console`;
  $("brandName").textContent = upper.split("").join(".");
}
function setView(name) {
  document.querySelectorAll(".nav-item").forEach((b) => b.classList.toggle("active", b.dataset.view === name));
  document.querySelectorAll(".view").forEach((v) => v.classList.toggle("active", v.dataset.vname === name));
  $("viewTitle").textContent = name.charAt(0).toUpperCase() + name.slice(1);
  $("chatForm").classList.toggle("hidden-composer", name !== "console");
  $("sidebar").classList.remove("open");
  refreshView(name);
}
document.querySelectorAll(".nav-item").forEach((b) => b.addEventListener("click", () => setView(b.dataset.view)));
$("newChatBtn").addEventListener("click", () => {
  setView("console");
  log.innerHTML = "";
  const hero = $("hero");
  if (hero) hero.style.display = "";
});
$("menuBtn").addEventListener("click", () => $("sidebar").classList.toggle("open"));
$("telemetryToggle").addEventListener("click", () => document.body.classList.toggle("rail-hidden"));
if (window.innerWidth < 1180) document.body.classList.add("rail-hidden");

/* ---------- voices ---------- */
function pickVoice() {
  const voices = speechSynthesis.getVoices();
  if (!voices.length) return;
  const select = $("voiceSelect");
  if (select && !select.options.length) {
    const english = voices.filter((v) => v.lang.startsWith("en"));
    (english.length ? english : voices).forEach((v) => {
      const opt = document.createElement("option");
      opt.value = v.name;
      opt.textContent = `${v.name} (${v.lang})`;
      select.appendChild(opt);
    });
    const saved = localStorage.getItem("evo_browser_voice");
    if (saved) select.value = saved;
  }
  selectedVoice =
    voices.find((v) => v.name === ($("voiceSelect") ? $("voiceSelect").value : "")) ||
    voices.find((v) => v.lang === "en-GB") ||
    voices.find((v) => v.lang.startsWith("en")) ||
    null;
}
$("voiceSelect") && $("voiceSelect").addEventListener("change", () => {
  localStorage.setItem("evo_browser_voice", $("voiceSelect").value);
  pickVoice();
});

/* ---------- views refresh ---------- */
async function refreshView(name) {
  if (name === "tasks") {
    const items = await (await fetch("/api/reminders")).json();
    const list = $("reminderList");
    list.innerHTML = "";
    for (const r of items) {
      const li = document.createElement("li");
      const info = document.createElement("div");
      info.innerHTML = `<strong>${esc(r.message)}</strong><small>${esc(r.kind)} — due ${fmtTime(r.due_at)}</small>`;
      const del = document.createElement("button");
      del.textContent = "Cancel";
      del.onclick = async () => { await fetch(`/api/reminders/${r.id}`, { method: "DELETE" }); refreshView("tasks"); };
      li.append(info, del);
      list.appendChild(li);
    }
    if (!items.length) list.innerHTML = "<li>Nothing scheduled.</li>";
  }

  if (name === "projects") {
    const now = Date.now();
    if (now - lastProjectsRefresh < 1500) return;
    lastProjectsRefresh = now;
    const items = await (await fetch("/api/projects")).json();
    const list = $("projectList");
    list.innerHTML = "";
    for (const p of items) {
      const li = document.createElement("li");
      const info = document.createElement("div");
      info.innerHTML = `<strong>#${p.id} ${esc(p.goal)}</strong><small>${esc(p.preview || "working...")}</small>`;
      li.appendChild(info);
      const badge = document.createElement("span");
      badge.className = `proj-status ${p.status}`;
      badge.textContent = p.status;
      li.appendChild(badge);
      if (p.status === "running") {
        const stop = document.createElement("button");
        stop.textContent = "Stop";
        stop.onclick = async () => { await fetch(`/api/projects/${p.id}`, { method: "DELETE" }); refreshView("projects"); };
        li.appendChild(stop);
        watchProject(p.id);
        loadLog(p.id, li);
      }
      list.appendChild(li);
    }
    if (!items.length) list.innerHTML = "<li>No missions yet. Launch one below.</li>";
  }

  if (name === "knowledge") {
    const items = await (await fetch("/api/knowledge")).json();
    const list = $("knowledgeList");
    list.innerHTML = "";
    for (const k of items) {
      const li = document.createElement("li");
      const info = document.createElement("div");
      info.innerHTML = `<strong>${esc(k.topic)}</strong><small>${esc(k.preview || "")}</small>`;
      const del = document.createElement("button");
      del.textContent = "Purge";
      del.onclick = async () => { await fetch(`/api/knowledge/${k.id}`, { method: "DELETE" }); refreshView("knowledge"); };
      li.append(info, del);
      list.appendChild(li);
    }
    if (!items.length) list.innerHTML = "<li>Nothing studied yet. Try: learn about black holes.</li>";
  }

  if (name === "memory") {
    const items = await (await fetch("/api/memory")).json();
    const list = $("memoryList");
    list.innerHTML = "";
    for (const m of items) {
      const li = document.createElement("li");
      const info = document.createElement("div");
      info.innerHTML = `<strong>${esc(m.key)}</strong><small>${esc(m.value)}</small>`;
      const del = document.createElement("button");
      del.textContent = "Forget";
      del.onclick = async () => { await fetch(`/api/memory/${encodeURIComponent(m.key)}`, { method: "DELETE" }); refreshView("memory"); };
      li.append(info, del);
      list.appendChild(li);
    }
    if (!items.length) list.innerHTML = "<li>No memories stored yet.</li>";
  }

  if (name === "system") {
    const s = await (await fetch("/api/status")).json();
    const rows = [
      ["CPU", s.cpu_percent != null ? s.cpu_percent + "%" : "?"],
      ["RAM", s.ram_total_gb ? `${s.ram_used_gb} / ${s.ram_total_gb} GB` : "?"],
      ["Battery", s.battery_percent != null ? `${Math.round(s.battery_percent)}%${s.charging ? " (charging)" : ""}` : "n/a"],
      ["Uptime", s.uptime_hours != null ? Math.round(s.uptime_hours) + " h" : "?"],
    ];
    $("statusList").innerHTML = rows.map(([k, v]) => `<li><span>${k}</span><strong>${esc(String(v))}</strong></li>`).join("");
    try {
      const audit = await (await fetch("/api/audit?limit=12")).json();
      $("auditBox").innerHTML =
        `<h3 class="section-title" style="margin-top:14px;">Recent actions</h3>` +
        (audit.length
          ? `<ul class='list plain small'>` + audit.slice(0, 10).map((a) =>
              `<li><span>${esc(a.tool)}</span><strong>${a.outcome === "ok" ? "✓" : "✗"} ${esc((a.detail || "").slice(0, 44))}</strong></li>`).join("") + `</ul>`
          : "<p class='hint'>No actions logged yet.</p>");
    } catch {}
  }

  if (name === "setup") {
    const cfg = await (await fetch("/api/settings")).json();
    applyBrand(cfg.name || "EVO");
    $("briefingEnabled").checked = !!cfg.briefing_enabled;
    $("briefingTime").value = cfg.briefing_time || "08:00";
    $("cityInput").value = cfg.city || "";
    $("calUrl").value = cfg.calendar_ical_url || "";
    $("ambientVision").value = cfg.ambient_vision_min ?? 0;
    $("autoApprove").checked = !!cfg.auto_approve_code;
    $("deepMode").checked = !!cfg.deep_mode;
    $("guiAllowed").checked = !!cfg.gui_allowed;
    $("allowMailSend").checked = !!cfg.allow_mail_send;
    $("neuralVoice").checked = localStorage.getItem("evo_neural") !== "0";
    try {
      const vs = await (await fetch("/api/voices")).json();
      const sel = $("neuralVoiceSelect");
      if (!sel.options.length) {
        Object.entries(vs.voices || {}).forEach(([id, label]) => {
          const opt = document.createElement("option");
          opt.value = id;
          opt.textContent = label;
          sel.appendChild(opt);
        });
        const saved = localStorage.getItem("evo_tts_voice");
        if (saved) sel.value = saved;
        else if (vs.default && vs.voices[vs.default]) sel.value = vs.default;
      }
    } catch {}
    const h = await (await fetch("/api/health")).json();
    $("llmState").textContent = h.llm_online
      ? "Language core: ONLINE"
      : h.llm_configured
        ? "Primary core unreachable — Ollama fallback active if installed."
        : "Language core offline";
  }
}
const refreshTab = refreshView;

/* ---------- telemetry rail ---------- */
async function refreshTelemetry() {
  try {
    const s = await (await fetch("/api/status")).json();
    const cpu = s.cpu_percent != null ? Math.round(s.cpu_percent) : 0;
    const ramPct = s.ram_total_gb ? Math.round((s.ram_used_gb / s.ram_total_gb) * 100) : 0;
    const bat = s.battery_percent != null ? Math.round(s.battery_percent) : null;
    $("barCpu").style.width = cpu + "%"; $("valCpu").textContent = cpu + "%";
    $("barRam").style.width = ramPct + "%"; $("valRam").textContent = ramPct + "%";
    $("barBat").style.width = (bat ?? 0) + "%"; $("valBat").textContent = bat == null ? "AC" : bat + "%";
    $("teleUptime").textContent = s.uptime_hours != null ? `up ${Math.round(s.uptime_hours)}h` : "";
  } catch {}
  try {
    const projects = await (await fetch("/api/projects")).json();
    const running = projects.filter((p) => p.status === "running");
    $("teleProjects").innerHTML = running.length
      ? running.slice(0, 4).map((p) => `<div class="mrow"><span>#${p.id}</span><b class="proj-status running">run</b></div>`).join("")
      : `<span>idle</span>`;
  } catch {}
  try {
    const watchers = await (await fetch("/api/watchers")).json();
    const active = watchers.filter((w) => w.status === "active").length;
    const triggered = watchers.filter((w) => w.status === "triggered").slice(-3);
    $("teleWatchers").innerHTML =
      `<div class="mrow"><span>${active} armed</span><b>${watchers.length - active} hit</b></div>` +
      triggered.map((w) => `<div class="mrow"><span>#${w.id} ${esc(w.kind)}</span></div>`).join("");
  } catch {}
  try {
    const audit = await (await fetch("/api/audit?limit=5")).json();
    $("teleAudit").innerHTML = audit.map((a) => `<div class="mrow"><span>${esc(a.tool)}</span><b>${a.outcome === "ok" ? "✓" : "✗"}</b></div>`).join("") || "—";
  } catch {}
}
setInterval(refreshTelemetry, 8000);

/* ---------- events ---------- */
async function pollEvents() {
  while (true) {
    try {
      const res = await fetch("/api/events");
      const data = await res.json();
      for (const ev of data.events || []) {
        if (ev.type === "halt") {
          speechSynthesis.cancel();
          if (currentAudio) currentAudio.pause();
          toast(ev.text);
          continue;
        }
        if (ev.type === "voice_exchange") {
          if (ev.user_text) addMsg(ev.user_text, "you");
          addMsg(ev.text, "jarvis");
          continue;
        }
        if (ev.type === "wake") {
          window.focus();
          toast("Listening...");
          document.body.classList.add("listening");
          const micBtn = $("micBtn");
          if (micBtn && !micBtn.classList.contains("on") && !micBtn.disabled) micBtn.click();
          continue;
        }
        if (ev.type === "skill_proposal") { toast(ev.text); speak(ev.text); continue; }
        if (ev.type === "welcome") { toast(ev.text); speak(ev.text); continue; }
        if (ev.kind === "project_log") { refreshView("projects"); continue; }
        toast(ev.text);
        if (!ev.spoken) speak(ev.text, ev.kind === "watcher" ? "alert" : "");
        if (ev.type === "project_done") refreshView("projects");
      }
      setOnline(true);
    } catch {
      setOnline(false);
      await new Promise((r) => setTimeout(r, 3000));
    }
  }
}

function setOnline(ok) {
  const el = $("connState");
  el.title = ok ? "server online" : "server offline";
  el.className = `state-dot ${ok ? "online" : "offline"}`;
}

/* ---------- speech recognition ---------- */
const logWatchers = {};

function watchProject(id) {
  if (logWatchers[id]) return;
  logWatchers[id] = setInterval(async () => {
    const p = await (await fetch(`/api/projects/${id}`)).json();
    if (p.status !== "running") {
      clearInterval(logWatchers[id]);
      delete logWatchers[id];
      toast(`Project #${id} ${p.status}: ${(p.result || "").slice(0, 120)}`);
      speak(`Project ${p.status}.`, "");
      refreshView("projects");
    }
  }, 4000);
}

async function loadLog(id, li) {
  const p = await (await fetch(`/api/projects/${id}`)).json();
  if (!p.log || !p.log.length) return;
  let box = li.querySelector(".proj-log");
  if (!box) {
    box = document.createElement("div");
    box.className = "proj-log";
    li.appendChild(box);
  }
  const last = p.log.slice(-6).map((l) => `• ${l.entry}`).join("\n");
  if (box.textContent !== last) box.textContent = last;
}

function setupSpeech() {
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  const micBtn = $("micBtn");
  let offlineVoice = false;

  if (!SR) {
    toast("Cloud speech unavailable — using EVO's offline ear.");
    offlineVoice = true;
  }

  if (SR) {
    recognition = new SR();
    recognition.lang = "en-GB";
    recognition.interimResults = true;
    recognition.continuous = false;

    recognition.onresult = (e) => {
      let interim = "", final = "";
      for (const r of e.results) {
        if (r.isFinal) final += r[0].transcript;
        else interim += r[0].transcript;
      }
      $("sttPreview").textContent = interim || final;
      if (final && !wakeMode) handleHeard(final.trim(), false);
      else if (final && wakeMode && !manualListen) handleHeard(final.trim(), true);
      else if (final && manualListen) handleHeard(final.trim(), false);
    };
    recognition.onend = () => {
      micBtn.classList.remove("on");
      document.body.classList.remove("listening");
      if (wakeMode && !manualListen) setTimeout(() => safeStart(), 250);
      manualListen = false;
    };
    recognition.onerror = (e) => {
      const reasons = {
        "not-allowed": "Microphone BLOCKED — click the 🔒/mic icon in the address bar and allow it.",
        "service-not-allowed": "Mic blocked by browser settings — allow microphone for this app.",
        "no-speech": "Didn't hear anything — try again.",
        "audio-capture": "No microphone found.",
        network: "Cloud speech unreachable — switching to EVO's offline ear.",
      };
      micBtn.classList.remove("on");
      document.body.classList.remove("listening");
      manualListen = false;
      if (e.error === "network" || e.error === "service-not-allowed") {
        offlineVoice = true;
      }
      toast(reasons[e.error] || `Mic error: ${e.error}`);
      $("sttPreview").textContent = "";
      if (offlineVoice && e.error === "network" && wakeMode) startOfflineCapture(true);
    };
  }

  /* ---- offline voice: record WAV in-browser -> server Vosk transcribe ---- */
  let audioCtx = null, mediaStream = null, processor = null, sourceNode = null;
  let recording = false, speechSeen = false, quietFrames = 0, collected = [];
  const REC_RATE = 16000, MAX_REC_MS = 9000, SILENCE_FRAMES = 24;

  async function startOfflineCapture(fromWake = false) {
    if (recording) return;
    try {
      mediaStream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
      });
    } catch {
      toast("Microphone BLOCKED — allow mic access for this site.");
      return;
    }
    recording = true; speechSeen = false; quietFrames = 0; collected = [];
    try { audioCtx = new AudioContext({ sampleRate: REC_RATE }); }
    catch { audioCtx = new AudioContext(); }
    sourceNode = audioCtx.createMediaStreamSource(mediaStream);
    processor = audioCtx.createScriptProcessor(2048, 1, 1);
    const rate = audioCtx.sampleRate;
    processor.onaudioprocess = (ev) => {
      if (!recording) return;
      const input = ev.inputBuffer.getChannelData(0);
      let peak = 0;
      for (let i = 0; i < input.length; i++) {
        const v = Math.abs(input[i]);
        if (v > peak) peak = v;
      }
      if (peak > 0.06) { speechSeen = true; quietFrames = 0; }
      else if (speechSeen) quietFrames++;
      for (let i = 0; i < input.length; i++) collected.push(input[i]);
      const frameMs = (input.length / rate) * 1000;
      if ((speechSeen && quietFrames * frameMs > 1100) || collected.length / rate * 1000 > MAX_REC_MS) {
        finishOfflineCapture(rate);
      }
    };
    sourceNode.connect(processor);
    processor.connect(audioCtx.destination);
    micBtn.classList.add("on");
    document.body.classList.add("listening");
    if (fromWake) $("sttPreview").textContent = "Listening...";
  }

  /* ---- ChatGPT-style session: after a reply, keep listening hands-free ---- */
  let sessionOpen = false;
  let sessionEmpties = 0;
  function sessionRearm(delay = 1200) {
    if (!wakeMode || manualListen || !offlineVoice) return;
    if (!sessionOpen) {
      if (sessionEmpties >= 3) toast("Session paused — say the wake word or press MIC to continue.");
      return;
    }
    setTimeout(() => { if (wakeMode && !manualListen && offlineVoice && !recording) safeStart(); }, delay);
  }

  async function finishOfflineCapture(rate) {
    recording = false;
    micBtn.classList.remove("on");
    document.body.classList.remove("listening");
    try { processor.disconnect(); sourceNode.disconnect(); } catch {}
    try { mediaStream.getTracks().forEach((t) => t.stop()); } catch {}
    const samples = new Int16Array(collected.length);
    for (let i = 0; i < collected.length; i++) {
      const s = Math.max(-1, Math.min(1, collected[i]));
      samples[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
    }
    collected = [];
    try { await audioCtx.close(); } catch {}
    audioCtx = null;
    const wav = encodeWav(samples, rate);
    $("sttPreview").textContent = "Transcribing locally...";
    try {
      const res = await fetch("/api/transcribe", { method: "POST", body: wav });
      if (!res.ok) {
        if (res.status === 503) toast("Offline speech engine not ready — run start.bat once to install it, then retry.");
        else if (res.status === 404) toast("EVO server is an old build — run stop-evo.bat, then start.bat.");
        else toast(`Transcription failed (${res.status}).`);
        sessionRearm(2500);
        return;
      }
      const data = await res.json();
      $("sttPreview").textContent = "";
      let text = (data.text || "").trim();
      if (!text) {
        sessionEmpties++;
        toast("Didn't hear anything — try again.");
        sessionRearm(800);
        return;
      }
      if (wakeMode && !manualListen) {
        if (isExitUtterance(text)) {
          sessionOpen = false;
          sessionEmpties = 0;
          toast("Session closed. Say the wake word or press MIC when you need me.");
          return;
        }
        const stripped = stripWakePhrase(text);
        if (stripped !== null) {
          // Wake phrase present: opens/continues the session.
          sessionOpen = true;
          sessionEmpties = 0;
          text = stripped;
          if (!text) {
            $("sttPreview").textContent = "I'm listening...";
            setTimeout(() => { if (wakeMode && !manualListen && offlineVoice && !recording) safeStart(); }, 400);
            return;
          }
        } else if (!sessionOpen) {
          // Not woken yet: ignore chatter, keep a low-cost ear open.
          sessionEmpties++;
          sessionRearm(800);
          return;
        } else {
          sessionEmpties = 0; // mid-session sentence - answered freely
        }
      }
      send(text);
      sessionRearm(1500);
    } catch (err) {
      $("sttPreview").textContent = "";
      toast("Cannot reach the EVO server — is it running? Try start.bat.");
      sessionRearm(2500);
    }
  }

  function isExitUtterance(text) {
    const t = normalizeForWake(text);
    return ["stop listening", "go to sleep", "go away", "that will be all", "goodbye", "end session"]
      .some((p) => t.includes(p));
  }

  function encodeWav(int16, rate) {
    const buf = new ArrayBuffer(44 + int16.length * 2);
    const v = new DataView(buf);
    const ws = (o, s) => { for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)); };
    ws(0, "RIFF"); v.setUint32(4, 36 + int16.length * 2, true); ws(8, "WAVE");
    ws(12, "fmt "); v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
    v.setUint32(24, rate, true); v.setUint32(28, rate * 2, true);
    v.setUint16(32, 2, true); v.setUint16(34, 16, true);
    ws(36, "data"); v.setUint32(40, int16.length * 2, true);
    for (let i = 0; i < int16.length; i++) v.setInt16(44 + i * 2, int16[i], true);
    return new Blob([buf], { type: "audio/wav" });
  }

  let manualListen = false;

  /* ---- wake phrase gating (shared by cloud SR and offline capture) ---- */
  let wakePhrases = ["wake up evo"];
  try {
    fetch("/api/wake-phrases").then((r) => r.json()).then((d) => {
      if (Array.isArray(d.phrases) && d.phrases.length) wakePhrases = d.phrases;
    }).catch(() => {});
  } catch {}

  const LEGACY_CALLSIGNS = ["jarvis", "hey jarvis", "evo", "evvo", "hey evo", "okay evo"];

  function normalizeForWake(s) {
    return String(s || "").toLowerCase().replace(/[^a-z0-9 ]+/g, " ").replace(/\s+/g, " ").trim();
  }

  function stripWakePhrase(text) {
    // Returns the remainder after a wake phrase, "" if only the phrase was
    // said, or null when no wake phrase is present.
    const s = normalizeForWake(text);
    if (!s) return null;
    const candidates = wakePhrases.concat(LEGACY_CALLSIGNS)
      .map((p) => normalizeForWake(p)).filter(Boolean);
    for (const p of candidates) {
      const idx = s.indexOf(p);
      if (idx >= 0) return s.slice(idx + p.length).trim();
    }
    return null;
  }
  window._stripWakePhrase = stripWakePhrase;

  function safeStart() {
    if (offlineVoice) { if (wakeMode) startOfflineCapture(false); return; }
    try { recognition.start(); } catch {}
  }
  function handleHeard(text, viaWake) {
    $("sttPreview").textContent = "";
    if (!text) return;
    if (viaWake) {
      const stripped = stripWakePhrase(text);
      if (stripped === null || !stripped) return;
      text = stripped;
    }
    send(text);
  }
  window._handleHeard = handleHeard;
  window._safeStart = safeStart;
  window._offlineVoice = () => offlineVoice;

  micBtn.addEventListener("click", () => {
    if (micBtn.classList.contains("on")) {
      manualListen = false;
      if (recording) { speechSeen = true; quietFrames = SILENCE_FRAMES; }
      else { try { recognition.stop(); } catch {} }
      return;
    }
    manualListen = true;
    sessionOpen = true;
    sessionEmpties = 0;
    if (offlineVoice || !recognition) {
      startOfflineCapture(false).then(() => {
        setTimeout(() => { if (recording && !speechSeen) finishOfflineCapture(audioCtx ? audioCtx.sampleRate : REC_RATE); }, 6000);
      });
      return;
    }
    try { recognition.stop(); } catch {}
    setTimeout(() => { micBtn.classList.add("on"); safeStart(); }, 120);
  });

  document.addEventListener("keydown", (e) => {
    const typing = ["TEXTAREA", "INPUT", "SELECT"].includes(document.activeElement.tagName);
    if (e.code === "Space" && !typing) { e.preventDefault(); micBtn.click(); }
  });
}

/* ---------- forms ---------- */
$("chatForm").addEventListener("submit", (e) => {
  e.preventDefault();
  send($("chatInput").value);
  $("chatInput").value = "";
  $("chatInput").style.height = "auto";
});
$("chatInput").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    $("chatForm").dispatchEvent(new Event("submit"));
  }
});
$("chatInput").addEventListener("input", function () {
  this.style.height = "auto";
  this.style.height = Math.min(this.scrollHeight, 160) + "px";
});

document.querySelectorAll(".chip").forEach((c) =>
  c.addEventListener("click", () => send(c.dataset.q)));

$("remForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const dueAt = new Date($("remWhen").value).getTime() / 1000;
  await fetch("/api/reminders", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message: $("remText").value, due_at: dueAt }),
  });
  $("remText").value = "";
  refreshView("tasks");
});

$("memForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  await fetch("/api/memory", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ key: $("memKey").value, value: $("memValue").value }),
  });
  $("memKey").value = ""; $("memValue").value = "";
  refreshView("memory");
});

$("projForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  await fetch("/api/projects", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ goal: $("projGoal").value }),
  });
  $("projGoal").value = "";
  addMsg("Mission launched. I will report back when it is finished.", "jarvis");
  speak("Mission launched. I will report back when it is finished.");
  refreshView("projects");
});

$("refreshStatus").addEventListener("click", () => refreshView("system"));

$("saveSettings").addEventListener("click", async () => {
  await fetch("/api/settings", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      briefing_enabled: $("briefingEnabled").checked,
      briefing_time: $("briefingTime").value,
      city: $("cityInput").value.trim(),
      calendar_ical_url: $("calUrl").value.trim(),
      ambient_vision_min: parseInt($("ambientVision").value || "0", 10),
      auto_approve_code: $("autoApprove").checked,
      deep_mode: $("deepMode").checked,
      gui_allowed: $("guiAllowed").checked,
      allow_mail_send: $("allowMailSend").checked,
    }),
  });
  localStorage.setItem("evo_neural", $("neuralVoice").checked ? "1" : "0");
  if ($("neuralVoiceSelect").value) localStorage.setItem("evo_tts_voice", $("neuralVoiceSelect").value);
  toast("Settings saved.");
  refreshView("setup");
});

$("neuralVoiceSelect").addEventListener("change", () => {
  localStorage.setItem("evo_tts_voice", $("neuralVoiceSelect").value);
});

/* ---------- wake word persistence ---------- */
$("wakeMode").addEventListener("change", (e) => {
  wakeMode = e.target.checked;
  localStorage.setItem("evo_wake", wakeMode ? "1" : "0");
  if (!window._safeStart) return;
  if (wakeMode) {
    if (recognition && !window._offlineVoice()) recognition.continuous = true;
    window._safeStart();
  } else if (recognition && !window._offlineVoice()) {
    try { recognition.stop(); } catch {}
  }
});
if (localStorage.getItem("evo_wake") === "1") {
  $("wakeMode").checked = true;
  const armAmbient = () => {
    wakeMode = true;
    if (window._safeStart) window._safeStart();
    document.removeEventListener("click", armAmbient);
    document.removeEventListener("keydown", armAmbient);
  };
  document.addEventListener("click", armAmbient);
  document.addEventListener("keydown", armAmbient);
}

/* ---------- boot ---------- */
async function greetByHour() {
  const h = new Date().getHours();
  const part = h < 12 ? "morning" : h < 18 ? "afternoon" : "evening";
  let address = "sir";
  try {
    const cfg = await (await fetch("/api/settings")).json();
    if (cfg.user_address) address = cfg.user_address;
    if (cfg.name) applyBrand(cfg.name);
  } catch {}
  $("heroGreeting").textContent = `Good ${part}, ${address}. Systems online.`;
}

async function loadConversations() {
  try {
    const msgs = await (await fetch("/api/conversations?limit=40")).json();
    if (!msgs.length) return;
    for (const m of msgs) addMsg(m.content, m.role === "user" ? "you" : "jarvis");
    log.scrollTop = log.scrollHeight;
  } catch {}
}

if ("speechSynthesis" in window) {
  pickVoice();
  speechSynthesis.onvoiceschanged = pickVoice;
}
setupSpeech();
greetByHour();
loadConversations();
["tasks", "projects", "memory", "knowledge"].forEach((t) => refreshView(t));
refreshView("setup");
refreshTelemetry();
pollEvents();
