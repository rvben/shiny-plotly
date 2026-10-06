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
        # Read the partial edges once, reusing their buffer for minimum and maximum.
        edge_at = np.concatenate([left_at, right_at], axis=1)
        edge_valid = np.concatenate([left_valid, right_valid], axis=1)
        edge_values = self.y[edge_at]
        edge_valid &= np.isfinite(edge_values)
        invalid = ~edge_valid
        np.copyto(edge_values, np.inf, where=invalid)
        columns = edge_values.argmin(axis=1)
        edge_low = np.where(edge_valid[rows, columns], edge_at[rows, columns], -1)
        np.copyto(edge_values, -np.inf, where=invalid)
        columns = edge_values.argmax(axis=1)
        edge_high = np.where(edge_valid[rows, columns], edge_at[rows, columns], -1)
        picks = []
        for extremes, edge, minimum in ((self.low, edge_low, True), (self.high, edge_high, False)):
            if blocks.shape[1] == 0:
                picks.append(edge[edge >= 0])
                continue
            middle_at = extremes[blocks]
            valid = block_valid & (middle_at >= 0)
            values = self.y[middle_at]
            sentinel = np.inf if minimum else -np.inf
            np.copyto(values, sentinel, where=~valid)
            columns = values.argmin(axis=1) if minimum else values.argmax(axis=1)
            middle = np.where(valid[rows, columns], middle_at[rows, columns], -1)
            # Invalid positions have index -1. Mask their values before comparisons;
            # ties between valid extrema always take the earlier global index.
            edge_y = np.where(edge >= 0, self.y[edge], sentinel)
            middle_y = np.where(middle >= 0, self.y[middle], sentinel)
            better = middle_y < edge_y if minimum else middle_y > edge_y
            take_middle = (middle >= 0) & (
                (edge < 0) | better | ((middle_y == edge_y) & (middle < edge))
            )
            selected = np.where(take_middle, middle, edge)
            picks.append(selected[selected >= 0])
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
