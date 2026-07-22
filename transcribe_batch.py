"""
Batch-transcribe MP4 recordings to VTT using faster-whisper.

Usage:
    python transcribe_batch.py --source "C:\\path\\to\\recordings" --output "C:\\path\\to\\transcripts"
    python transcribe_batch.py --source "..." --output "..." --model medium
    python transcribe_batch.py --source "..." --output "..." --recurse
    python transcribe_batch.py --source "..." --output "..." --timeout 7200
    python transcribe_batch.py --source "..." --output "..." --staging "C:\\tmp\\staging"

Resume-safe: skips files whose .vtt already exists in --output.
Times out per-file after --timeout seconds (default 5400 = 90 min), logs to NEEDS_FOLLOWUP.txt.

--staging: copy each MP4 to a local staging folder before transcribing, then delete it.
  Use this when --source is a network/OneDrive path to avoid keeping large video files locally.
  Only one file is staged at a time; disk headroom needed = one MP4 + working space (~1 GB typical).

Requires:
    pip install faster-whisper
    ffmpeg in PATH (winget install Gyan.FFmpeg)
"""

import argparse
import multiprocessing
import os
import re
import shutil
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Config defaults
# ---------------------------------------------------------------------------

DEFAULT_MODEL   = "small"
DEFAULT_TIMEOUT = 5400   # seconds — 90 min; a 1-hr meeting at worst-case 0.5x + margin
DEFAULT_PATTERN = "*.mp4"


# ---------------------------------------------------------------------------
# Transcription worker (runs in a subprocess so it's killable on timeout)
# ---------------------------------------------------------------------------

def _transcribe_worker(source_path: str, output_dir: str, model_name: str, result_queue):
    """
    Spawned in a separate process. Writes result or error string to result_queue.
    Imports are inside the function so the parent process doesn't need faster-whisper
    to be imported before spawning (avoids issues with multiprocessing 'spawn' start
    method on Windows).
    """
    try:
        from faster_whisper import WhisperModel

        model = WhisperModel(model_name, device="cpu", compute_type="int8")
        segments, info = model.transcribe(
            source_path,
            language="en",
            beam_size=5,
            vad_filter=True,           # skip silent gaps — speeds up long recordings
            vad_parameters={"min_silence_duration_ms": 500},
        )

        # Build VTT content
        lines = ["WEBVTT", ""]
        for seg in segments:
            start = _format_vtt_time(seg.start)
            end   = _format_vtt_time(seg.end)
            # faster-whisper doesn't provide speaker labels without diarization;
            # emit clean cues so downstream parsing still works
            text  = seg.text.strip()
            if text:
                lines.append(f"{start} --> {end}")
                lines.append(text)
                lines.append("")

        stem    = Path(source_path).stem
        out_vtt = Path(output_dir) / f"{stem}.vtt"
        out_vtt.write_text("\n".join(lines), encoding="utf-8")
        result_queue.put(("ok", str(out_vtt)))

    except Exception as exc:
        result_queue.put(("error", str(exc)))


def _format_vtt_time(seconds: float) -> str:
    ms  = int(round(seconds * 1000))
    s   = ms // 1000
    ms  = ms % 1000
    m   = s // 60
    s   = s % 60
    h   = m // 60
    m   = m % 60
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------

