"""Deterministic synthetic Chromaprint fixtures for the segment-detection eval harness.

Real media is never needed: each episode's ``head``/``tail`` is random uint32 noise with a
shared, identical "signature" run spliced in at the labelled offset (the same signature
across every episode of a case), exactly the repeated intro/credits a real season exhibits.
The detector should recover the planted segments; the eval harness measures precision/recall
and boundary error against the labels.

Fixtures are stored compactly as parameters (seed + offsets + durations) and expanded into
full fingerprints here, so the JSON stays small and the data is reproducible.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

import workers.fingerprint_worker as fw


def _random_points(n: int, rng: np.random.Generator) -> list[int]:
    if n <= 0:
        return []
    return [int(x) for x in rng.integers(0, 2**32, size=n, dtype=np.uint64)]


def _signature(n: int, seed: int) -> list[int]:
    """A fixed pseudo-random run shared across every episode of a case."""
    return _random_points(n, np.random.default_rng(seed))


def _corrupt(points: list[int], n_bits: int, rng: np.random.Generator) -> list[int]:
    """Flip ``n_bits`` distinct random bits in every point.

    Models a *partially* shared region: the same underlying audio (a shared outro music
    bed) with unique per-episode content (dialogue) layered over it. The bits are
    perturbed without destroying the match, which is exactly the condition a loose
    Hamming tolerance mistakes for genuinely-identical audio.
    """
    if n_bits <= 0:
        return list(points)
    out = []
    for value in points:
        mask = 0
        for pos in rng.choice(32, size=n_bits, replace=False):
            mask |= 1 << int(pos)
        out.append(value ^ mask)
    return out


def _build_window(
    window_sec: float,
    signature: list[int],
    sig_start_sec: float,
    noise_seed: int,
    bit_flips: int = 0,
    extra_splices: list[tuple[list[int], float]] | None = None,
) -> list[int]:
    """Random-noise window with ``signature`` spliced in starting at ``sig_start_sec``.

    ``bit_flips`` optionally flips a few low bits in a fraction of signature points to model
    real episode-to-episode variation (kept well under the matcher's Hamming threshold).
    ``extra_splices`` writes additional ``(points, start_sec)`` runs first, so the main
    signature always wins on overlap.
    """
    rng = np.random.default_rng(noise_seed)
    total = fw.seconds_to_point(window_sec)
    points = _random_points(total, rng)
    for extra_points, extra_start_sec in extra_splices or []:
        extra_idx = fw.seconds_to_point(extra_start_sec)
        for offset, value in enumerate(extra_points):
            idx = extra_idx + offset
            if 0 <= idx < total:
                points[idx] = value
    start_idx = fw.seconds_to_point(sig_start_sec)
    sig = list(signature)
    if bit_flips:
        for i in range(0, len(sig), max(1, len(sig) // max(1, bit_flips))):
            sig[i] = sig[i] ^ 0x1  # flip the lowest bit only (Hamming 1)
    for offset, value in enumerate(sig):
        idx = start_idx + offset
        if 0 <= idx < total:
            points[idx] = value
    return points


def expand_case(case: dict) -> tuple[list[dict], dict]:
    """Expand a compact fixture into ``EpisodeFingerprint`` row data + per-episode labels.

    Returns ``(rows, labels)`` where each row is a dict with ``episode``/``duration_sec``/
    ``fingerprint`` and ``labels`` maps ``str(episode) -> {"intro": [s, e], "credits": [s, e]}``.
    """
    head_dur = float(case["head_duration_sec"])
    tail_dur = float(case["tail_duration_sec"])
    ep_duration = float(case["episode_duration_sec"])
    tail_base = max(0.0, ep_duration - tail_dur)
    seed = int(case.get("seed", 0))

    intro = case.get("intro")
    credits = case.get("credits")
    intro_sig = (
        _signature(fw.seconds_to_point(intro["duration"]), seed + 1) if intro else []
    )
    credits_sig = (
        _signature(fw.seconds_to_point(credits["duration"]), seed + 2)
        if credits
        else []
    )
    # Optional shared music bed immediately preceding the credits: the same audio in every
    # episode, but with unique dialogue over it (per-episode bit corruption). The label
    # stays at the true credits start, so a detector that latches onto the bed scores a
    # boundary error and an IoU miss.
    bleed = (credits or {}).get("bleed") if credits else None
    bleed_sig = (
        _signature(fw.seconds_to_point(bleed["duration"]), seed + 3) if bleed else []
    )

    rows: list[dict] = []
    labels: dict[str, dict] = {}
    for ep in case["episodes"]:
        ep_num = int(ep) if not isinstance(ep, dict) else int(ep["episode"])
        intro_start = float(intro["start"]) if intro else None
        credits_abs_start = float(credits["abs_start"]) if credits else None
        # Optional per-episode jitter to exercise non-zero alignment shifts, plus
        # ``no_intro``/``no_credits`` flags that make an episode an "anomalous neighbour"
        # (pure noise in that window, so every pair containing it misses).
        ep_has_intro = intro is not None
        ep_has_credits = credits is not None
        if isinstance(ep, dict):
            intro_start = float(ep.get("intro_start", intro_start)) if intro else None
            credits_abs_start = (
                float(ep.get("credits_abs_start", credits_abs_start))
                if credits
                else None
            )
            ep_has_intro = ep_has_intro and not ep.get("no_intro", False)
            ep_has_credits = ep_has_credits and not ep.get("no_credits", False)

        head = (
            _build_window(head_dur, intro_sig, intro_start, seed * 100 + ep_num)
            if ep_has_intro
            else _random_points(
                fw.seconds_to_point(head_dur),
                np.random.default_rng(seed * 100 + ep_num),
            )
        )
        credits_local = (credits_abs_start - tail_base) if ep_has_credits else None
        credits_splices = []
        if ep_has_credits and bleed:
            credits_splices.append(
                (
                    _corrupt(
                        bleed_sig,
                        int(bleed.get("corruption_bits", 8)),
                        np.random.default_rng(seed * 300 + ep_num),
                    ),
                    credits_local - float(bleed["duration"]),
                )
            )
        tail = (
            _build_window(
                tail_dur,
                credits_sig,
                credits_local,
                seed * 200 + ep_num,
                extra_splices=credits_splices,
            )
            if ep_has_credits
            else _random_points(
                fw.seconds_to_point(tail_dur),
                np.random.default_rng(seed * 200 + ep_num),
            )
        )
        rows.append(
            {
                "episode": ep_num,
                "duration_sec": ep_duration,
                "fingerprint": {
                    "head": head,
                    "tail": tail,
                    "head_duration_sec": head_dur,
                    "tail_duration_sec": tail_dur,
                    "head_point_count": len(head),
                    "tail_point_count": len(tail),
                    "chromaprint_sample_duration": fw.CHROMAPRINT_SAMPLE_DURATION,
                    "fingerprint_version": fw.FINGERPRINT_VERSION,
                    "detector_version": fw.DETECTOR_VERSION,
                    "tail_visual_version": fw.VISUAL_FEATURE_VERSION,
                    "tail_visual_sample_fps": fw.CREDITS_VISUAL_SAMPLE_FPS,
                    "tail_visual_runs": (
                        [
                            {
                                "start_sec": credits_local,
                                "end_sec": credits_local
                                + min(float(credits["duration"]), 20.0),
                                "score": 0.95,
                                "qualifying_ratio": 0.95,
                            }
                        ]
                        if ep_has_credits
                        else []
                    ),
                    # Synthetic media cannot exercise FFmpeg's low-resolution
                    # visual extractor.  Seed the already-confirmed v2 visual
                    # result so the season learner/evaluator exercises the same
                    # serving path as production without falling back to the
                    # retired tail-audio matcher.
                    "credits_v2": (
                        {
                            "version": fw.VISUAL_FEATURE_VERSION,
                            "candidate": {
                                "start_sec": credits_abs_start,
                                "end_sec": credits_abs_start
                                + float(credits["duration"]),
                                "duration_sec": float(credits["duration"]),
                                "confidence": 0.95,
                                "source": "visual-v3+boundary",
                                "visual_evidence": {
                                    "version": fw.VISUAL_FEATURE_VERSION,
                                    "score": 0.95,
                                    "qualifying_ratio": 0.95,
                                    "onset_sec": credits_abs_start,
                                    "terminal_score": 0.95,
                                    "boundary_confirmed": True,
                                },
                            },
                            "reason": None,
                        }
                        if ep_has_credits
                        else {
                            "version": fw.VISUAL_FEATURE_VERSION,
                            "candidate": None,
                            "reason": "no_terminal_visual_credits_run",
                        }
                    ),
                    "extraction_mode": "ffmpeg_chromaprint_raw",
                },
            }
        )
        label: dict = {}
        if ep_has_intro:
            label["intro"] = [intro_start, intro_start + float(intro["duration"])]
        if ep_has_credits:
            label["credits"] = [
                credits_abs_start,
                credits_abs_start + float(credits["duration"]),
            ]
        labels[str(ep_num)] = label
    return rows, labels


def load_cases() -> list[dict]:
    here = Path(__file__).parent
    cases = []
    for path in sorted(here.glob("*.json")):
        with open(path) as fh:
            case = json.load(fh)
            case["_name"] = path.stem
            cases.append(case)
    return cases
