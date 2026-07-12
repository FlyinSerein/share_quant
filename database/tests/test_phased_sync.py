from __future__ import annotations

import unittest

from share_quant.phased_sync import plan_chunks


class PhasedSyncTest(unittest.TestCase):
    def test_plan_chunks_uses_groups_and_dataset_chunk_size(self) -> None:
        chunks = plan_chunks(
            groups=["static", "market"],
            enabled={"stock_basic": True, "daily": True, "trade_cal": False},
            start="2021-01-01",
            end="2021-01-03",
        )

        self.assertEqual([chunk.key for chunk in chunks], [
            "stock_basic:-:-",
            "daily:2021-01-01:2021-01-01",
            "daily:2021-01-02:2021-01-02",
            "daily:2021-01-03:2021-01-03",
        ])


if __name__ == "__main__":
    unittest.main()
