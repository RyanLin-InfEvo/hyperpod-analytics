# SPDX-FileCopyrightText: 2026 RyanLin-InfEvo
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Single-commit orphan branch publishing with path isolation & lease retries (§6.2)."""

from __future__ import annotations

import gzip
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from hyperpod.config import PipelineConfig, load_config
from hyperpod.models.fills import FillsFile
from hyperpod.models.manifest import Manifest
from hyperpod.models.snapshot import Snapshot
from hyperpod.storage.formats import read_json_file
from hyperpod.storage.site_writer import (
    _merge_fill_file,
    rebuild_manifest,
    validate_site_data_directory,
)
from hyperpod.util.time import ms_to_utc_date

BranchName = Literal["state", "gh-pages"]
PublishOwner = Literal["fast", "daily", "frontend", "bootstrap"]

_SECRET_PATTERN = re.compile(
    rb"(github_pat_[A-Za-z0-9_]{12,}|ghp_[A-Za-z0-9_]{12,}|gho_[A-Za-z0-9_]{12,}"
    rb"|-----BEGIN [A-Z ]*PRIVATE KEY-----|Authorization:\s*Bearer\s+\S+)"
)


class GitPublishError(RuntimeError):
    """Raised when secret scanning, validation, or git push fails."""


@dataclass(frozen=True)
class GitPublishResult:
    """Metadata describing a completed single-commit orphan branch push."""

    branch: BranchName
    owner: PublishOwner
    commit_sha: str
    attempts: int
    had_remote_branch: bool


def _run_git(
    args: list[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
    )
    if check and proc.returncode != 0:
        raise GitPublishError(
            f"git {' '.join(args)} failed (rc={proc.returncode}): "
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )
    return proc


def scan_directory_for_secrets(root: Path) -> None:
    """Fail loudly if any non-.git file under ``root`` contains secret/credential markers."""
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if ".git" in path.relative_to(root).parts:
            continue
        if path.name == ".env" or path.name.startswith(".env."):
            raise GitPublishError(f"Secret file forbidden in publish tree: {path.name}")
        data = path.read_bytes()
        match = _SECRET_PATTERN.search(data)
        if not match and path.suffix == ".gz":
            try:
                decompressed = gzip.decompress(data)
            except (OSError, EOFError):
                decompressed = b""
            match = _SECRET_PATTERN.search(decompressed)
        if match:
            raise GitPublishError(
                f"Detected forbidden credential pattern in publish file {path.relative_to(root)}"
            )


def _copy_dir_contents(src: Path, dst: Path) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        target = dst / item.name
        if item.is_dir():
            shutil.copytree(item, target, dirs_exist_ok=True)
        elif item.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)


def _compacted_snapshot_dates(data_dir: Path) -> set[str]:
    dates: set[str] = set()
    months_dir = data_dir / "snapshots" / "months"
    if not months_dir.is_dir():
        return dates
    for m_path in sorted(months_dir.glob("*.json.gz")):
        snap = Snapshot.model_validate(read_json_file(m_path))
        for t in snap.series.t:
            dates.add(ms_to_utc_date(t))
    return dates


def _compacted_fill_dates(data_dir: Path, coin: str) -> set[str]:
    dates: set[str] = set()
    months_dir = data_dir / "fills" / coin / "months"
    if not months_dir.is_dir():
        return dates
    for m_path in sorted(months_dir.glob("*.json.gz")):
        fills_file = FillsFile.model_validate(read_json_file(m_path))
        for row in fills_file.fills:
            dates.add(ms_to_utc_date(row[0]))
    return dates


def apply_scoped_overlay(
    source_dir: Path,
    target_worktree: Path,
    *,
    branch: BranchName,
    owner: PublishOwner,
) -> None:
    """Apply ``source_dir`` onto ``target_worktree`` strictly scoped to ``(branch, owner)``."""
    if branch == "state":
        if owner == "frontend":
            raise ValueError("owner='frontend' cannot write to 'state' branch")
        partitions: tuple[str, ...]
        if owner == "fast":
            partitions = ("fast",)
        elif owner == "daily":
            partitions = ("daily",)
        else:
            partitions = ("fast", "daily", "bootstrap")

        for part in partitions:
            dst_part = target_worktree / part
            if dst_part.exists():
                shutil.rmtree(dst_part)
            src_part = source_dir / part
            if src_part.is_dir():
                shutil.copytree(src_part, dst_part)
        return

    # branch == "gh-pages"
    (target_worktree / ".nojekyll").write_bytes(b"")
    dst_data = target_worktree / "data"

    if owner == "frontend":
        for item in list(target_worktree.iterdir()):
            if item.name in {".git", ".nojekyll", "data"}:
                continue
            if item.is_dir():
                shutil.rmtree(item)
            else:
                item.unlink()
        for item in source_dir.iterdir():
            if item.name in {".git", ".nojekyll", "data"}:
                continue
            target = target_worktree / item.name
            if item.is_dir():
                shutil.copytree(item, target, dirs_exist_ok=True)
            elif item.is_file():
                shutil.copy2(item, target)
        return

    if owner == "fast":
        dst_data.mkdir(parents=True, exist_ok=True)

        src_latest = source_dir / "snapshots" / "latest.json"
        if src_latest.is_file():
            dst_latest = dst_data / "snapshots" / "latest.json"
            dst_latest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_latest, dst_latest)

        compacted_snap_dates = _compacted_snapshot_dates(dst_data)
        src_snap_days = source_dir / "snapshots" / "days"
        if src_snap_days.is_dir():
            dst_snap_days = dst_data / "snapshots" / "days"
            dst_snap_days.mkdir(parents=True, exist_ok=True)
            for day_path in sorted(src_snap_days.glob("*.json")):
                if day_path.stem not in compacted_snap_dates:
                    shutil.copy2(day_path, dst_snap_days / day_path.name)

        src_fills_root = source_dir / "fills"
        if src_fills_root.is_dir():
            for coin_dir in sorted(p for p in src_fills_root.iterdir() if p.is_dir()):
                coin = coin_dir.name
                compacted_dates = _compacted_fill_dates(dst_data, coin)
                src_coin_days = coin_dir / "days"
                if src_coin_days.is_dir():
                    dst_coin_days = dst_data / "fills" / coin / "days"
                    dst_coin_days.mkdir(parents=True, exist_ok=True)
                    for day_path in sorted(src_coin_days.glob("*.json")):
                        if day_path.stem not in compacted_dates:
                            _merge_fill_file(day_path, dst_coin_days / day_path.name)

                src_coin_months = coin_dir / "months"
                if src_coin_months.is_dir():
                    dst_coin_months = dst_data / "fills" / coin / "months"
                    dst_coin_months.mkdir(parents=True, exist_ok=True)
                    for m_path in sorted(src_coin_months.glob("*.json.gz")):
                        _merge_fill_file(m_path, dst_coin_months / m_path.name)

        for filename in ("whitelist.json", "criteria.json", "candidates.json"):
            src_meta = source_dir / filename
            dst_meta = dst_data / filename
            if src_meta.is_file() and not dst_meta.is_file():
                shutil.copy2(src_meta, dst_meta)

        src_manifest = source_dir / "manifest.json"
        dst_manifest = dst_data / "manifest.json"
        if src_manifest.is_file() and not dst_manifest.is_file():
            shutil.copy2(src_manifest, dst_manifest)
        return

    if owner == "daily":
        dst_data.mkdir(parents=True, exist_ok=True)
        for filename in ("whitelist.json", "criteria.json", "candidates.json", "manifest.json"):
            src_meta = source_dir / filename
            if src_meta.is_file():
                shutil.copy2(src_meta, dst_data / filename)

        src_candles = source_dir / "candles"
        if src_candles.is_dir():
            dst_candles = dst_data / "candles"
            if dst_candles.exists():
                shutil.rmtree(dst_candles)
            shutil.copytree(src_candles, dst_candles)

        src_snap_months = source_dir / "snapshots" / "months"
        dst_snap_months = dst_data / "snapshots" / "months"
        if src_snap_months.is_dir():
            if dst_snap_months.exists():
                shutil.rmtree(dst_snap_months)
            shutil.copytree(src_snap_months, dst_snap_months)

        compacted_snap_dates = _compacted_snapshot_dates(dst_data)
        dst_snap_days = dst_data / "snapshots" / "days"
        if dst_snap_days.is_dir():
            for existing_day in sorted(dst_snap_days.glob("*.json")):
                if existing_day.stem in compacted_snap_dates:
                    existing_day.unlink()

        src_snap_days = source_dir / "snapshots" / "days"
        if src_snap_days.is_dir():
            dst_snap_days.mkdir(parents=True, exist_ok=True)
            for day_path in sorted(src_snap_days.glob("*.json")):
                if day_path.stem in compacted_snap_dates:
                    continue
                dst_day = dst_snap_days / day_path.name
                if not dst_day.is_file():
                    shutil.copy2(day_path, dst_day)
                else:
                    src_snap = Snapshot.model_validate(read_json_file(day_path))
                    dst_snap = Snapshot.model_validate(read_json_file(dst_day))
                    src_last = src_snap.series.t[-1] if src_snap.series.t else src_snap.keyframe.t
                    dst_last = dst_snap.series.t[-1] if dst_snap.series.t else dst_snap.keyframe.t
                    if src_last >= dst_last:
                        shutil.copy2(day_path, dst_day)

        src_latest = source_dir / "snapshots" / "latest.json"
        if src_latest.is_file():
            dst_latest = dst_data / "snapshots" / "latest.json"
            dst_latest.parent.mkdir(parents=True, exist_ok=True)
            if not dst_latest.is_file():
                shutil.copy2(src_latest, dst_latest)
            else:
                src_l = Snapshot.model_validate(read_json_file(src_latest))
                dst_l = Snapshot.model_validate(read_json_file(dst_latest))
                if src_l.keyframe.t >= dst_l.keyframe.t:
                    shutil.copy2(src_latest, dst_latest)

        src_fills_root = source_dir / "fills"
        if src_fills_root.is_dir():
            for coin_dir in sorted(p for p in src_fills_root.iterdir() if p.is_dir()):
                coin = coin_dir.name
                src_coin_months = coin_dir / "months"
                if src_coin_months.is_dir():
                    dst_coin_months = dst_data / "fills" / coin / "months"
                    dst_coin_months.mkdir(parents=True, exist_ok=True)
                    for m_path in sorted(src_coin_months.glob("*.json.gz")):
                        _merge_fill_file(m_path, dst_coin_months / m_path.name)

                compacted_fill_dates = _compacted_fill_dates(dst_data, coin)
                dst_coin_days = dst_data / "fills" / coin / "days"
                if dst_coin_days.is_dir():
                    for existing_fill_day in sorted(dst_coin_days.glob("*.json")):
                        if existing_fill_day.stem in compacted_fill_dates:
                            existing_fill_day.unlink()

                src_coin_days = coin_dir / "days"
                if src_coin_days.is_dir():
                    dst_coin_days.mkdir(parents=True, exist_ok=True)
                    for day_path in sorted(src_coin_days.glob("*.json")):
                        if day_path.stem not in compacted_fill_dates:
                            _merge_fill_file(day_path, dst_coin_days / day_path.name)
        return

    # owner == "bootstrap"
    if dst_data.exists():
        shutil.rmtree(dst_data)
    _copy_dir_contents(source_dir, dst_data)


def publish_orphan_branch(
    source_dir: Path,
    *,
    remote_url: str,
    branch: BranchName,
    owner: PublishOwner,
    commit_message: str,
    max_retries: int = 3,
    cfg: PipelineConfig | None = None,
    schemas_dir: Path | None = None,
) -> GitPublishResult:
    """Publish scoped changes to an orphan branch as a single amended commit (§6.2)."""
    if max_retries < 1:
        raise ValueError("max_retries must be >= 1")
    if branch == "state" and owner == "frontend":
        raise ValueError("owner='frontend' cannot publish to 'state' branch")

    effective_cfg = cfg or load_config()
    src_manifest: Manifest | None = None
    if (
        branch == "gh-pages"
        and owner in {"fast", "daily"}
        and (source_dir / "manifest.json").is_file()
    ):
        src_manifest = Manifest.model_validate(read_json_file(source_dir / "manifest.json"))

    last_error = ""
    for attempt in range(1, max_retries + 1):
        ls_proc = _run_git(["ls-remote", "--heads", remote_url, f"refs/heads/{branch}"])
        remote_line = ls_proc.stdout.strip().splitlines()
        remote_sha: str | None = remote_line[0].split()[0] if remote_line else None

        with tempfile.TemporaryDirectory(prefix=f"hyperpod-{branch}-") as tmpdir:
            wt = Path(tmpdir) / "repo"
            if remote_sha is not None:
                _run_git(
                    [
                        "clone",
                        "--depth=1",
                        "--single-branch",
                        "--branch",
                        branch,
                        remote_url,
                        str(wt),
                    ]
                )
            else:
                wt.mkdir(parents=True, exist_ok=True)
                _run_git(["init"], cwd=wt)
                _run_git(["checkout", "--orphan", branch], cwd=wt)
                _run_git(["remote", "add", "origin", remote_url], cwd=wt)

            dst_prior_manifest: Manifest | None = None
            prior_manifest_path = wt / "data" / "manifest.json"
            if branch == "gh-pages" and prior_manifest_path.is_file():
                dst_prior_manifest = Manifest.model_validate(read_json_file(prior_manifest_path))

            apply_scoped_overlay(source_dir, wt, branch=branch, owner=owner)

            data_dir = wt / "data"
            if (
                branch == "gh-pages"
                and data_dir.is_dir()
                and (data_dir / "manifest.json").is_file()
            ):
                if owner in {"fast", "daily"} and src_manifest is not None:
                    now_val = max(
                        src_manifest.generatedAt,
                        dst_prior_manifest.generatedAt if dst_prior_manifest else 0,
                    )
                    fast_candidates = [
                        x
                        for x in (
                            src_manifest.pipeline.fastLastOk,
                            dst_prior_manifest.pipeline.fastLastOk if dst_prior_manifest else None,
                        )
                        if x is not None
                    ]
                    daily_candidates = [
                        x
                        for x in (
                            src_manifest.pipeline.dailyLastOk,
                            dst_prior_manifest.pipeline.dailyLastOk if dst_prior_manifest else None,
                        )
                        if x is not None
                    ]
                    rebuild_manifest(
                        data_dir,
                        effective_cfg,
                        now_ms=now_val,
                        fast_last_ok=max(fast_candidates) if fast_candidates else None,
                        daily_last_ok=max(daily_candidates) if daily_candidates else None,
                        extra_warnings=src_manifest.pipeline.warnings,
                        schemas_dir=schemas_dir,
                    )
                validate_site_data_directory(data_dir, schemas_dir=schemas_dir)

            scan_directory_for_secrets(wt)

            _run_git(["config", "user.name", "hyperpod-bot"], cwd=wt)
            _run_git(
                ["config", "user.email", "hyperpod-bot@users.noreply.github.com"],
                cwd=wt,
            )
            _run_git(["add", "-A"], cwd=wt)

            if remote_sha is not None:
                cnt_proc = _run_git(["rev-list", "--count", "HEAD"], cwd=wt)
                if cnt_proc.stdout.strip() == "1":
                    _run_git(["commit", "--amend", "--allow-empty", "-m", commit_message], cwd=wt)
                else:
                    _run_git(["checkout", "--orphan", "_hyperpod_single"], cwd=wt)
                    _run_git(["commit", "--allow-empty", "-m", commit_message], cwd=wt)
                    _run_git(["branch", "-M", "_hyperpod_single", branch], cwd=wt)
            else:
                _run_git(["commit", "--allow-empty", "-m", commit_message], cwd=wt)

            cnt_check = _run_git(["rev-list", "--count", "HEAD"], cwd=wt).stdout.strip()
            if cnt_check != "1":
                raise GitPublishError(f"Expected single commit on {branch}, got count={cnt_check}")
            commit_sha = _run_git(["rev-parse", "HEAD"], cwd=wt).stdout.strip()

            push_args = (
                [
                    "push",
                    f"--force-with-lease=refs/heads/{branch}:{remote_sha}",
                    "origin",
                    branch,
                ]
                if remote_sha is not None
                else ["push", "-u", "origin", branch]
            )
            push_proc = _run_git(push_args, cwd=wt, check=False)
            if push_proc.returncode == 0:
                return GitPublishResult(
                    branch=branch,
                    owner=owner,
                    commit_sha=commit_sha,
                    attempts=attempt,
                    had_remote_branch=remote_sha is not None,
                )

            last_error = push_proc.stderr.strip() or push_proc.stdout.strip()

    raise GitPublishError(
        f"Failed to push branch {branch!r} after {max_retries} attempts: {last_error}"
    )
