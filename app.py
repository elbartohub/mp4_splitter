import io
import os
import threading
import time
import shutil
import tempfile
import zipfile
from datetime import datetime
from functools import lru_cache
from html import escape
from pathlib import Path
from subprocess import PIPE, CalledProcessError, Popen, run
from uuid import uuid4

from flask import Flask, Response, abort, jsonify, redirect, request, send_file, url_for
from werkzeug.utils import secure_filename


app = Flask(__name__)


JOB_TTL_SECONDS = 60 * 60
MAX_JOBS = 25
ORPHAN_CLEANUP_EVERY_SECONDS = 5 * 60
_last_orphan_cleanup_at = 0.0
_jobs_lock = threading.Lock()
_jobs: dict[str, dict] = {}


INDEX_HTML = """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>MP4 Splitter</title>
    <style>
      body { font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif; margin: 32px; }
      .card { max-width: 720px; border: 1px solid #ddd; border-radius: 12px; padding: 20px; }
      .row { display: flex; gap: 12px; flex-wrap: wrap; }
      label { display: block; margin: 10px 0 6px; font-weight: 600; }
      input[type="file"], input[type="number"] { width: 100%; padding: 10px; border: 1px solid #ccc; border-radius: 10px; }
      .drop { border: 2px dashed #bbb; border-radius: 12px; padding: 18px; text-align: center; background: #fafafa; cursor: pointer; user-select: none; }
      .drop.drag { border-color: #111; background: #f3f3f3; }
      .drop-title { font-weight: 700; margin-bottom: 6px; }
      .drop-sub { color: #444; font-size: 14px; }
      .file-name { margin-top: 10px; font-size: 14px; color: #111; word-break: break-word; }
      button { margin-top: 16px; padding: 10px 14px; border: 0; background: #111; color: #fff; border-radius: 10px; cursor: pointer; }
      .hint { color: #444; font-size: 14px; margin-top: 8px; line-height: 1.35; }
      .err { color: #b00020; font-weight: 600; }
      .ok { color: #0b6b0b; font-weight: 600; }
      code { background: #f5f5f5; padding: 2px 6px; border-radius: 6px; }
    </style>
  </head>
  <body>
    <div class="card">
      <h1 style="margin-top:0">MP4 Splitter</h1>
      <p class="hint">Upload an MP4, choose a clip duration, preview the split clips, then download as ZIP.</p>
      %%STATUS_BLOCK%%
      <form action="/split" method="post" enctype="multipart/form-data">
        <label for="video">MP4 file</label>
        <div id="drop" class="drop" role="button" tabindex="0" aria-label="Drop MP4 here or click to select">
          <div class="drop-title">Drop MP4 here</div>
          <div class="drop-sub">or click to choose a file</div>
          <div id="fileName" class="file-name"></div>
        </div>
        <input id="video" name="video" type="file" accept="video/mp4" required style="display:none" />

        <div class="row">
          <div style="flex:1; min-width: 220px;">
            <label for="duration">Clip duration (seconds)</label>
            <input id="duration" name="duration" type="number" min="1" step="1" value="30" required />
          </div>
        </div>

        <div class="row" style="margin-top: 12px;">
          <label style="display:flex; gap:10px; align-items:center; font-weight: 500;">
            <input name="prores" type="checkbox" value="1" checked />
            Convert to ProRes 422 (MOV) for Final Cut (master clips), plus MP4 proxies for preview
          </label>
        </div>

        <button type="submit">Split & Preview</button>
      </form>
      <p class="hint">
        Requires <code>ffmpeg</code> on PATH. Example: 3 minutes = 180 seconds; duration 30 seconds → 6 clips.
      </p>
    </div>
    <script>
      const input = document.getElementById("video");
      const drop = document.getElementById("drop");
      const fileName = document.getElementById("fileName");

      function setName() {
        const f = input.files && input.files[0];
        fileName.textContent = f ? f.name : "";
      }

      drop.addEventListener("click", () => input.click());
      drop.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") input.click();
      });
      input.addEventListener("change", setName);

      drop.addEventListener("dragover", (e) => {
        e.preventDefault();
        drop.classList.add("drag");
      });
      drop.addEventListener("dragleave", () => drop.classList.remove("drag"));
      drop.addEventListener("drop", (e) => {
        e.preventDefault();
        drop.classList.remove("drag");
        const files = e.dataTransfer && e.dataTransfer.files;
        if (!files || files.length === 0) return;
        const f = files[0];
        const ok = (f.type && f.type === "video/mp4") || f.name.toLowerCase().endsWith(".mp4");
        if (!ok) {
          fileName.textContent = "Please drop a .mp4 file.";
          return;
        }
        input.files = files;
        setName();
      });
    </script>
  </body>
</html>
"""

