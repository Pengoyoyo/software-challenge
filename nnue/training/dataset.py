"""Binary dataset loader with player-relative feature encoding.

File format: 108 bytes/sample
  board[100]: u8  piece at each square (0-7)
  player[1]:  u8  1=ONE, 2=TWO
  turn[2]:    u16 LE
  score[4]:   i32 LE  raw AB score from mover's POV
  outcome[1]: i8  +1=mover wins, 0=draw, -1=mover loses
"""

import struct
import numpy as np
import torch
from pathlib import Path
from torch.utils.data import Dataset

SAMPLE_BYTES = 108
LABEL_SCALE = 3000.0  # tanh(score / scale) → [-1, 1]

# piece index → one-hot position (0=empty, 1-3=own_s/m/l, 4-6=opp_s/m/l, 7=squid)
_OWN_PIECE_ONE = {1: 1, 2: 2, 3: 3}   # player ONE's own pieces
_OPP_PIECE_ONE = {4: 4, 5: 5, 6: 6}   # player ONE sees TWO's pieces as opponent
_OWN_PIECE_TWO = {4: 1, 5: 2, 6: 3}   # player TWO's own pieces
_OPP_PIECE_TWO = {1: 4, 2: 5, 3: 6}   # player TWO sees ONE's pieces as opponent


def encode_board(board: np.ndarray, player: int) -> np.ndarray:
    """board: int8[100], player: 1 or 2 → float32[800]."""
    features = np.zeros(800, dtype=np.float32)
    if player == 1:
        own_map, opp_map = _OWN_PIECE_ONE, _OPP_PIECE_ONE
    else:
        own_map, opp_map = _OWN_PIECE_TWO, _OPP_PIECE_TWO

    for sq in range(100):
        piece = int(board[sq])
        if piece == 0:
            feat_idx = sq * 8 + 0  # empty
        elif piece == 7:
            feat_idx = sq * 8 + 7  # squid
        elif piece in own_map:
            feat_idx = sq * 8 + own_map[piece]
        elif piece in opp_map:
            feat_idx = sq * 8 + opp_map[piece]
        else:
            continue
        features[feat_idx] = 1.0
    return features


class PiranhaNNUEDataset(Dataset):
    SAMPLE_DTYPE = np.dtype([
        ("board",   "u1", 100),
        ("player",  "u1"),
        ("_pad",    "u1", 2),
        ("score",   "<i4"),
        ("outcome", "i1"),
    ])

    def __init__(self, paths, max_score: int = 500_000, wdl_lambda: float = 0.5):
        raw = b"".join(p.read_bytes() for p in paths)
        records = np.frombuffer(raw, dtype=self.SAMPLE_DTYPE)
        n_total = len(records)

        mask = np.abs(records["score"]) <= max_score
        records = records[mask]

        feats = np.stack([
            encode_board(b, p) for b, p in zip(records["board"], records["player"])
        ])

        eval_label    = np.tanh(records["score"].astype(np.float32) / LABEL_SCALE)
        outcome_label = records["outcome"].astype(np.float32)
        labels = wdl_lambda * outcome_label + (1.0 - wdl_lambda) * eval_label

        self.X = torch.from_numpy(feats)
        self.y = torch.from_numpy(labels)
        print(f"Loaded {len(self.y):,} samples from {n_total:,} total "
              f"(filtered {n_total - len(self.y):,} out-of-range, λ={wdl_lambda})")

    def __len__(self):
        return len(self.y)

    def __getitem__(self, idx):
        return self.X[idx], self.y[idx]
