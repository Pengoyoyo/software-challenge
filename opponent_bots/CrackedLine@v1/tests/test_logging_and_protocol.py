from __future__ import annotations

import contextlib
import io
import unittest

from logic import PiranhasBot
from rust_bridge import DepthTrace, RustEngineProcess


class ProtocolAndLoggingTests(unittest.TestCase):
    def test_parse_extended_result_with_iterations(self) -> None:
        bridge = RustEngineProcess()
        line = (
            "result 1 19 28 -112 6 700000000 341797 12000 5346 2200 15000 3 1 2 0 12 4 39 2 2 "
            "1,-20,272,0,1000000,272000;2,-216,498,30,1000000,498000"
        )
        result = bridge._parse_result(line)

        self.assertTrue(result.has_move)
        self.assertEqual(result.from_sq, 19)
        self.assertEqual(result.to_sq, 28)
        self.assertEqual(result.legal_root_count, 39)
        self.assertEqual(result.team, 2)
        self.assertEqual(len(result.iterations), 2)
        self.assertEqual(result.iterations[0].depth, 1)
        self.assertEqual(result.iterations[1].score, -216)

    def test_parse_hash_response(self) -> None:
        bridge = RustEngineProcess()
        self.assertEqual(bridge._parse_hash("hash 123456789"), 123456789)

    def test_direction_mapping(self) -> None:
        self.assertEqual(PiranhasBot._direction_name_and_arrow(11, 22), ("DownRight", "↘"))
        self.assertEqual(PiranhasBot._direction_name_and_arrow(22, 11), ("UpLeft", "↖"))
        self.assertEqual(PiranhasBot._direction_name_and_arrow(25, 35), ("Down", "↓"))

    def test_move_block_format(self) -> None:
        bot = object.__new__(PiranhasBot)
        traces = [
            DepthTrace(depth=1, score=-20, nodes_delta=272, tt_hits_delta=0, elapsed_ns_delta=1_000_000, nps_iter=677478),
            DepthTrace(depth=2, score=-216, nodes_delta=498, tt_hits_delta=30, elapsed_ns_delta=1_000_000, nps_iter=845288),
        ]

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            bot._print_move_block(turn=2, legal_count=39, team=2, chosen_key=(19, 28), traces=traces)

        lines = [line.rstrip("\n") for line in stderr.getvalue().splitlines()]
        self.assertEqual(lines[0], "=== Zug 2 ===")
        self.assertEqual(lines[1], "Rust Search: 39 moves, team=2")
        self.assertEqual(lines[2], "d1: -20 | 272n 0h 677478nps 0.00s")
        self.assertEqual(lines[3], "d2: -216 | 498n 30h 845288nps 0.00s")
        self.assertEqual(lines[4], "-> (9, 1) (DownLeft ↙)")


if __name__ == "__main__":
    unittest.main()