PREVIEW_HTML = """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Preview Clips</title>
    <style>
      body { font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif; margin: 32px; }
      .card { max-width: 980px; border: 1px solid #ddd; border-radius: 12px; padding: 20px; }
      .row { display: flex; gap: 12px; flex-wrap: wrap; align-items: center; }
      .meta { color: #444; font-size: 14px; margin: 10px 0 18px; line-height: 1.35; }
      .actions a, .actions button { display: inline-block; margin-right: 10px; }
      a { color: #0b57d0; text-decoration: none; }
      a:hover { text-decoration: underline; }
      .btn { padding: 10px 14px; border-radius: 10px; border: 1px solid #111; background: #111; color: #fff; cursor: pointer; }
      .btn-ghost { padding: 10px 14px; border-radius: 10px; border: 1px solid #999; background: #fff; color: #111; cursor: pointer; }
      .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 14px; }
      .clip { border: 1px solid #ddd; border-radius: 12px; padding: 12px; }
      .clip-title { font-weight: 700; margin-bottom: 8px; }
      video { width: 100%; border-radius: 10px; background: #000; }
      .clip-links { margin-top: 8px; font-size: 14px; }
      .err { color: #b00020; font-weight: 600; }
    </style>
  </head>
  <body>
    <div class="card">
      <div class="row" style="justify-content: space-between;">
        <h1 style="margin:0">Preview</h1>
        <div class="actions">
          <a class="btn-ghost" href="/">New Split</a>
          <a class="btn" href="%%ZIP_URL%%">Download ZIP</a>
        </div>
      </div>
      <div class="meta">
        <div><strong>File:</strong> %%FILENAME%%</div>
        <div><strong>Clip duration:</strong> %%DURATION%% seconds</div>
        <div><strong>Clips:</strong> %%COUNT%%</div>
        <div><strong>Master format:</strong> %%MASTER_FMT%%</div>
      </div>
      %%CLIPS%%
      <form action="%%DELETE_URL%%" method="post" style="margin-top:18px;">
        <button class="btn-ghost" type="submit">Delete This Job</button>
      </form>
    </div>
  </body>
</html>
"""

