#!/usr/bin/env python3
"""GAS Connection - job queue and result collector for Google AI Studio image runs.

The browser half (attach images, send prompt, download result) is done by Claude through the
Claude in Chrome extension using gas_bridge.js. This script does the disk half. Standard library
only (Pillow is optional, used to shrink images over 9 MB). Run it from your project root.

  gas.py new [IMAGES or FOLDERS...] --prompt "..." [--model M] [--resolution R] [--aspect A]
             [--thinking T] [--together] [--count N] [--prompts FILE.json] [--out DIR] [--name JOB]
  gas.py next    [JOB]         next pending item as JSON (inputs, prompt, url, bridge_call)
  gas.py collect [JOB]         move finished downloads into the job's output folder
  gas.py fail ITEM "reason"    mark an item failed      gas.py reset [JOB]   failed -> pending
  gas.py status  [JOB]         gas.py list                gas.py models       gas.py doctor
  gas.py bridge                print the path of gas_bridge.js

JOB defaults to the most recent job. State lives in ./.gas/, results in ./gas-outputs/<job>/
unless --out is given.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("GAS_ROOT", Path.cwd())).resolve()
STATE = ROOT / ".gas"
JOBS = STATE / "jobs"
DEFAULT_OUT = ROOT / "gas-outputs"
IMG_EXT = {".png", ".jpg", ".jpeg", ".webp", ".heic", ".heif", ".gif", ".bmp"}
MAX_UPLOAD = 9 * 1024 * 1024  # Claude in Chrome file_upload limit is 10 MB per call

# What the AI Studio UI offered when this was written (Nov 2026). The bridge reads the live
# options too, so treat this as guidance - gasStatus() is the source of truth.
MODELS = {
    "gemini-3.1-flash-image": {
        "name": "Nano Banana 2", "note": "default - strong quality, fast",
        "resolutions": ["512", "1K", "2K", "4K"], "thinking": ["Minimal", "High"],
        "aspect_ratios": ["Auto", "1:1", "9:16", "16:9", "3:4", "4:3", "3:2", "2:3", "5:4", "4:5",
                          "21:9", "4:1", "1:4", "8:1", "1:8"],
    },
    "gemini-3-pro-image": {
        "name": "Nano Banana Pro", "note": "best quality and text rendering, slower",
        "resolutions": ["1K", "2K", "4K"], "thinking": [],
        "aspect_ratios": ["Auto", "1:1", "9:16", "16:9", "3:4", "4:3", "3:2", "2:3", "5:4", "4:5", "21:9"],
    },
    "gemini-3.1-flash-lite-image": {
        "name": "Nano Banana 2 Lite", "note": "fastest/cheapest, quick recolors and stickers",
        "resolutions": ["1K"], "thinking": ["Minimal", "High"],
        "aspect_ratios": ["Auto", "1:1", "9:16", "16:9", "3:4", "4:3", "3:2", "2:3", "5:4", "4:5",
                          "21:9", "4:1", "1:4", "8:1", "1:8"],
    },
    "gemini-2.5-flash-image": {
        "name": "Nano Banana (original)", "note": "older model, no resolution setting",
        "resolutions": [], "thinking": [],
        "aspect_ratios": ["Auto", "1:1", "9:16", "16:9", "3:4", "4:3", "3:2", "2:3", "5:4", "4:5", "21:9"],
    },
}
DEFAULT_MODEL = "gemini-3.1-flash-image"


def out(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False))


def downloads_dir() -> Path:
    if os.environ.get("GAS_DOWNLOADS"):
        return Path(os.environ["GAS_DOWNLOADS"]).expanduser()
    if sys.platform == "win32":
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as k:
                val, _ = winreg.QueryValueEx(k, "{374DE290-123F-4565-9164-39C4925E467B}")
                return Path(os.path.expandvars(val))
        except OSError:
            pass
    elif sys.platform.startswith("linux"):
        cfg = Path.home() / ".config" / "user-dirs.dirs"
        if cfg.exists():
            for line in cfg.read_text().splitlines():
                if line.startswith("XDG_DOWNLOAD_DIR="):
                    return Path(os.path.expandvars(line.split("=", 1)[1].strip('"')))
    return Path.home() / "Downloads"


def job_path(job: str | None) -> Path:
    if job:
        p = JOBS / f"{job}.json"
        if not p.exists():
            sys.exit(f"no such job: {job} (see: gas.py list)")
        return p
    jobs = sorted(JOBS.glob("*.json"), key=lambda p: p.stat().st_mtime) if JOBS.exists() else []
    if not jobs:
        sys.exit("no jobs yet - create one with: gas.py new <images> --prompt ...")
    return jobs[-1]


def load(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8"))


def save(p: Path, data: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    ignore = STATE / ".gitignore"
    if not ignore.exists():
        ignore.write_text("*\n", encoding="utf-8")  # keep job state out of the user's git
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def shrink_if_needed(path: Path) -> Path:
    if path.stat().st_size <= MAX_UPLOAD:
        return path
    try:
        from PIL import Image
    except ImportError:
        sys.exit(f"{path.name} is over 9 MB; install Pillow (pip install pillow) or shrink it first")
    dst_dir = STATE / "resized"
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / (path.stem + ".jpg")
    im = Image.open(path).convert("RGB")
    im.thumbnail((3072, 3072))
    im.save(dst, "JPEG", quality=92)
    return dst


def gather(paths: list[str]) -> list[Path]:
    files: list[Path] = []
    for raw in paths:
        p = Path(raw).expanduser().resolve()
        if p.is_dir():
            files += sorted(f for f in p.iterdir() if f.suffix.lower() in IMG_EXT)
        elif p.is_file():
            files.append(p)
        else:
            sys.exit(f"not found: {raw}")
    return files


def check_settings(model: str, resolution: str | None, aspect: str | None, thinking: str | None) -> list[str]:
    info = MODELS.get(model)
    if not info:
        return [f"unknown model {model!r}; it will be tried anyway (known: {', '.join(MODELS)})"]
    warn = []
    if resolution and resolution not in info["resolutions"]:
        warn.append(f"{info['name']} offers resolutions {info['resolutions'] or 'none'}, not {resolution!r}")
    if aspect and aspect not in info["aspect_ratios"]:
        warn.append(f"{info['name']} offers aspect ratios {info['aspect_ratios']}, not {aspect!r}")
    if thinking and thinking not in info["thinking"]:
        warn.append(f"{info['name']} offers thinking levels {info['thinking'] or 'none'}, not {thinking!r}")
    return warn


def cmd_new(a) -> None:
    files = gather(a.inputs)
    per_prompt = {}
    if a.prompts:
        per_prompt = json.loads(Path(a.prompts).read_text(encoding="utf-8"))
    if not a.prompt and not per_prompt:
        sys.exit("give --prompt, or --prompts FILE.json mapping image file names to prompts")
    warn = check_settings(a.model, a.resolution, a.aspect, a.thinking)
    if warn and not a.force:
        out({"error": "settings not offered for this model (use --force to try anyway)", "details": warn})
        sys.exit(2)

    if files and a.together:
        groups = [files]
    elif files:
        groups = [[f] for f in files]
    else:
        groups = [[] for _ in range(a.count)]  # text-to-image

    job = a.name or datetime.now().strftime("job_%Y%m%d_%H%M%S")
    if (JOBS / f"{job}.json").exists():
        sys.exit(f"job {job} already exists")
    items = []
    for n, group in enumerate(groups, 1):
        label = group[0].stem if len(group) == 1 else (f"combined_{n:03d}" if group else f"image_{n:03d}")
        prompt = per_prompt.get(group[0].name, per_prompt.get(group[0].stem)) if len(group) == 1 else None
        items.append({
            "id": f"{job}_{n:04d}",
            "label": label,
            "inputs": [str(shrink_if_needed(f)) for f in group],
            "prompt": prompt or a.prompt,
            "status": "pending",
            "outputs": [],
            "note": "",
        })
    missing = [it["label"] for it in items if not it["prompt"]]
    if missing:
        sys.exit(f"no prompt for: {', '.join(missing)}")
    # AI Studio keeps the last-used settings across chats, so always set every value explicitly.
    info = MODELS.get(a.model, {})
    settings = {"aspectRatio": a.aspect or "Auto"}
    if a.resolution or info.get("resolutions"):
        settings["resolution"] = a.resolution or ("1K" if "1K" in info.get("resolutions", []) else None)
    if a.thinking or info.get("thinking"):
        settings["thinking"] = a.thinking or info["thinking"][0]
    settings = {k: v for k, v in settings.items() if v}
    data = {
        "job": job, "model": a.model, "settings": settings,
        "out_dir": str(Path(a.out).resolve() if a.out else DEFAULT_OUT / job),
        "created": datetime.now().isoformat(timespec="seconds"), "items": items,
    }
    save(JOBS / f"{job}.json", data)
    out({"job": job, "model": a.model, "settings": settings, "items": len(items),
         "out_dir": data["out_dir"], "warnings": warn})


def item_view(data: dict, it: dict) -> dict:
    call = {"prompt": it["prompt"], "name": it["id"], "model": data["model"]}
    if data.get("settings"):
        call["settings"] = data["settings"]
    return {
        "job": data["job"], "id": it["id"], "inputs": it["inputs"], "prompt": it["prompt"],
        "model": data["model"], "settings": data.get("settings", {}),
        "url": f"https://aistudio.google.com/prompts/new_chat?model={data['model']}",
        "bridge_call": f"await gasRun({json.dumps(call, ensure_ascii=False)})",
    }


def cmd_next(a) -> None:
    data = load(job_path(a.job))
    pending = [it for it in data["items"] if it["status"] == "pending"]
    if not pending:
        out({"job": data["job"], "done": True, "out_dir": data["out_dir"]})
        return
    view = item_view(data, pending[0])
    view["remaining"] = len(pending)
    out(view)


def cmd_collect(a) -> None:
    p = job_path(a.job)
    data = load(p)
    dest = Path(data["out_dir"])
    dl = downloads_dir()
    moved = []
    deadline = time.time() + a.wait
    while True:
        for it in data["items"]:
            if it["status"] == "done":
                continue
            found = [f for f in sorted(dl.glob(f"gas_{it['id']}*"))
                     if not f.name.endswith((".crdownload", ".tmp", ".part"))]
            if not found:
                continue
            dest.mkdir(parents=True, exist_ok=True)
            it["outputs"] = []
            for k, f in enumerate(found):
                target = dest / f"{it['label']}{'' if k == 0 else f'_{k + 1}'}{f.suffix}"
                n = 2
                while target.exists():
                    target = dest / f"{it['label']}{'' if k == 0 else f'_{k + 1}'}-v{n}{f.suffix}"
                    n += 1
                shutil.move(str(f), target)
                it["outputs"].append(str(target))
                moved.append(str(target))
            it["status"], it["note"] = "done", ""
        if moved or time.time() >= deadline:
            break
        time.sleep(0.5)
    save(p, data)
    left = sum(it["status"] == "pending" for it in data["items"])
    res = {"job": data["job"], "collected": moved, "pending": left, "out_dir": str(dest)}
    if not moved:
        res["hint"] = f"nothing named gas_<item>* in {dl}; set GAS_DOWNLOADS if your browser saves elsewhere"
    out(res)


def cmd_fail(a) -> None:
    for p in JOBS.glob("*.json"):
        data = load(p)
        for it in data["items"]:
            if it["id"] == a.item:
                it["status"], it["note"] = "failed", a.reason
                save(p, data)
                out({"failed": a.item, "reason": a.reason})
                return
    sys.exit(f"no such item: {a.item}")


def cmd_reset(a) -> None:
    p = job_path(a.job)
    data = load(p)
    n = 0
    for it in data["items"]:
        if it["status"] == "failed":
            it["status"], n = "pending", n + 1
    save(p, data)
    out({"job": data["job"], "reset": n})


def cmd_status(a) -> None:
    data = load(job_path(a.job))
    counts: dict[str, int] = {}
    for it in data["items"]:
        counts[it["status"]] = counts.get(it["status"], 0) + 1
    out({"job": data["job"], "model": data["model"], "settings": data.get("settings"), "counts": counts,
         "out_dir": data["out_dir"],
         "failed": [{"id": it["id"], "label": it["label"], "note": it["note"]}
                    for it in data["items"] if it["status"] == "failed"]})


def cmd_list(a) -> None:
    rows = []
    for p in sorted(JOBS.glob("*.json"), key=lambda p: p.stat().st_mtime) if JOBS.exists() else []:
        d = load(p)
        done = sum(it["status"] == "done" for it in d["items"])
        rows.append({"job": d["job"], "model": d["model"], "done": f"{done}/{len(d['items'])}", "created": d["created"]})
    out(rows)


def cmd_doctor(a) -> None:
    dl = downloads_dir()
    try:
        import PIL  # noqa: F401
        pil = True
    except ImportError:
        pil = False
    out({"python": sys.version.split()[0], "project_root": str(ROOT), "downloads_dir": str(dl),
         "downloads_exists": dl.exists(), "pillow_installed": pil, "bridge": str(HERE / "gas_bridge.js")})


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("new", help="create a job")
    s.add_argument("inputs", nargs="*", help="image files or folders (omit for text-to-image)")
    s.add_argument("--prompt")
    s.add_argument("--prompts", help="JSON file mapping image file name (or stem) -> prompt")
    s.add_argument("--model", default=DEFAULT_MODEL)
    s.add_argument("--resolution", help="e.g. 512, 1K, 2K, 4K (model dependent)")
    s.add_argument("--aspect", help="e.g. Auto, 1:1, 16:9, 9:16 (model dependent)")
    s.add_argument("--thinking", help="Minimal or High (model dependent)")
    s.add_argument("--together", action="store_true", help="send all images in ONE prompt")
    s.add_argument("--count", type=int, default=1, help="number of images when there are no inputs")
    s.add_argument("--out", help="output folder (default ./gas-outputs/<job>)")
    s.add_argument("--name", help="job name")
    s.add_argument("--force", action="store_true", help="skip the model/settings check")
    s.set_defaults(f=cmd_new)
    for name, fn in (("next", cmd_next), ("reset", cmd_reset), ("status", cmd_status)):
        s = sub.add_parser(name); s.add_argument("job", nargs="?"); s.set_defaults(f=fn)
    s = sub.add_parser("collect"); s.add_argument("job", nargs="?")
    s.add_argument("--wait", type=float, default=5, help="seconds to wait for downloads"); s.set_defaults(f=cmd_collect)
    s = sub.add_parser("fail"); s.add_argument("item"); s.add_argument("reason"); s.set_defaults(f=cmd_fail)
    sub.add_parser("list").set_defaults(f=cmd_list)
    sub.add_parser("models").set_defaults(f=lambda a: out(MODELS))
    sub.add_parser("doctor").set_defaults(f=cmd_doctor)
    sub.add_parser("bridge").set_defaults(f=lambda a: print(HERE / "gas_bridge.js"))
    a = ap.parse_args()
    a.f(a)


if __name__ == "__main__":
    main()
