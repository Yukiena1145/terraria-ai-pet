"""地形分类测试：绿色植物不能再被当成石块，未知方块要有自己的符号。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from monitor import MemoryGameMonitor, TILE_DIRT, TILE_PLANT, TILE_STONE, TILE_TREE, TILE_WOOD_BLOCK  # noqa: E402


def cell(tile_type: int, *, wall: int = 0) -> dict:
    return {"has_tile": True, "tile_type": tile_type, "has_wall": wall > 0, "wall_type": wall}


class TileCharTest(unittest.TestCase):
    def test_plant_is_not_stone(self) -> None:
        # 3 = 植物，24 = 腐化植物，52 = 藤蔓，27 = 向日葵
        for tile_id in (3, 24, 52, 27):
            self.assertEqual(MemoryGameMonitor._tile_char(cell(tile_id)), TILE_PLANT, tile_id)
        self.assertNotEqual(MemoryGameMonitor._tile_char(cell(3)), TILE_STONE)

    def test_grass_ground_is_still_dirt(self) -> None:
        # 草地/丛林草是实心地面，仍然算泥土，能挖。
        for tile_id in (0, 2, 23, 60, 70, 109):
            self.assertEqual(MemoryGameMonitor._tile_char(cell(tile_id)), TILE_DIRT, tile_id)

    def test_stone_and_wood(self) -> None:
        self.assertEqual(MemoryGameMonitor._tile_char(cell(1)), TILE_STONE)
        self.assertEqual(MemoryGameMonitor._tile_char(cell(30)), TILE_WOOD_BLOCK)
        self.assertEqual(MemoryGameMonitor._tile_char(cell(5)), TILE_TREE)

    def test_unknown_has_own_symbol(self) -> None:
        # 9999 不在任何表里：以前会变成 S（石块），现在必须是 ?。
        self.assertEqual(MemoryGameMonitor._tile_char(cell(9999)), "?")

    def test_wall_when_no_tile(self) -> None:
        self.assertEqual(
            MemoryGameMonitor._tile_char({"has_tile": False, "has_wall": True, "tile_type": 0}),
            "B",
        )


if __name__ == "__main__":
    unittest.main()