PROCESSING_HTML = """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Processing…</title>
    <style>
      body { font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif; margin: 32px; }
      .card { max-width: 720px; border: 1px solid #ddd; border-radius: 12px; padding: 20px; }
      .meta { color: #444; font-size: 14px; margin: 10px 0 12px; line-height: 1.35; }
      .bar { height: 14px; background: #eee; border-radius: 999px; overflow: hidden; }
      .bar > div { height: 100%; background: #111; width: 0%; transition: width 250ms ease; }
      .row { display: flex; justify-content: space-between; align-items: center; gap: 12px; margin-top: 10px; }
      .err { color: #b00020; font-weight: 600; white-space: pre-wrap; }
      code { background: #f5f5f5; padding: 2px 6px; border-radius: 6px; }
      a { color: #0b57d0; text-decoration: none; }
      a:hover { text-decoration: underline; }
    </style>
  </head>
  <body>
    <div class="card">
      <h1 style="margin-top:0">Processing…</h1>
      <div class="meta">
        <div><strong>File:</strong> %%FILENAME%%</div>
        <div><strong>Clip duration:</strong> %%DURATION%% seconds</div>
        <div><strong>Master format:</strong> %%MASTER_FMT%%</div>
      </div>
      <div class="bar"><div id="barFill"></div></div>
      <div class="row">
        <div id="pct">0%</div>
        <div id="msg"></div>
      </div>
      <div id="err" class="err" style="margin-top: 14px;"></div>
      <div class="meta" style="margin-top: 14px;">
        Keep this tab open. When processing finishes, it will automatically switch to the preview page.
      </div>
      <div class="meta">
        If it looks stuck, check that <code>ffmpeg</code> works: <code>ffmpeg -version</code>.
      </div>
      <div class="meta"><a href="/">Start over</a></div>
    </div>

    <script>
      const jobId = "%%JOB_ID%%";
      const statusUrl = `/job/${jobId}/status`;
      const barFill = document.getElementById("barFill");
      const pct = document.getElementById("pct");
      const msg = document.getElementById("msg");
      const err = document.getElementById("err");

      function clamp(n) { return Math.max(0, Math.min(100, n)); }
      async function tick() {
        try {
          const res = await fetch(statusUrl, { cache: "no-store" });
          if (!res.ok) throw new Error(`status ${res.status}`);
          const data = await res.json();
          const p = clamp(Number(data.progress ?? 0));
          barFill.style.width = `${p}%`;
          pct.textContent = `${p}%`;
          msg.textContent = data.message || "";
          if (data.state === "done") {
            window.location.href = `/job/${jobId}`;
            return;
          }
          if (data.state === "error") {
            err.textContent = data.error || "Unknown error.";
            return;
          }
        } catch (e) {
          msg.textContent = "Connecting…";
        }
        setTimeout(tick, 800);
      }
      tick();
    </script>
  </body>
</html>
"""


def _ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


@lru_cache(maxsize=1)
def _resolve_ffmpeg_exe() -> tuple[str | None, str]:
    override = os.environ.get("FFMPEG_PATH", "").strip().strip('"')
    candidates: list[str] = []
    if override:
        candidates.append(override)

    if os.name == "nt":
        try:
            p = run(["where.exe", "ffmpeg"], check=False, capture_output=True, text=True)
            for line in (p.stdout or "").splitlines():
                line = line.strip().strip('"')
                if line:
                    candidates.append(line)
        except Exception:
            pass

    which_hit = shutil.which("ffmpeg")
    if which_hit:
        candidates.append(which_hit)

    seen: set[str] = set()
    unique_candidates: list[str] = []
    for c in candidates:
        cl = c.strip()
        if not cl:
            continue
        if cl in seen:
            continue
        seen.add(cl)
        unique_candidates.append(cl)

    if not unique_candidates:
        return None, "ffmpeg not found on PATH."

    attempts: list[str] = []
    for exe in unique_candidates:
        cmd = [exe, "-version"]
        try:
            p = run(cmd, check=False, capture_output=True, text=True)
        except Exception as e:
            attempts.append(f"- {exe}: {e}")
            continue
        if p.returncode == 0:
            return exe, ""
        attempts.append(f"- {exe}: exit code {p.returncode} ({hex(p.returncode & 0xFFFFFFFF)})")

    msg = "ffmpeg found, but none of the candidates worked.\nTried:\n" + "\n".join(attempts)
    msg += "\n\nFix: install a working Windows 64-bit ffmpeg build, or set FFMPEG_PATH to a known-good ffmpeg.exe."
    return None, msg


@lru_cache(maxsize=1)
def _ffmpeg_check() -> tuple[bool, str]:
    exe, msg = _resolve_ffmpeg_exe()
    if not exe:
        return False, msg
    return True, ""


def _render_index(status_block: str) -> str:
    return INDEX_HTML.replace("%%STATUS_BLOCK%%", status_block)


