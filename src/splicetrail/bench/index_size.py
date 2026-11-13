"""Exact (flat) vs compressed (IVF-PQ) descriptor index: size on disk and search time.

Descriptors are real 5x5 luminance-pattern descriptors from the scikit-image
benchmark photos (needs the ``bench`` extra). One photo is held out and its
descriptors are the queries, so no query is in the index.

Usage:
    python -m splicetrail.bench.index_size                 # 200,000 descriptors
    python -m splicetrail.bench.index_size --n 50000 --queries 5000
"""

from __future__ import annotations

import argparse
import os
import tempfile
import time
from typing import Dict, List, Tuple

import numpy as np

from splicetrail.bench.datasets import load_photos
from splicetrail.descriptors.core import LuminanceDescriptor
from splicetrail.search.index import ANNIndex


def photo_descriptors(patch_size: int = 5) -> List[Tuple[str, np.ndarray]]:
    """Descriptors of every textured patch of each benchmark photo, per photo."""
    describe = LuminanceDescriptor(patch_size)
    out = []
    for name, image in load_photos().items():
        pixels = np.asarray(image, dtype=np.float32) / 255.0
        windows = np.lib.stride_tricks.sliding_window_view(
            pixels, (patch_size, patch_size), axis=(0, 1)
        )  # (H', W', 3, s, s)
        patches = windows.reshape(-1, 3, patch_size, patch_size).transpose(0, 2, 3, 1)
        desc, textured = describe.compute_batch(patches)
        out.append((name, desc[textured]))
    return out


def measure(
    n: int = 200_000,
    queries: int = 20_000,
    k: int = 8,
    nprobe: int = 16,
    seed: int = 0,
) -> Dict[str, Dict[str, float]]:
    """Build both index kinds over the same ``n`` descriptors and time ``queries`` searches."""
    rng = np.random.RandomState(seed)
    per_photo = photo_descriptors()
    _, held_out = per_photo[-1]
    pool = np.concatenate([d for _, d in per_photo[:-1]])
    if len(pool) < n:
        raise SystemExit(f"only {len(pool)} descriptors available, asked for {n}")
    data = pool[rng.choice(len(pool), n, replace=False)]
    query = held_out[rng.choice(len(held_out), min(queries, len(held_out)), False)]

    results = {}
    with tempfile.TemporaryDirectory() as tmp:
        for kind in ("flat", "ivfpq"):
            # train_size=n: the ivfpq index trains on the full set as it is added.
            index = ANNIndex(data.shape[1], kind=kind, train_size=n, nprobe=nprobe)
            t0 = time.perf_counter()
            index.add("photos", data)
            build = time.perf_counter() - t0
            index.search(query[:100], k)  # warm-up
            t0 = time.perf_counter()
            index.search(query, k)
            search = time.perf_counter() - t0
            prefix = os.path.join(tmp, kind)
            index.save(prefix)
            results[kind] = {
                "size_mb": os.path.getsize(f"{prefix}.index") / 1e6,
                "build_s": build,
                "search_s": search,
            }
    return results


def main(argv=None) -> Dict[str, Dict[str, float]]:
    p = argparse.ArgumentParser(
        prog="python -m splicetrail.bench.index_size",
        description=__doc__.splitlines()[0],
    )
    p.add_argument("--n", type=int, default=200_000, help="descriptors indexed")
    p.add_argument("--queries", type=int, default=20_000, help="query descriptors")
    p.add_argument("--k", type=int, default=8, help="neighbours per query")
    p.add_argument("--nprobe", type=int, default=16, help="IVF cells visited")
    p.add_argument("--seed", type=int, default=0)
    args = p.parse_args(argv)
    r = measure(args.n, args.queries, args.k, args.nprobe, args.seed)
    print(f"{args.n} descriptors (25-d), {args.queries} queries, k={args.k}\n")
    print(f"{'index':8}{'size MB':>10}{'build s':>10}{'search s':>10}")
    for kind, row in r.items():
        print(
            f"{kind:8}{row['size_mb']:10.1f}{row['build_s']:10.2f}{row['search_s']:10.3f}"
        )
    flat, pq = r["flat"], r["ivfpq"]
    print(
        f"\nivfpq is {flat['size_mb'] / pq['size_mb']:.1f}x smaller and searches "
        f"{flat['search_s'] / pq['search_s']:.1f}x faster"
    )
    return r


if __name__ == "__main__":
    main()
