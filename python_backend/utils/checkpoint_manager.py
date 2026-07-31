"""
Checkpoint Manager
------------------
On AgentVision close, compresses all per-program data into numbered
tar.gz checkpoints stored in:
  <AV_ROOT>/checkpoints/<profile_name>/checkpoint_00001.tar.gz

Each checkpoint contains:
  - Screenshots (PNG/annotated PNG) from the program's snapshot folder
  - Log files: log.txt, actions.jsonl, raw_output.txt
  - The .agentvision/state.json snapshot

After compression, originals are deleted to save space.
Idempotent: safe to call multiple times. Skips if nothing to archive.

DELETION IS CONDITIONAL ON THE ARCHIVE COPY EXISTING
----------------------------------------------------
The GUI used to tell the user "nothing is permanently lost — every clear creates
a numbered checkpoint". This module is the only thing that could have made that
sentence true, and it did not: it deleted the list of files it MEANT to archive. Every
`tar.add` was wrapped in `except: print warning`, the tarball was never read
back, and the delete loop then unlinked the collected list unconditionally. So
a file that failed to enter the archive (permission denied, vanished mid-walk —
AgentVision's own capture writes into these same folders — locked on Windows)
was deleted anyway, and the user had been told it was saved.

Worse, it was deterministic and silent: `_arcname` flattened everything outside
AV_ROOT to `<profile>/<basename>`, so `log/stats/session.log` and
`log/crashes/session.log` became the SAME member name. tar stored both,
extraction kept the last, and both originals were unlinked. Measured on a
fixture: 11 files deleted, 3 of them unrecoverable.

So: nothing is deleted until the tarball has been RE-OPENED and the member that
proves preservation has been found in it, exactly once, at the same size as the
file on disk. Bookkeeping is not evidence; the archive on disk is. Anything that
fails that check is kept and reported. The cost is one extra decompressing pass
over the checkpoint we just wrote, which is cheap next to deleting a user's data
because a warning scrolled past.

Two smaller rules follow from the same principle:
  * Live logs (log.txt / actions.jsonl / raw_output.txt) are TRUNCATED, not
    unlinked. The debugged program usually still holds an fd on them; unlinking
    on macOS leaves it writing to an invisible inode and silently discards
    everything it logs afterwards.
  * `.av_frame_seq` is archived but never removed. It is the frame counter —
    deleting it restarts numbering at 1, and the next run then overwrites frames
    that survived this checkpoint under the names they already used.

WHAT VERIFICATION CANNOT FIX — SCOPE
------------------------------------
The promise was false in a second way that no care inside this module addresses:
the collector reads `<project_root>/snapshots/`, while the bridge writes frames
to `<project_root>/agentvision/<profile>/` (bridge_server._base_for +
program_connector.profile_output_folder). Those are different directories, the
first exists for none of the configured profiles, and the second is what the
GUI's Clear Data button deletes — 35,746 files / 1.0 GB on this machine, none of
it in any checkpoint. `checkpoints/` here has never held a single archive.

That is deliberately NOT fixed by widening the collector. Clearing a gigabyte by
first writing a gigabyte tarball onto the same filesystem frees nothing, and on
a disk that has already been filled by AgentVision's own dumps it is its own
hazard. Instead the dialog now states which files are archived and which are
deleted for good, with counts taken from `_collect_files` — see
gui/agent_vision_gui.py:_clear_output_folder.
"""
from __future__ import annotations

import io
import json
import os
import re
import tarfile
from datetime import datetime, timezone
from pathlib import Path

AV_ROOT = Path(__file__).resolve().parent.parent.parent
CHECKPOINTS_ROOT = AV_ROOT / "checkpoints"

#: Filename prefixes AgentVision itself writes into a `snapshots/` folder.
#: A project may have its OWN snapshots/ directory full of PNGs that
#: AgentVision never created; those are not ours to archive or delete.
_AV_SNAPSHOT_PREFIXES = ("frame_", "shot_")