def _cleanup_jobs() -> None:
    global _last_orphan_cleanup_at
    now = time.time()
    with _jobs_lock:
        expired = [job_id for job_id, job in _jobs.items() if now - job["created_at"] > JOB_TTL_SECONDS]
        for job_id in expired:
            job = _jobs.pop(job_id, None)
            if job:
                shutil.rmtree(job["root"], ignore_errors=True)

        if len(_jobs) > MAX_JOBS:
            oldest = sorted(_jobs.items(), key=lambda kv: kv[1]["created_at"])[: max(0, len(_jobs) - MAX_JOBS)]
            for job_id, job in oldest:
                _jobs.pop(job_id, None)
                shutil.rmtree(job["root"], ignore_errors=True)

    if now - _last_orphan_cleanup_at < ORPHAN_CLEANUP_EVERY_SECONDS:
        return
    _last_orphan_cleanup_at = now

    tmp_dir = Path(tempfile.gettempdir())
    try:
        for p in tmp_dir.glob("mp4_split_job_*"):
            if not p.is_dir():
                continue
            try:
                age = now - p.stat().st_mtime
            except Exception:
                continue
            if age > JOB_TTL_SECONDS:
                shutil.rmtree(p, ignore_errors=True)
    except Exception:
        return


def _get_job(job_id: str) -> dict | None:
    with _jobs_lock:
        return _jobs.get(job_id)


def _create_job(
    root: Path,
    filename: str,
    segment_seconds: int,
    prores: bool,
) -> str:
    job_id = uuid4().hex
    job = {
        "id": job_id,
        "root": str(root),
        "filename": filename,
        "segment_seconds": segment_seconds,
        "prores": prores,
        "clips": [],
        "master_names": [],
        "preview_names": [],
        "master_label": "ProRes 422 (MOV)" if prores else "MP4 (re-encode)",
        "state": "processing",
        "progress": 0,
        "message": "Queued",
        "error": "",
        "created_at": time.time(),
    }
    with _jobs_lock:
        _jobs[job_id] = job
    return job_id


def _update_job(job_id: str, **fields) -> None:
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            return
        job.update(fields)


def _set_progress(job_id: str, progress: int, message: str | None = None) -> None:
    p = max(0, min(100, int(progress)))
    if message is None:
        _update_job(job_id, progress=p)
    else:
        _update_job(job_id, progress=p, message=message)


def _format_ffmpeg_failure(cmd: list[str], err: CalledProcessError) -> str:
    rc = err.returncode
    rc_hex = hex(rc & 0xFFFFFFFF)
    stderr = (getattr(err, "stderr", None) or "").strip()
    stdout = (getattr(err, "stdout", None) or "").strip()

    parts: list[str] = []
    parts.append(f"ffmpeg failed (exit code {rc} / {rc_hex}).")
    parts.append("Command:")
    parts.append(" ".join(cmd))
    if stderr:
        parts.append("")
        parts.append("stderr:")
        parts.append(stderr)
    if stdout:
        parts.append("")
        parts.append("stdout:")
        parts.append(stdout)

    if rc in (3221225785, 3221225781, 3221225477, 3221225601):
        parts.append("")
        parts.append("This looks like a Windows ffmpeg crash / missing dependency.")
        parts.append("Fix: reinstall ffmpeg (64-bit) from a trusted build (e.g. gyan.dev or BtbN) and install Microsoft Visual C++ Redistributable (x64).")

    return "\n".join(parts)

@lru_cache(maxsize=1)
def _resolve_ffprobe_exe() -> tuple[str | None, str]:
    ffmpeg_exe, msg = _resolve_ffmpeg_exe()
    if ffmpeg_exe:
        p = Path(ffmpeg_exe)
        probe_name = "ffprobe.exe" if os.name == "nt" else "ffprobe"
        candidate = str(p.with_name(probe_name))
        if Path(candidate).exists():
            return candidate, ""
    which_hit = shutil.which("ffprobe")
    if which_hit:
        return which_hit, ""
    return None, "ffprobe not found."


