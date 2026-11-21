# SPDX-FileCopyrightText: 2026 RyanLin-InfEvo
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Columnar quantization, deterministic gzip JSON I/O, and snapshot replay helpers (§3.3)."""

from __future__ import annotations

import gzip
import json
import math
import os
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from hyperpod.models.snapshot import Snapshot, TrackedInterval
from hyperpod.scoring.trades import quantize_size


def quantize_sig_figs(val: float, sig_figs: int) -> float:
    """Round a finite float to ``sig_figs`` significant digits, normalizing ``-0.0``."""
    if sig_figs < 1:
        raise ValueError(f"sig_figs must be >= 1, got {sig_figs}")
    if not math.isfinite(val):
        raise ValueError(f"Value must be finite, got {val!r}")
    if val == 0.0:
        return 0.0
    rounded = float(f"{val:.{sig_figs}g}")
    return 0.0 if rounded == 0.0 else rounded


def quantize_av(av: float | None) -> float | None:
    """Quantize account value to 4 significant figures (§3.3)."""
    if av is None:
        return None
    return quantize_sig_figs(av, 4)


def quantize_price(px: float | None) -> float | None:
    """Quantize strictly positive price to 6 significant figures (§3.3)."""
    if px is None:
        return None
    if not math.isfinite(px) or px <= 0.0:
        raise ValueError(f"Price must be finite and > 0, got {px!r}")
    return quantize_sig_figs(px, 6)


def quantize_notional(notional: float) -> float:
    """Quantize non-negative USD notional / volume to 6 significant figures."""
    if not math.isfinite(notional) or notional < 0.0:
        raise ValueError(f"Notional must be finite and >= 0, got {notional!r}")
    return quantize_sig_figs(notional, 6)


def quantize_szi(szi: float, sz_decimals: int = 5) -> float:
    """Quantize signed position size to ``sz_decimals`` with dust-zeroing (§3.3, §5.3)."""
    return quantize_size(szi, sz_decimals)


def atomic_write_bytes(path: Path, payload: bytes) -> None:
    """Atomically write raw bytes via same-directory temp file + ``os.replace``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def read_json_file(path: Path) -> Any:
    """Read a plain ``.json`` or gzip-compressed ``.json.gz`` payload."""
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return json.load(fh)
    return json.loads(path.read_text(encoding="utf-8"))


def write_json_file_atomic(path: Path, payload: Any, *, compress: bool | None = None) -> None:
    """Atomically write compact JSON or deterministic ``mtime=0`` gzipped JSON."""
    use_gzip = path.suffix == ".gz" if compress is None else compress
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    data = gzip.compress(raw, compresslevel=9, mtime=0) if use_gzip else raw + b"\n"
    atomic_write_bytes(path, data)


def is_addr_tracked_at(tracked: Sequence[TrackedInterval], addr_id: int, t_ms: int) -> bool:
    """Return True iff ``addr_id`` has an active ``tracked`` interval covering ``t_ms``."""
    return any(
        aid == addr_id and from_ts <= t_ms and (to_ts is None or t_ms <= to_ts)
        for aid, from_ts, to_ts in tracked
    )


def replay_snapshot_positions_at(
    snapshot: Snapshot,
    t_ms: int,
) -> dict[tuple[int, str], tuple[float, float | None]]:
    """Replay active tracked positions at ``t_ms`` from ``keyframe`` + ``posChanges``."""
    if t_ms < snapshot.keyframe.t:
        return {}
    pos_by_idx: dict[tuple[int, int], tuple[float, float | None]] = {
        (aid, cid): (float(szi), float(epx) if epx is not None else None)
        for aid, cid, szi, epx in snapshot.keyframe.pos
    }
    relevant_changes = sorted(
        (chg for chg in snapshot.posChanges if chg[0] <= t_ms),
        key=lambda chg: chg[0],
    )
    for _, aid, cid, szi, epx in relevant_changes:
        if szi == 0.0:
            pos_by_idx.pop((aid, cid), None)
        else:
            pos_by_idx[(aid, cid)] = (float(szi), float(epx) if epx is not None else None)

    out: dict[tuple[int, str], tuple[float, float | None]] = {}
    for (aid, cid), (szi, epx) in pos_by_idx.items():
        if szi == 0.0 or not is_addr_tracked_at(snapshot.tracked, aid, t_ms):
            continue
        out[(aid, snapshot.coins[cid])] = (szi, epx)
    return out
