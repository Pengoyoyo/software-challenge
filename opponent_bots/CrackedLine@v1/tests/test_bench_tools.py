from __future__ import annotations

from collections import Counter, defaultdict
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class BenchToolTests(unittest.TestCase):
    def test_build_policy_merge_is_order_independent(self) -> None:
        bpc = load_module("build_policy_cache", ROOT / "bench" / "build_policy_cache.py")

        samples_a = [(10, 111, 20, 6), (10, 111, 24, 8), (22, 222, -3, 5)]
        samples_b = [(22, 333, 5, 4), (10, 111, 22, 7)]

        aggs_1 = defaultdict(lambda: bpc.Agg(move_counts=Counter()))
        bpc.merge_samples(aggs_1, samples_a)
        bpc.merge_samples(aggs_1, samples_b)

        aggs_2 = defaultdict(lambda: bpc.Agg(move_counts=Counter()))
        bpc.merge_samples(aggs_2, samples_b)
        bpc.merge_samples(aggs_2, samples_a)

        self.assertEqual(aggs_1[10].samples, aggs_2[10].samples)
        self.assertEqual(aggs_1[10].score_sum, aggs_2[10].score_sum)
        self.assertEqual(aggs_1[10].depth_sum, aggs_2[10].depth_sum)
        self.assertEqual(aggs_1[10].move_counts, aggs_2[10].move_counts)
        self.assertEqual(aggs_1[22].move_counts, aggs_2[22].move_counts)

    def test_seed_for_game_seed_count_cycle(self) -> None:
        bpc = load_module("build_policy_cache", ROOT / "bench" / "build_policy_cache.py")
        values = [bpc.seed_for_game(i, 500, 3) for i in range(8)]
        self.assertEqual(values, [500, 501, 502, 500, 501, 502, 500, 501])

    def test_tune_weights_roundtrip(self) -> None:
        tune = load_module("tune_eval_spsa", ROOT / "bench" / "tune_eval_spsa.py")
        weights = dict(tune.DEFAULT_WEIGHTS)
        weights["w_late_disconnect_pressure"] += 123
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "weights.txt"
            tune.write_weights_file(path, weights)
            loaded = tune.load_weights_file(path)
        self.assertEqual(loaded["w_late_disconnect_pressure"], weights["w_late_disconnect_pressure"])
        self.assertEqual(loaded["w_largest"], weights["w_largest"])

    def test_clamp_weight_non_negative(self) -> None:
        tune = load_module("tune_eval_spsa", ROOT / "bench" / "tune_eval_spsa.py")
        self.assertEqual(tune.clamp_weight("w_largest", -5000, 340), 0)
        self.assertGreaterEqual(tune.clamp_weight("connect_bonus", 10_000_000, 70_000), 0)


if __name__ == "__main__":
    unittest.main()