def _probe_duration_seconds(input_path: Path) -> float | None:
    exe, _ = _resolve_ffprobe_exe()
    if not exe:
        return None
    cmd = [
        exe,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(input_path),
    ]
    try:
        p = run(cmd, check=True, capture_output=True, text=True)
    except Exception:
        return None
    s = (p.stdout or "").strip()
    try:
        return float(s)
    except Exception:
        return None


def _run_ffmpeg_with_percent(
    cmd: list[str],
    duration_seconds: float | None,
    job_id: str,
    base_pct: int,
    span_pct: int,
    message: str,
) -> None:
    _set_progress(job_id, base_pct, message)
    if duration_seconds and duration_seconds > 0 and span_pct > 0:
        progress_cmd = cmd[:]
        progress_cmd[1:1] = ["-progress", "pipe:1", "-nostats"]
        p = Popen(progress_cmd, stdout=PIPE, stderr=PIPE, text=True)
        last_pct = -1
        while True:
            line = p.stdout.readline() if p.stdout else ""
            if line == "" and p.poll() is not None:
                break
            line = line.strip()
            if not line:
                continue
            if line.startswith("out_time_ms="):
                v = line.split("=", 1)[1].strip()
                try:
                    out_time_ms = int(v)
                except Exception:
                    continue
                frac = max(0.0, min(1.0, (out_time_ms / 1_000_000.0) / duration_seconds))
                pct = base_pct + int(frac * span_pct)
                if pct != last_pct:
                    last_pct = pct
                    _set_progress(job_id, pct, message)
        _, stderr = p.communicate()
        rc = p.wait()
        if rc != 0:
            raise RuntimeError(
                f"ffmpeg failed (exit code {rc} / {hex(rc & 0xFFFFFFFF)}).\nCommand:\n"
                + " ".join(cmd)
                + ("\n\nstderr:\n" + (stderr or "").strip() if stderr else "")
            )
        _set_progress(job_id, base_pct + span_pct, message)
        return

    try:
        run(cmd, check=True, capture_output=True, text=True)
    except CalledProcessError as e:
        raise RuntimeError(_format_ffmpeg_failure(cmd, e)) from e
    _set_progress(job_id, base_pct + span_pct, message)


def _split_by_time_copy(input_path: Path, out_dir: Path, segment_seconds: int, segment_format: str, ext: str) -> list[Path]:
    exe, msg = _resolve_ffmpeg_exe()
    if not exe:
        raise RuntimeError(msg)

    out_pattern = str(out_dir / f"clip_%03d.{ext}")
    cmd = [
        exe,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-i",
        str(input_path),
        "-map",
        "0",
        "-c",
        "copy",
        "-f",
        "segment",
        "-segment_time",
        str(segment_seconds),
        "-reset_timestamps",
        "1",
        "-segment_format",
        segment_format,
        out_pattern,
    ]
    try:
        run(cmd, check=True, capture_output=True, text=True)
    except CalledProcessError as e:
        raise RuntimeError(f"ffmpeg split failed:\n{_format_ffmpeg_failure(cmd, e)}") from e

    return sorted(out_dir.glob(f"clip_*.{ext}"))


def _transcode_to_prores_422(
    input_path: Path,
    out_path: Path,
    job_id: str,
    base_pct: int,
    span_pct: int,
) -> None:
    exe, msg = _resolve_ffmpeg_exe()
    if not exe:
        raise RuntimeError(msg)

    cmd = [
        exe,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-i",
        str(input_path),
        "-map",
        "0",
        "-c:v",
        "prores_ks",
        "-profile:v",
        "2",
        "-pix_fmt",
        "yuv422p10le",
        "-vendor",
        "apl0",
        "-c:a",
        "pcm_s16le",
        "-ar",
        "48000",
        str(out_path),
    ]
    duration = _probe_duration_seconds(input_path)
    _run_ffmpeg_with_percent(cmd, duration, job_id=job_id, base_pct=base_pct, span_pct=span_pct, message="Transcoding to ProRes…")