def _log(log_path: Path, msg: str) -> None:
    line = f"{datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')} | {msg}"
    print(line, flush=True)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _log_followup(followup_path: Path, filename: str, reason: str) -> None:
    line = f"{datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')} | {filename} | {reason}"
    with open(followup_path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


# ---------------------------------------------------------------------------
# Dependency checks
# ---------------------------------------------------------------------------

def _check_deps() -> None:
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        sys.exit("faster-whisper not installed. Run: pip install faster-whisper")

    if not shutil.which("ffmpeg"):
        sys.exit("ffmpeg not found in PATH. Run: winget install Gyan.FFmpeg  then restart shell.")


def _has_audio(path: Path) -> bool:
    """Return True if ffprobe detects at least one audio stream."""
    import subprocess
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-select_streams", "a",
             "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        return bool(result.stdout.strip())
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Batch-transcribe MP4s to VTT using faster-whisper")
    parser.add_argument("--source",  required=True, help="Folder containing .mp4 files")
    parser.add_argument("--output",  required=True, help="Folder to write .vtt files into")
    parser.add_argument("--model",   default=DEFAULT_MODEL,
                        help=f"Whisper model: tiny/small/medium/large-v2 (default: {DEFAULT_MODEL})")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT,
                        help=f"Seconds before a single file is abandoned (default: {DEFAULT_TIMEOUT})")
    parser.add_argument("--recurse", action="store_true",
                        help="Walk subfolders of --source")
    parser.add_argument("--staging", default=None,
                        help="Local folder to stage one MP4 at a time (copy-transcribe-delete). "
                             "Use when --source is a network/OneDrive path to avoid filling local disk.")
    args = parser.parse_args()

    source = Path(args.source)
    output = Path(args.output)
    staging = Path(args.staging) if args.staging else None

    if not source.exists():
        sys.exit(f"Source folder not found: {source}")

    output.mkdir(parents=True, exist_ok=True)
    if staging:
        staging.mkdir(parents=True, exist_ok=True)
    log_path      = output / "transcribe_batch_log.txt"
    followup_path = output / "NEEDS_FOLLOWUP.txt"

    _check_deps()

    # Discover files
    glob = "**/*.mp4" if args.recurse else "*.mp4"
    files = sorted(source.glob(glob))

    if not files:
        sys.exit(f"No .mp4 files found in {source}")

    total   = len(files)
    done    = 0
    skipped = 0
    failed  = 0

    _log(log_path, f"=== transcribe_batch start === source={source} output={output} model={args.model} timeout={args.timeout}s files={total}")

    # Windows requires 'spawn'; set explicitly so behaviour is consistent.
    ctx = multiprocessing.get_context("spawn")

    for i, file in enumerate(files, start=1):
        stem    = file.stem
        vtt_out = output / f"{stem}.vtt"

        prefix = f"[{i}/{total}]"

        if vtt_out.exists():
            _log(log_path, f"{prefix} [skip] {file.name} — already transcribed")
            skipped += 1
            continue

        # Stage the file locally if requested (copy from network, delete after)
        local_file = file
        if staging:
            local_file = staging / file.name
            _log(log_path, f"{prefix} [copy] {file.name} -> {staging} ...")
            copy_ok = False
            for attempt in range(1, 4):
                try:
                    shutil.copy2(str(file), str(local_file))
                    copy_ok = True
                    break
                except Exception as exc:
                    if attempt < 3:
                        wait = 2 ** attempt
                        _log(log_path, f"{prefix} [copy-retry {attempt}/3] {file.name} — {exc} — retrying in {wait}s ...")
                        time.sleep(wait)
                    else:
                        _log(log_path, f"{prefix} [error] {file.name} — copy to staging failed after 3 attempts: {exc}")
                        _log_followup(followup_path, file.name, f"COPY ERROR: {exc}")
            if not copy_ok:
                failed += 1
                continue

        if not _has_audio(local_file):
            _log(log_path, f"{prefix} [no-audio] {file.name} — no audio stream, skipping")
            if staging and local_file.exists():
                local_file.unlink(missing_ok=True)
            skipped += 1
            continue

        _log(log_path, f"{prefix} [transcribe] {file.name} ...")
        t_start = time.monotonic()

        result_queue = ctx.Queue()
        proc = ctx.Process(
            target=_transcribe_worker,
            args=(str(local_file), str(output), args.model, result_queue),
            daemon=True,
        )
        proc.start()
        proc.join(timeout=args.timeout)

        if proc.is_alive():
            proc.kill()
            proc.join()
            elapsed = int(time.monotonic() - t_start)
            msg = f"TIMEOUT after {elapsed}s (limit {args.timeout}s)"
            _log(log_path, f"{prefix} [timeout] {file.name} — {msg}")
            _log_followup(followup_path, file.name, msg)
            failed += 1
            if vtt_out.exists():
                vtt_out.unlink(missing_ok=True)
            if staging and local_file.exists():
                local_file.unlink(missing_ok=True)
            continue

        try:
            status, detail = result_queue.get_nowait()
        except Exception:
            status, detail = "error", "worker exited with no result"

        elapsed = int(time.monotonic() - t_start)

        if staging and local_file.exists():
            local_file.unlink(missing_ok=True)
            _log(log_path, f"{prefix} [staged-cleanup] {local_file.name} removed from staging")
            # Brief pause so OneDrive sync client settles before the next copy
            time.sleep(2)

        if status == "ok":
            done += 1
            _log(log_path, f"{prefix} [done] {file.name} -> {detail} ({elapsed}s)")
        else:
            failed += 1
            _log(log_path, f"{prefix} [error] {file.name} — {detail}")
            _log_followup(followup_path, file.name, f"ERROR: {detail}")

    _log(log_path, f"=== transcribe_batch complete === done={done} skipped={skipped} failed={failed}/{total}")

    if failed:
        print(f"\nFailed/timed-out files logged to: {followup_path}")


if __name__ == "__main__":
    # Windows multiprocessing requires this guard
    multiprocessing.freeze_support()
    main()
