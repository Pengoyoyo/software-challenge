"""Binary dataset loader with player-relative feature encoding.

File format: 107 bytes/sample
  board[100]: u8  piece at each square (0-7)
  player[1]:  u8  1=ONE, 2=TWO
  turn[2]:    u16 LE
  score[4]:   i32 LE  raw AB score from mover's POV
"""

import struct
import numpy as np
import torch
from torch.utils.data import Dataset

SAMPLE_BYTES = 107
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
    def __init__(self, path: str, max_score: int = 500_000):
        with open(path, "rb") as f:
            raw = f.read()

        n_total = len(raw) // SAMPLE_BYTES
        boards, players, labels = [], [], []

        for i in range(n_total):
            offset = i * SAMPLE_BYTES
            board = np.frombuffer(raw[offset:offset + 100], dtype=np.uint8).copy()
            player = raw[offset + 100]
            score = struct.unpack_from("<i", raw, offset + 103)[0]

            if abs(score) > max_score:
                continue

            feat = encode_board(board, player)
            label = np.tanh(score / LABEL_SCALE)

            boards.append(feat)
            players.append(player)
            labels.append(label)

        self.X = torch.from_numpy(np.stack(boards))
        self.y = torch.tensor(labels, dtype=torch.float32)
        print(f"Loaded {len(self.y):,} samples from {n_total:,} total (filtered {n_total - len(self.y):,} terminal)")

    def __len__(self) -> int:
        return len(self.y)

    def __getitem__(self, idx):
        return self.X[idx], self.y[idx]