def _make_h264_proxy(
    input_path: Path,
    out_path: Path,
    job_id: str,
    base_pct: int,
    span_pct: int,
    message: str,
) -> None:
    exe, msg = _resolve_ffmpeg_exe()
    if not exe:
        raise RuntimeError(msg)

    cmd = [
        exe,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-i",
        str(input_path),
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "23",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "160k",
        "-movflags",
        "+faststart",
        str(out_path),
    ]
    duration = _probe_duration_seconds(input_path)
    _run_ffmpeg_with_percent(cmd, duration, job_id=job_id, base_pct=base_pct, span_pct=span_pct, message=message)


def _split_mp4_reencode(
    input_path: Path,
    out_dir: Path,
    segment_seconds: int,
    job_id: str,
    base_pct: int,
    span_pct: int,
) -> list[Path]:
    exe, msg = _resolve_ffmpeg_exe()
    if not exe:
        raise RuntimeError(msg)

    out_pattern = str(out_dir / "clip_%03d.mp4")
    cmd = [
        exe,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-i",
        str(input_path),
        "-map",
        "0",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
        "-sc_threshold",
        "0",
        "-force_key_frames",
        f"expr:gte(t,n_forced*{segment_seconds})",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
        "-f",
        "segment",
        "-segment_time",
        str(segment_seconds),
        "-reset_timestamps",
        "1",
        out_pattern,
    ]
    duration = _probe_duration_seconds(input_path)
    _run_ffmpeg_with_percent(cmd, duration, job_id=job_id, base_pct=base_pct, span_pct=span_pct, message="Re-encoding & splitting…")
    return sorted(out_dir.glob("clip_*.mp4"))


def _split_mp4_with_ffmpeg(input_path: Path, out_dir: Path, segment_seconds: int) -> list[Path]:
    return _split_by_time_copy(input_path=input_path, out_dir=out_dir, segment_seconds=segment_seconds, segment_format="mp4", ext="mp4")


@app.before_request
def _before_request() -> None:
    _cleanup_jobs()


@app.get("/")
def index() -> Response:
    ok, msg = _ffmpeg_check()
    if ok:
        status_block = '<p class="ok">Ready.</p>'
    else:
        status_block = '<p class="err">ffmpeg is not available or not working. Install/reinstall ffmpeg and restart the server.</p>'
    return Response(_render_index(status_block=status_block), mimetype="text/html")


@app.get("/job/<job_id>")
def job_preview(job_id: str) -> Response:
    job = _get_job(job_id)
    if not job:
        abort(404)

    if job.get("state") != "done":
        html = PROCESSING_HTML
        html = html.replace("%%JOB_ID%%", escape(job_id))
        html = html.replace("%%FILENAME%%", escape(job.get("filename", "")))
        html = html.replace("%%DURATION%%", str(job.get("segment_seconds", "")))
        html = html.replace("%%MASTER_FMT%%", escape(job.get("master_label", "")))
        return Response(html, mimetype="text/html")

    clips_html_parts: list[str] = []
    clips_html_parts.append('<div class="grid">')
    for clip in job["clips"]:
        preview_url = url_for("get_preview_clip", job_id=job_id, clip_name=clip["preview"])
        master_url = url_for("get_master_clip", job_id=job_id, clip_name=clip["master"])
        clip_title = escape(clip["master"])
        clips_html_parts.append(
            '\n'.join(
                [
                    '<div class="clip">',
                    f'<div class="clip-title">{clip_title}</div>',
                    f'<video controls preload="metadata" src="{preview_url}"></video>',
                    f'<div class="clip-links"><a href="{master_url}" download>Download Master</a></div>',
                    "</div>",
                ]
            )
        )
    clips_html_parts.append("</div>")

    html = PREVIEW_HTML
    html = html.replace("%%ZIP_URL%%", url_for("download_zip", job_id=job_id))
    html = html.replace("%%DELETE_URL%%", url_for("delete_job", job_id=job_id))
    html = html.replace("%%FILENAME%%", escape(job["filename"]))
    html = html.replace("%%DURATION%%", str(job["segment_seconds"]))
    html = html.replace("%%COUNT%%", str(len(job["clips"])))
    html = html.replace("%%MASTER_FMT%%", escape(job["master_label"]))
    html = html.replace("%%CLIPS%%", "\n".join(clips_html_parts))
    return Response(html, mimetype="text/html")