#: Logs the debugged program keeps an open fd on: truncate, never unlink.
_LIVE_LOG_NAMES = {"log.txt", "actions.jsonl", "raw_output.txt"}


def _is_av_snapshot(p: Path) -> bool:
    return p.name.startswith(_AV_SNAPSHOT_PREFIXES)


_CKPT_NUM_RE = re.compile(r"^checkpoint_(\d+)\.tar\.gz$")


def _next_checkpoint_number(folder: Path) -> int:
    """Return a checkpoint number that is not already taken in this folder.

    The old implementation read `existing[-1].stem` and commented it as
    "checkpoint_00001" — but the stem of `checkpoint_00001.tar.gz` is
    `checkpoint_00001.tar`, so `int("00001.tar")` ALWAYS raised and the
    `len(existing) + 1` fallback was the only branch that ever ran. Numbering was
    therefore "how many files are in here, plus one", which collides the moment a
    checkpoint is deleted — i.e. the moment someone frees disk space, which is the
    whole reason this folder gets pruned.

    Reproduced: with 00001/00002/00003 present, deleting 00001 makes the next
    number 3, and the tarball was opened "w:gz" — truncating a good archive of
    the user's data in place, in a module whose own guarantee was that
    "sequential numbering never overwrites an existing checkpoint".

    Parse the NAME, take the highest, and add one. Hand-named archives that do
    not match are ignored, which is safe: we only ever create numbered names.
    """
    highest = 0
    try:
        for p in folder.glob("checkpoint_*.tar.gz"):
            m = _CKPT_NUM_RE.match(p.name)
            if m:
                highest = max(highest, int(m.group(1)))
    except Exception:
        pass
    return highest + 1


def _collect_files(profile) -> tuple[list[Path], list[Path]]:
    """Collect all archivable files for a profile.

    Returns (runtime_files, config_files):
      runtime_files — deleted after successful archive (screenshots, logs, sidecars)
      config_files  — archived but NEVER deleted (schema, state, attached marker, config/)
    """
    runtime: list[Path] = []
    config:  list[Path] = []

    root = (getattr(profile, "project_root", "") or "").strip()
    root_path = Path(root) if root and Path(root).exists() else None

    if root_path:
        # ── Screenshots in project snapshots dir ──────────────────────────
        # ONLY files AgentVision itself writes. `snapshots/` may be the
        # project's own directory: globbing *.png there collected (and then
        # deleted) artwork and reference images AgentVision never created.
        snap_dir = root_path / "snapshots"
        if snap_dir.exists():
            runtime += [p for p in snap_dir.glob("*.png") if _is_av_snapshot(p)]
            runtime += list(snap_dir.glob("frame_*_annotated.png"))
            runtime += list(snap_dir.glob("frame_*_frame.json"))
            runtime += list(snap_dir.glob("frame_*_annotations.json"))

        # ── Log files ─────────────────────────────────────────────────────
        log_dir = root_path / "log"
        if log_dir.exists():
            for fname in ("log.txt", "actions.jsonl", "raw_output.txt"):
                p = log_dir / fname
                if p.exists() and p.stat().st_size > 0:
                    runtime.append(p)
            # The frame counter is archived but NEVER removed — see module
            # docstring: resetting it makes the next run overwrite the frames
            # that survived this checkpoint.
            seq_p = log_dir / ".av_frame_seq"
            if seq_p.exists():
                config.append(seq_p)
            # stats/ and crashes/ — all files inside, recursively
            for sub in ("stats", "crashes"):
                sub_dir = log_dir / sub
                if sub_dir.exists():
                    runtime += [f for f in sub_dir.rglob("*") if f.is_file()]

        # ── .agentvision/ — config, archived but not deleted ──────────────
        av_dir = root_path / ".agentvision"
        for fname in ("state.json", "schema.json", "attached.json"):
            p = av_dir / fname
            if p.exists():
                config.append(p)

        # ── config/ directory — archive only, not deleted ─────────────────
        cfg_dir = root_path / "config"
        if cfg_dir.exists():
            config += [f for f in cfg_dir.rglob("*") if f.is_file()]

    # ── AV-side snapshots (bridge writes here) ────────────────────────────
    profile_name = getattr(profile, "name", "") or "unknown"
    av_snap = AV_ROOT / "snapshots" / profile_name
    if av_snap.exists():
        runtime += list(av_snap.glob("*.png"))
        runtime += list(av_snap.glob("frame_*_frame.json"))
        runtime += list(av_snap.glob("frame_*_annotations.json"))

    # Flat snapshots dir (bridge default when no profile subfolder)
    flat_snap = AV_ROOT / "snapshots"
    if flat_snap.exists():
        runtime += [f for f in flat_snap.glob("shot_*.png") if f.is_file()]
        runtime += [f for f in flat_snap.glob("frame_*_frame.json") if f.is_file()]
        runtime += [f for f in flat_snap.glob("frame_*_annotations.json") if f.is_file()]

    # ── Deduplicate each list independently ──────────────────────────────
    def _dedup(lst: list[Path]) -> list[Path]:
        seen: set[Path] = set()
        out: list[Path] = []
        for f in lst:
            r = f.resolve()
            if r not in seen:
                seen.add(r)
                out.append(f)
        return out

    return _dedup(runtime), _dedup(config)


