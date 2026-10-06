"""An exact block index for repeated broad views of very long, sparsely gapped lines."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

BLOCK = 64


@dataclass
class ExtremaIndex:
    y: np.ndarray
    low: np.ndarray
    high: np.ndarray
    gaps: np.ndarray

    @classmethod
    def build(cls, y: np.ndarray) -> ExtremaIndex | None:
        """Index each block's first finite min/max; decline densely gapped data.

        Indices and sparse gap positions together retain at most 0.375 bytes per point
        (plus one partial block), rather than another full-size numeric array.
        """
        finite = np.isfinite(y)
        if len(y) - np.count_nonzero(finite) > len(y) // BLOCK:
            return None
        count = -(-len(y) // BLOCK)
        rows = np.arange(count)
        work = np.full(count * BLOCK, np.inf)
        np.copyto(work[: len(y)], y, where=finite)
        buckets = work.reshape(count, BLOCK)
        low = buckets.argmin(axis=1)
        valid = np.isfinite(buckets[rows, low])
        low = np.where(valid, rows * BLOCK + low, -1)
        work[: len(y)][~finite] = -np.inf
        work[len(y) :] = -np.inf
        high = buckets.argmax(axis=1)
        high = np.where(valid, rows * BLOCK + high, -1)
        return cls(y, low, high, np.flatnonzero(~finite))

    def pick(self, start: int, stop: int, budget: int, *, connectgaps: bool) -> np.ndarray:
        """Exactly the ordinary sample's absolute indices in ``y[start:stop]``.

        Complete blocks contribute their indexed extrema. Only the two partial edges
        of each bucket are scanned; columns remain in index order, preserving ties.
        """
        n = stop - start
        if n <= budget:
            return np.arange(start, stop)
        width = -(-(n - 2) // ((budget - 2) // 2))
        count = -(-(n - 2) // width)
        rows = np.arange(count)
        starts = start + 1 + rows * width
        stops = np.minimum(starts + width, stop - 1)
        left = -(-starts // BLOCK)
        right = stops // BLOCK
        offsets = np.arange(BLOCK)
        left_at = starts[:, None] + offsets
        left_valid = left_at < np.minimum(stops, left * BLOCK)[:, None]
        right_at = np.maximum(starts, right * BLOCK)[:, None] + offsets
        right_valid = right_at < stops[:, None]
        # Padding is masked below; clip before gathering so the final bucket stays safe.
        left_at = np.minimum(left_at, len(self.y) - 1)
        right_at = np.minimum(right_at, len(self.y) - 1)
        blocks = left[:, None] + np.arange(max(0, int(np.max(right - left))))
        block_valid = blocks < right[:, None]
        blocks = np.minimum(blocks, len(self.low) - 1)
        picks = []
        for extremes, minimum in ((self.low, True), (self.high, False)):
            middle_at = extremes[blocks]
            at = np.concatenate([left_at, middle_at, right_at], axis=1)
            valid = np.concatenate(
                [left_valid, block_valid & (middle_at >= 0), right_valid], axis=1
            )
            values = self.y[at]
            valid &= np.isfinite(values)
            values = np.where(valid, values, np.inf if minimum else -np.inf)
            columns = values.argmin(axis=1) if minimum else values.argmax(axis=1)
            picks.append(at[rows, columns][valid[rows, columns]])
        kept = np.unique(np.concatenate([[start, stop - 1], *picks]))
        if connectgaps or len(self.gaps) == 0:
            return kept
        after = np.searchsorted(self.gaps, kept[:-1], side="right")
        first = self.gaps[np.minimum(after, len(self.gaps) - 1)]
        between = (
            (after < len(self.gaps))
            & (first < kept[1:])
            & np.isfinite(self.y[kept[:-1]])
            & np.isfinite(self.y[kept[1:]])
        )
        return np.union1d(kept, first[between])