@app.get("/job/<job_id>/status")
def job_status(job_id: str) -> Response:
    job = _get_job(job_id)
    if not job:
        abort(404)
    return jsonify(
        {
            "id": job_id,
            "state": job.get("state", "processing"),
            "progress": int(job.get("progress", 0)),
            "message": job.get("message", ""),
            "error": job.get("error", ""),
        }
    )


def _process_job(job_id: str) -> None:
    job = _get_job(job_id)
    if not job:
        return

    root = Path(job["root"])
    filename = job["filename"]
    segment_seconds = int(job["segment_seconds"])
    prores = bool(job.get("prores", False))
    input_path = root / filename
    master_dir = root / "master"
    preview_dir = root / "preview"

    try:
        _set_progress(job_id, 1, "Starting…")
        master_dir.mkdir(parents=True, exist_ok=True)
        preview_dir.mkdir(parents=True, exist_ok=True)

        if prores:
            prores_path = root / "intermediate_prores.mov"
            _transcode_to_prores_422(
                input_path=input_path,
                out_path=prores_path,
                job_id=job_id,
                base_pct=0,
                span_pct=60,
            )

            _set_progress(job_id, 62, "Splitting ProRes…")
            master_clips = _split_by_time_copy(
                input_path=prores_path,
                out_dir=master_dir,
                segment_seconds=segment_seconds,
                segment_format="mov",
                ext="mov",
            )

            if not master_clips:
                raise RuntimeError("No clips were produced.")

            clips: list[dict[str, str]] = []
            n = len(master_clips)
            for i, master_clip in enumerate(master_clips):
                base = 70 + int((i * 30) / max(1, n))
                span = max(1, int(30 / max(1, n)))
                proxy_name = master_clip.with_suffix(".mp4").name
                proxy_path = preview_dir / proxy_name
                _make_h264_proxy(
                    input_path=master_clip,
                    out_path=proxy_path,
                    job_id=job_id,
                    base_pct=base,
                    span_pct=span,
                    message=f"Creating proxy {i + 1}/{n}…",
                )
                clips.append({"master": master_clip.name, "preview": proxy_name})

            _update_job(job_id, master_label="ProRes 422 (MOV)")
        else:
            _set_progress(job_id, 10, "Starting MP4 re-encode split…")
            master_clips = _split_mp4_reencode(
                input_path=input_path,
                out_dir=master_dir,
                segment_seconds=segment_seconds,
                job_id=job_id,
                base_pct=10,
                span_pct=70,
            )
            if not master_clips:
                raise RuntimeError("No clips were produced.")

            clips = [{"master": p.name, "preview": p.name} for p in master_clips]
            _set_progress(job_id, 90, "Preparing preview…")
            for p in master_clips:
                shutil.copy2(p, preview_dir / p.name)
            _update_job(job_id, master_label="MP4 (re-encode)")

        master_names = [c["master"] for c in clips]
        preview_names = [c["preview"] for c in clips]
        _update_job(job_id, clips=clips, master_names=master_names, preview_names=preview_names)
        _update_job(job_id, state="done", progress=100, message="Done")

        try:
            if input_path.exists():
                input_path.unlink()
        except Exception:
            pass
        try:
            prores_path = root / "intermediate_prores.mov"
            if prores_path.exists():
                prores_path.unlink()
        except Exception:
            pass
    except Exception as e:
        _update_job(job_id, state="error", error=str(e), message="Error")


