---
name: aistudio-images
description: Generate or edit images with Google AI Studio's Nano Banana models (Gemini image models) through the user's own signed-in browser, via the Claude in Chrome extension, and save the results into the project. Use when the user gives one or many images to edit/restyle/upscale/combine, or asks to generate images with AI Studio, Gemini, or Nano Banana. Asks which model and resolution to use, runs the batch, and files results under gas-outputs/.
---

# AI Studio images (GAS Connection)

Runs image jobs in the Google AI Studio **web app** in the user's own browser (Chrome, Brave, Edge
or any Chromium browser with the Claude in Chrome extension), using their AI Studio account and
plan. No API key. Two files do the work, both in this skill's `scripts/` folder:

- `gas.py` — job queue on disk (stdlib Python). Run it from the user's project root.
  Use `python3` (or `python` on Windows). Below, `GAS` means `python3 "<this skill's base dir>/scripts/gas.py"`.
- `gas_bridge.js` — injected into the AI Studio tab with `javascript_tool`. Defines
  `gasStatus()`, `gasSettings()`, `gasNewChat()`, `gasRun()`, `gasGrab()`.

## 1. Ask for model and resolution (always, unless the user already said)

Before creating a job, ask with **AskUserQuestion** in ONE dialog (skip any question the user
already answered; reuse earlier answers in the same session):

1. **Model** (header "Model"):
   - Nano Banana 2 (Recommended) — `gemini-3.1-flash-image`, strong quality, fast, 512/1K/2K/4K
   - Nano Banana Pro — `gemini-3-pro-image`, best quality and text in images, slower, 1K/2K/4K
   - Nano Banana 2 Lite — `gemini-3.1-flash-lite-image`, fastest, 1K only
   - Nano Banana (original) — `gemini-2.5-flash-image`, no resolution setting
2. **Resolution** (header "Resolution"): 1K (Recommended), 2K, 4K, 512
3. **Aspect ratio** (header "Aspect"): Auto - keep input shape (Recommended), 1:1, 16:9, 9:16
   (others available: 3:4, 4:3, 3:2, 2:3, 5:4, 4:5, 21:9, and on Nano Banana 2 / Lite also 4:1, 1:4, 8:1, 1:8)

If the chosen resolution isn't offered by the chosen model (Lite = 1K only, original = none,
Pro has no 512), say so in one line and use the nearest offered value (original: omit it).
`GAS models` prints the full table.

## 2. Create the job

```
GAS new <files/folders...> --prompt "<prompt>" --model <id> --resolution <R> --aspect <A>
```
- One run per image by default. `--together` sends all images in ONE prompt (e.g. "put the
  product from image 1 into the scene from image 2").
- Different prompt per image: `--prompts prompts.json` (`{"cat.png": "...", "dog": "..."}`).
- Text-to-image with no inputs: omit files, add `--count N`.
- `--out <dir>` to save somewhere specific (default `./gas-outputs/<job>/`), `--name <job>`.
- It refuses settings the model doesn't offer (exit 2, lists them); fix and rerun.

## 3. Open AI Studio and load the bridge

1. Claude in Chrome: `tabs_context_mcp`, then `tabs_create_mcp`, and `navigate` to the `url`
   from `GAS next` (it carries `?model=`).
2. Load the bridge. Try the cached copy first (one short call):
   `(0, eval)(localStorage.gasBridge + "; gasBridgeInit()")` → `"GAS bridge 0.1.2 ready"`.
   If it throws, or reports an older version than the `VERSION` in `scripts/gas_bridge.js`,
   Read that file and pass its full text to `javascript_tool` (this also refreshes the cache).
   Repeat the one-liner after every real navigation (e.g. switching model = new job `url`).
   Then call `await gasStatus()`.
3. Check the status: `signedIn` false → ask the user to sign in to AI Studio in that browser.
   `locked` true → their plan lacks this model; offer another model. Never type passwords.

## 4. Run each item (two browser round trips per image)

Loop on `GAS next` until it returns `"done": true`:

- **Batch A:** `javascript_tool` `await gasNewChat()` (skip for the very first item on a fresh
  page) + `find` "hidden file input" → gives the file-input ref (it changes every chat).
- **Batch B:** `file_upload` (ref, the item's `inputs`; ≤10 MB per call, gas.py already shrinks
  bigger files) + `javascript_tool` with the item's `bridge_call` verbatim. For text-to-image
  items (no inputs) skip the upload.
- `gasRun` result:
  - `ok: true` → files were downloaded as `gas_<item>.<ext>`. Run `GAS collect` (every item or
    every few items) to move them into the output folder and mark them done.
  - `error: "settings"` → `applied` shows which value isn't offered; tell the user.
  - `error: "wrong model..."` → the tab is on another model: navigate to the item's `url`,
    re-arm the bridge, retry.
  - `ok: false` with `text` like "Image recitation block", a safety message, or quota/rate-limit
    text → `GAS fail <item> "<short reason>"` and continue. Quota/rate limit: stop the batch
    and tell the user.
  - timed out but an image is visible → `await gasGrab({name: "<item id>"})`, then collect.
- Run items one at a time in one tab. Don't open parallel tabs.

## 5. Finish

`GAS collect` then `GAS status`. Tell the user how many succeeded, the output folder, and any
failures with reasons (`GAS reset` makes failed items pending again for a retry). Show one or two
results with Read if helpful. Close the tab you created.

## Notes

- Typical times: Nano Banana 2 at 1K about 15 s, at 2K about 25 s; Pro at 4K about 45 s. A
  single `gasRun` call waits up to 300 s.
- Pro shows draft images while "Thinking"; the bridge skips those and saves only the final.
- AI Studio remembers the last settings across chats; jobs always set every value explicitly.
- Very plain inputs (flat shapes) can trigger "Image recitation block"; real photos/art are fine.
- If downloads don't appear, the browser may save elsewhere or ask where to save: set
  `GAS_DOWNLOADS=<folder>` or turn off "Ask where to save each file". `GAS doctor` shows paths.
- The UI can change. If a selector breaks, `gasStatus()` and a screenshot show what moved.