_SOURCE_EXTS = {".py", ".json", ".ini", ".yaml", ".yml", ".toml", ".cfg", ".md", ".txt"}
_SOURCE_SKIP_DIRS = {
    ".git", "__pycache__", ".mypy_cache", ".pytest_cache", "node_modules",
    "venv", ".venv", "env", ".env", "dist", "build", ".tox", "checkpoints",
    "snapshots", "log", "crashes",
}
_SOURCE_SIZE_CAP = 50 * 1024 * 1024  # 50 MB total source cap


def _collect_source_files(profile) -> list[Path]:
    """Collect source code files from the project root.
    Only text/config file types. Skips generated/cache/runtime dirs.
    Hard cap: stop adding once accumulated size exceeds 50 MB (pre-compression).
    Returns list of Paths. These are NEVER deleted after archiving."""
    root = (getattr(profile, "project_root", "") or "").strip()
    if not root or not Path(root).exists():
        return []

    root_path = Path(root).resolve()
    collected: list[Path] = []
    total_size = 0

    for p in sorted(root_path.rglob("*")):
        if not p.is_file():
            continue
        # Skip if any parent dir is in the skip list
        if any(part in _SOURCE_SKIP_DIRS for part in p.parts):
            continue
        if p.suffix.lower() not in _SOURCE_EXTS:
            continue
        try:
            sz = p.stat().st_size
        except Exception:
            continue
        if total_size + sz > _SOURCE_SIZE_CAP:
            break
        collected.append(p)
        total_size += sz

    return collected


def _arcname(filepath: Path, profile_name: str,
             root_path: Path | None = None) -> str:
    """Generate a clean archive path for a file — one member name per file.

    The old fallback was `f"{profile_name}/{filepath.name}"`, i.e. the BASENAME,
    for everything outside AV_ROOT. That is every file in the user's project, so
    `log/stats/session.log` and `log/crashes/session.log` produced the same
    member name: tar stored both, extraction kept one, and the delete loop
    removed both originals. Keeping the path relative to the project root makes
    the mapping injective, and anything relative to neither root is namespaced
    by its full path so it cannot collide either.
    """
    resolved = filepath.resolve()
    try:
        return str(resolved.relative_to(AV_ROOT.resolve()))
    except ValueError:
        pass
    if root_path is not None:
        try:
            return f"{profile_name}/{resolved.relative_to(Path(root_path).resolve())}"
        except ValueError:
            pass
    # Outside both roots: namespace by the whole path rather than the basename.
    flat = str(resolved).lstrip("/").replace(":", "_")
    return f"{profile_name}/_external/{flat}"