@app.get("/preview/<job_id>/<clip_name>")
def get_preview_clip(job_id: str, clip_name: str) -> Response:
    job = _get_job(job_id)
    if not job:
        abort(404)
    if clip_name not in set(job["preview_names"]):
        abort(404)
    clip_path = Path(job["root"]) / "preview" / clip_name
    if not clip_path.exists():
        abort(404)
    return send_file(str(clip_path), mimetype="video/mp4", as_attachment=False, max_age=0)


@app.get("/master/<job_id>/<clip_name>")
def get_master_clip(job_id: str, clip_name: str) -> Response:
    job = _get_job(job_id)
    if not job:
        abort(404)
    if clip_name not in set(job["master_names"]):
        abort(404)
    clip_path = Path(job["root"]) / "master" / clip_name
    if not clip_path.exists():
        abort(404)
    mimetype = "video/quicktime" if clip_path.suffix.lower() == ".mov" else "video/mp4"
    return send_file(str(clip_path), mimetype=mimetype, as_attachment=False, max_age=0)


@app.get("/download/<job_id>.zip")
def download_zip(job_id: str) -> Response:
    job = _get_job(job_id)
    if not job:
        abort(404)

    zip_buf = io.BytesIO()
    clips_dir = Path(job["root"]) / "master"
    with zipfile.ZipFile(zip_buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name in job["master_names"]:
            p = clips_dir / name
            if p.exists():
                zf.write(p, arcname=name)
    zip_buf.seek(0)

    base = Path(job["filename"]).stem
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    download_name = f"{base}_clips_{job['segment_seconds']}s_{ts}.zip"
    return send_file(
        zip_buf,
        mimetype="application/zip",
        as_attachment=True,
        download_name=download_name,
        max_age=0,
    )


@app.post("/delete/<job_id>")
def delete_job(job_id: str) -> Response:
    with _jobs_lock:
        job = _jobs.pop(job_id, None)
    if job:
        shutil.rmtree(job["root"], ignore_errors=True)
    return redirect(url_for("index"))


@app.post("/split")
def split() -> Response:
    ok, msg = _ffmpeg_check()
    if not ok:
        return Response(msg, status=500, mimetype="text/plain")

    if "video" not in request.files:
        return Response("Missing 'video' upload field.", status=400, mimetype="text/plain")

    file = request.files["video"]
    if not file.filename:
        return Response("No file selected.", status=400, mimetype="text/plain")

    try:
        segment_seconds = int(request.form.get("duration", "30"))
    except ValueError:
        return Response("Invalid duration.", status=400, mimetype="text/plain")

    if segment_seconds < 1:
        return Response("Duration must be >= 1 second.", status=400, mimetype="text/plain")

    safe_name = secure_filename(file.filename)
    if not safe_name.lower().endswith(".mp4"):
        return Response("Only .mp4 is supported.", status=400, mimetype="text/plain")

    prores = request.form.get("prores", "").strip() == "1"

    job_root = Path(tempfile.mkdtemp(prefix="mp4_split_job_"))
    input_path = job_root / safe_name
    master_dir = job_root / "master"
    preview_dir = job_root / "preview"
    master_dir.mkdir(parents=True, exist_ok=True)
    preview_dir.mkdir(parents=True, exist_ok=True)

    try:
        file.save(str(input_path))
    except Exception:
        shutil.rmtree(job_root, ignore_errors=True)
        raise

    job_id = _create_job(root=job_root, filename=safe_name, segment_seconds=segment_seconds, prores=prores)
    _set_progress(job_id, 0, "Queued")
    t = threading.Thread(target=_process_job, args=(job_id,), daemon=True)
    t.start()
    return redirect(url_for("job_preview", job_id=job_id))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True)
