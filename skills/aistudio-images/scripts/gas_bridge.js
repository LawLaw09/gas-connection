// GAS Connection bridge — runs inside an https://aistudio.google.com chat tab.
// Injected by Claude through the Claude in Chrome extension (javascript_tool), once per page load.
//
//   await gasStatus()                       -> model, locked?, available + current settings
//   await gasSettings({resolution, aspectRatio, thinking})
//                                           -> picks those values in the Run settings panel
//   await gasNewChat()                      -> fresh chat WITHOUT a page reload (keeps this bridge + model)
//   await gasRun({prompt, name, settings})  -> applies settings, fills prompt, clicks Run (images must
//                                              already be attached via file_upload), waits for the reply,
//                                              downloads every new image as gas_<name>[_N].<ext>
//   await gasGrab({name})                   -> downloads the image(s) of the latest reply (recovery)
//
// Downloads land in the browser's download folder; scripts/gas.py collect files them into the project.
//
// The bridge caches itself in this site's localStorage. After a page navigation, re-arm it with:
//   (0, eval)(localStorage.gasBridge + "; gasBridgeInit()")
// (returns "GAS bridge <version> ready"; if it throws, paste this whole file again).

function gasBridgeInit() {
  const VERSION = "0.1.2";
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  // Final images only: drafts shown inside the model's "Thinking" panel (ms-thought-chunk) are skipped.
  const genImgs = (root = document) =>
    [...root.querySelectorAll("img")].filter(
      (i) => i.src.startsWith("blob:") && /Generated Image/i.test(i.alt) && !i.closest("ms-thought-chunk")
    );
  const submitBtn = () =>
    document.querySelector("button.ctrl-enter-submits") ||
    [...document.querySelectorAll("button")].find((b) => /\b(Run|Stop)\b/.test(b.innerText));
  const busy = () => /\bStop\b/.test(submitBtn()?.innerText || "");
  const turns = () => [...document.querySelectorAll("ms-chat-turn")];
  const clean = (t) =>
    (t || "")
      .replace(/more_vert|thumb_up|thumb_down|\bedit\b|(User|Model)\s+\d+:\d+\s*[AP]M/g, "")
      .replace(/\n{2,}/g, "\n")
      .trim()
      .slice(0, 800);
  const combo = (label) =>
    [...document.querySelectorAll('[role="combobox"]')].find(
      (c) => (c.getAttribute("aria-label") || "").toLowerCase() === label.toLowerCase()
    );
  const closeOverlay = async () => {
    document.querySelector(".cdk-overlay-backdrop")?.click();
    await sleep(300);
  };

  async function readCombo(label) {
    const c = combo(label);
    if (!c) return null;
    c.click();
    await sleep(500);
    const options = [...document.querySelectorAll('[role="option"]')].map((o) => o.innerText.trim());
    await closeOverlay();
    return { current: c.innerText.trim(), options };
  }

  async function setCombo(label, value) {
    if (value == null || value === "") return { skipped: true };
    const c = combo(label);
    if (!c) return { error: `${label}: not offered for this model` };
    if (c.innerText.trim().toLowerCase() === String(value).toLowerCase()) return { value: c.innerText.trim() };
    c.click();
    await sleep(500);
    const opts = [...document.querySelectorAll('[role="option"]')];
    const hit = opts.find((o) => o.innerText.trim().toLowerCase() === String(value).toLowerCase());
    if (!hit) {
      const options = opts.map((o) => o.innerText.trim());
      await closeOverlay();
      return { error: `${label}: "${value}" not available`, options };
    }
    hit.click();
    await sleep(400);
    return { value: c.innerText.trim() };
  }

  function modelId() {
    const fromUrl = new URL(location.href).searchParams.get("model");
    const fromPanel = (document.querySelector("ms-run-settings") || document.body).innerText.match(/gemini-[\w.-]+-image[\w.-]*/);
    return fromPanel?.[0] || fromUrl || null;
  }

  async function gasStatus() {
    const head = document.body.innerText.slice(0, 4000);
    return {
      url: location.href,
      model: modelId(),
      locked: /only available with a Google AI plan|Unlock Nano Banana/i.test(head),
      signedIn: !/Sign in/i.test(document.title),
      resolution: await readCombo("Resolution"),
      aspectRatio: await readCombo("Aspect ratio"),
      thinking: await readCombo("Thinking Level"),
      turns: turns().length,
    };
  }

  async function gasSettings({ resolution, aspectRatio, thinking } = {}) {
    return {
      resolution: await setCombo("Resolution", resolution),
      aspectRatio: await setCombo("Aspect ratio", aspectRatio),
      thinking: await setCombo("Thinking Level", thinking),
    };
  }

  async function gasNewChat() {
    const b = [...document.querySelectorAll("button")].find((x) => x.getAttribute("aria-label") === "New chat");
    if (b && !b.disabled) {
      b.click();
      await sleep(1500);
    }
    return { ok: turns().length === 0, turns: turns().length, model: modelId() };
  }

  async function download(imgs, name) {
    const files = [];
    for (let k = 0; k < imgs.length; k++) {
      const blob = await fetch(imgs[k].src).then((r) => r.blob());
      const ext = (blob.type.split("/")[1] || "png").replace("jpeg", "jpg");
      const file = `gas_${name}${k ? "_" + (k + 1) : ""}.${ext}`;
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = file;
      document.body.appendChild(a);
      a.click();
      a.remove();
      files.push({ file, bytes: blob.size, w: imgs[k].naturalWidth, h: imgs[k].naturalHeight });
      await sleep(300);
    }
    return files;
  }

  async function gasRun({ prompt, name, model, settings, timeoutSec = 300 }) {
    if (!name) return { ok: false, error: "name is required" };
    const ta = document.querySelector('textarea[aria-label="Enter a prompt"]') || document.querySelector("textarea");
    if (!ta) return { ok: false, error: "prompt box not found (not on an AI Studio chat page, or signed out?)" };
    const current = modelId();
    if (model && current && current !== model)
      return { ok: false, error: `wrong model: tab is on ${current}, job wants ${model}. Navigate to the job url and re-inject.` };

    const applied = settings ? await gasSettings(settings) : null;
    const settingErrors = applied && Object.values(applied).filter((v) => v.error);
    if (settingErrors?.length) return { ok: false, error: "settings", applied };

    const before = new Set(genImgs().map((i) => i.src));
    const turnsBefore = turns().length;

    ta.focus();
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value").set.call(ta, prompt);
    ta.dispatchEvent(new Event("input", { bubbles: true }));
    await sleep(500);

    // Click Run once it's enabled, and confirm a run actually started (a click made while an
    // attachment is still being processed is silently ignored) - retry up to 3 times.
    const ready = (b) => b && !b.disabled && b.getAttribute("aria-disabled") !== "true" && /\bRun\b/.test(b.innerText);
    let started = false;
    for (let attempt = 0; attempt < 3 && !started; attempt++) {
      let btn = null;
      for (let i = 0; i < 30 && !ready(btn); i++) {
        await sleep(attempt || i ? 1000 : 0);
        btn = submitBtn();
      }
      if (!ready(btn)) return { ok: false, error: "Run button missing or disabled (model locked? upload stuck?)" };
      btn.click();
      for (let i = 0; i < 10 && !started; i++) {
        await sleep(500);
        started = busy() || turns().length > turnsBefore;
      }
    }
    if (!started) return { ok: false, error: "clicking Run did not start a run (3 tries)" };

    // Done = the Run button is back (no "Stop") and the reply is settled.
    const t0 = Date.now();
    let settled = 0, lastCount = -1;
    while (Date.now() - t0 < timeoutSec * 1000) {
      await sleep(1000);
      if (busy()) {
        settled = 0;
        continue;
      }
      const count = genImgs().filter((i) => !before.has(i.src) && i.complete && i.naturalWidth > 0).length;
      settled = count === lastCount ? settled + 1 : 0;
      lastCount = count;
      if (settled >= 2) break; // idle and unchanged for ~2 s
    }

    turns().at(-1)?.scrollIntoView(); // the chat is virtualised; make sure the reply is rendered
    await sleep(800);
    const files = await download(genImgs().filter((i) => !before.has(i.src)), name);
    const timedOut = !files.length && Date.now() - t0 >= timeoutSec * 1000;
    return {
      ok: files.length > 0,
      files,
      text: clean(turns().at(-1)?.innerText),
      applied,
      error: files.length ? null : timedOut ? "timed out waiting for the model" : "no image in reply (see text)",
      seconds: Math.round((Date.now() - t0) / 1000),
    };
  }

  async function gasGrab({ name }) {
    const last = turns().at(-1);
    const imgs = last ? genImgs(last) : [];
    const files = await download(imgs.length ? imgs : genImgs().slice(-1), name);
    return { ok: files.length > 0, files, text: clean(last?.innerText) };
  }

  Object.assign(window, { gasStatus, gasSettings, gasNewChat, gasRun, gasGrab });
  try {
    localStorage.setItem("gasBridge", gasBridgeInit.toString());
  } catch (e) {
    /* storage blocked: re-paste the file after navigations */
  }
  return `GAS bridge ${VERSION} ready`;
}
gasBridgeInit();