def collected_runtime_paths(profile) -> set[Path]:
    """Which files a checkpoint of `profile` would archive-and-remove.

    Public so a caller can tell the user which of the files it is about to delete
    are covered by a checkpoint and which are not, instead of asserting a
    guarantee. See gui/agent_vision_gui.py:_clear_output_folder.
    """
    try:
        runtime, _config = _collect_files(profile)
        return {p.resolve() for p in runtime}
    except Exception:
        return set()


def _verify_archived(ckpt_path: Path, added: list[tuple[Path, str]]
                     ) -> tuple[list[tuple[Path, str]], list[tuple[Path, str]]]:
    """Re-open the checkpoint and ask the ARCHIVE which files it contains.

    Returns (verified, unverified). A file is verified only when the tarball
    holds its member name EXACTLY ONCE and that member's size matches the file
    still on disk. Both halves matter:

      * "exactly once" is what catches an arcname collision. Two files sharing a
        member name means extraction yields one of them, so neither original is
        safe to delete.
      * "same size" is what catches a file that changed after it was archived —
        a live log that grew, a frame rewritten by the capture loop. The archive
        copy is then not this file, so this file stays.

    Anything we cannot read back is treated as absent. A checkpoint we cannot
    open is not a checkpoint.
    """
    if not added:
        return [], []
    try:
        with tarfile.open(ckpt_path, "r:gz") as tar:
            counts: dict[str, int] = {}
            sizes: dict[str, int] = {}
            for m in tar.getmembers():
                if not m.isfile():
                    continue
                counts[m.name] = counts.get(m.name, 0) + 1
                sizes[m.name] = m.size
    except Exception as exc:
        return [], [(f, f"checkpoint could not be re-opened: {exc}")
                    for f, _a in added]

    verified: list[tuple[Path, str]] = []
    unverified: list[tuple[Path, str]] = []
    for f, arc in added:
        n = counts.get(arc, 0)
        if n == 0:
            unverified.append((f, f"member {arc!r} absent from the checkpoint"))
            continue
        if n > 1:
            unverified.append((f, f"member name {arc!r} is shared by {n} files — "
                                  f"extraction would keep only one"))
            continue
        try:
            on_disk = f.stat().st_size
        except Exception as exc:
            unverified.append((f, f"could not stat before delete: {exc}"))
            continue
        if int(sizes.get(arc, -1)) != int(on_disk):
            unverified.append((f, f"file changed after archiving "
                                  f"({on_disk}B on disk vs {sizes.get(arc)}B "
                                  f"archived)"))
            continue
        verified.append((f, arc))
    return verified, unverified


def _remove_archived(verified: list[tuple[Path, str]], *, verbose: bool = True
                     ) -> tuple[list[str], list[str], list[tuple[Path, str]]]:
    """Reclaim the space for files proven to be in the checkpoint.

    Live logs are truncated in place rather than unlinked: the debugged program
    normally still holds an open fd, and an unlinked file on macOS/Linux keeps
    receiving its output into an inode nobody can see again.
    """
    deleted: list[str] = []
    truncated: list[str] = []
    kept: list[tuple[Path, str]] = []
    for f, _arc in verified:
        try:
            if f.name in _LIVE_LOG_NAMES:
                os.truncate(f, 0)
                truncated.append(str(f))
            else:
                f.unlink()
                deleted.append(str(f))
        except Exception as exc:
            kept.append((f, f"could not remove: {exc}"))
            if verbose:
                print(f"[checkpoint] warning: could not remove {f}: {exc}")
    return deleted, truncated, kept


