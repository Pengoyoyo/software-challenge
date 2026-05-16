"""NNUE model: 800 → 256 → 32 → 1 with ClippedReLU."""

import torch
import torch.nn as nn


class NNUE(nn.Module):
    def __init__(self, l1_size: int = 256, l2_size: int = 32):
        super().__init__()
        self.l1 = nn.Linear(800, l1_size)
        self.l2 = nn.Linear(l1_size, l2_size)
        self.out = nn.Linear(l2_size, 1)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = torch.clamp(self.l1(x), 0.0, 1.0)
        x = torch.clamp(self.l2(x), 0.0, 1.0)
        return self.out(x).squeeze(-1)