def _write_removal_manifest(ckpt_path: Path, profile_name: str, num: int, *,
                            deleted: list[str], truncated: list[str],
                            kept: list[tuple[Path, str]]) -> None:
    """Record what was ACTUALLY removed, next to the checkpoint.

    The manifest inside the tarball lists what we intended to archive, which is
    the wrong record for reconstructing a loss. This one is written after the
    fact, is readable without extracting anything, and names every file that was
    kept and why.
    """
    try:
        side = ckpt_path.with_name(f"checkpoint_{num:05d}.removed.json")
        side.write_text(json.dumps({
            "profile": profile_name,
            "checkpoint": num,
            "checkpoint_archive": ckpt_path.name,
            "written_at": datetime.now(timezone.utc).isoformat(),
            "verified_in_archive_then_deleted": deleted,
            "verified_in_archive_then_truncated": truncated,
            "kept_because_not_provably_archived": [
                {"path": str(f), "reason": w} for f, w in kept],
            "deleted_count": len(deleted),
            "truncated_count": len(truncated),
            "kept_count": len(kept),
        }, indent=2))
    except Exception:
        pass


def save_checkpoint(profile, verbose: bool = True, *,
                    delete_originals: bool = True) -> str | None:
    """
    Compress all data for `profile` into the next numbered checkpoint.
    Returns the checkpoint path on success, None if nothing to archive.

    With `delete_originals` (the default) the runtime files that are PROVEN to
    be in the finished archive are then removed. Pass False for any automatic,
    unprompted caller: closing a window is not a request to destroy data, and
    the on-close path has no confirmation dialog in front of it.
    """
    profile_name = (getattr(profile, "name", "") or "unknown").strip()
    safe_name = "".join(c if c.isalnum() or c in "-_." else "_" for c in profile_name)

    runtime_files, config_files = _collect_files(profile)
    source_files = _collect_source_files(profile)

    all_files = runtime_files + config_files + source_files
    if not all_files:
        if verbose:
            print(f"[checkpoint] {profile_name}: nothing to archive — skipping")
        return None

    ckpt_folder = CHECKPOINTS_ROOT / safe_name
    ckpt_folder.mkdir(parents=True, exist_ok=True)

    # Belt as well as braces: pick a free number, then let the FILESYSTEM be the
    # one that guarantees it is free. `tarfile.open(..., "x:gz")` fails if the
    # path exists rather than truncating it, so no numbering mistake — and no
    # second process writing the same folder at the same moment — can destroy an
    # archive that is already there.
    num = _next_checkpoint_number(ckpt_folder)
    ckpt_path = ckpt_folder / f"checkpoint_{num:05d}.tar.gz"
    for _bump in range(64):
        if not ckpt_path.exists():
            break
        num += 1
        ckpt_path = ckpt_folder / f"checkpoint_{num:05d}.tar.gz"
    else:
        if verbose:
            print(f"[checkpoint] ERROR: could not find a free checkpoint number "
                  f"in {ckpt_folder} — nothing archived, nothing deleted")
        return None

    root = (getattr(profile, "project_root", "") or "").strip()
    root_path = Path(root).resolve() if root else None

    manifest = {
        "profile": profile_name,
        "checkpoint": num,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "runtime_files": [str(f) for f in runtime_files],
        "config_files": [str(f) for f in config_files],
        "source_files": [str(f) for f in source_files],
        "runtime_count": len(runtime_files),
        "config_count": len(config_files),
        "source_count": len(source_files),
        "total_count": len(all_files),
    }

    try:
        # `added` records only the files tar.add ACCEPTED. A file whose add
        # raised never enters it, so it can never reach the delete stage.
        added: list[tuple[Path, str]] = []
        add_failures: list[tuple[Path, str]] = []

        with tarfile.open(ckpt_path, "x:gz", compresslevel=9) as tar:
            manifest_bytes = json.dumps(manifest, indent=2).encode()
            info = tarfile.TarInfo(name="manifest.json")
            info.size = len(manifest_bytes)
            tar.addfile(info, io.BytesIO(manifest_bytes))

            # Runtime data (screenshots, logs, sidecars, stats, crashes)
            for f in runtime_files:
                arc = _arcname(f, profile_name, root_path)
                try:
                    tar.add(f, arcname=arc)
                    added.append((f, arc))
                except Exception as e:
                    add_failures.append((f, str(e)))
                    if verbose:
                        print(f"[checkpoint] warning: NOT archived, so NOT "
                              f"deleted — runtime {f.name}: {e}")

            # Config data (.agentvision/, config/) — archived but not deleted
            for f in config_files:
                try:
                    tar.add(f, arcname=_arcname(f, profile_name, root_path))
                except Exception as e:
                    if verbose:
                        print(f"[checkpoint] warning: skipping config {f.name}: {e}")

            # Source code — stored under source/ prefix, never deleted
            for f in source_files:
                try:
                    if root_path:
                        try:
                            rel = f.resolve().relative_to(root_path)
                            arcname = f"source/{rel}"
                        except ValueError:
                            arcname = f"source/{f.name}"
                    else:
                        arcname = f"source/{f.name}"
                    tar.add(f, arcname=arcname)
                except Exception as e:
                    if verbose:
                        print(f"[checkpoint] warning: skipping source {f.name}: {e}")

        if verbose:
            size_kb = ckpt_path.stat().st_size // 1024
            print(f"[checkpoint] {profile_name}: saved checkpoint_{num:05d}.tar.gz "
                  f"({len(runtime_files)} runtime + {len(config_files)} config + "
                  f"{len(source_files)} source, {size_kb} KB compressed)")

        # ── VERIFY, then delete ───────────────────────────────────────────
        if not delete_originals:
            _write_removal_manifest(ckpt_path, profile_name, num,
                                    deleted=[], truncated=[],
                                    kept=[(f, "archive-only run: nothing deleted")
                                          for f, _a in added])
            if verbose:
                print(f"[checkpoint] {profile_name}: archive-only — "
                      f"{len(added)} runtime file(s) copied, none removed")
            return str(ckpt_path)

        verified, unverified = _verify_archived(ckpt_path, added)

        deleted, truncated, kept = _remove_archived(verified, verbose=verbose)

        for f, why in unverified:
            if verbose:
                print(f"[checkpoint] KEPT (not provably in the checkpoint) "
                      f"{f}: {why}")

        _write_removal_manifest(ckpt_path, profile_name, num,
                                deleted=deleted, truncated=truncated,
                                kept=[(f, w) for f, w in unverified]
                                     + [(f, w) for f, w in kept]
                                     + [(f, f"tar.add failed: {w}")
                                        for f, w in add_failures])

        if verbose:
            print(f"[checkpoint] {profile_name}: removed {len(deleted)} + "
                  f"truncated {len(truncated)} runtime file(s); kept "
                  f"{len(unverified) + len(kept) + len(add_failures)} that the "
                  f"checkpoint does not provably contain "
                  f"(config + source preserved)")

        return str(ckpt_path)

    except FileExistsError as e:
        # Another writer got this number between our check and our create. That
        # file is THEIRS — removing our "partial" would remove their finished
        # archive. Abort and delete nothing, in either folder.
        if verbose:
            print(f"[checkpoint] {profile_name}: {ckpt_path.name} appeared while "
                  f"writing it — aborting without touching it or any original "
                  f"({e})")
        return None
    except Exception as e:
        if verbose:
            print(f"[checkpoint] ERROR saving checkpoint for {profile_name}: {e}")
        try:
            ckpt_path.unlink(missing_ok=True)
        except Exception:
            pass
        return None


def save_all_checkpoints(profiles: dict, verbose: bool = True, *,
                         delete_originals: bool = False) -> list[str]:
    """Save checkpoints for all profiles. Returns list of created checkpoint paths.

    Defaults to ARCHIVE-ONLY. The only caller is the GUI's window-close handler,
    which runs for every profile with no prompt and no undo; a bulk automatic
    sweep is the last place that should be allowed to delete a user's logs.
    """
    results = []
    for name, profile in profiles.items():
        path = save_checkpoint(profile, verbose=verbose,
                               delete_originals=delete_originals)
        if path:
            results.append(path)
    return results
