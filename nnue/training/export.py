"""Export trained NNUE weights to a flat binary file for Rust.

Layout (all f32 little-endian):
  l1_w:  [800 * L1]
  l1_b:  [L1]
  l2_w:  [L1 * L2]
  l2_b:  [L2]
  out_w: [L2]
  out_b: [1]

Usage:
    python training/export.py --model model.pt --out data/weights.bin [--l1 256] [--l2 32]
"""

import argparse
import struct
import torch

from model import NNUE


def export(args):
    model = NNUE(l1_size=args.l1, l2_size=args.l2)
    model.load_state_dict(torch.load(args.model, map_location="cpu"))
    model.eval()

    tensors = [
        model.l1.weight.data,   # [l1, 800]
        model.l1.bias.data,     # [l1]
        model.l2.weight.data,   # [l2, l1]
        model.l2.bias.data,     # [l2]
        model.out.weight.data,  # [1, l2]
        model.out.bias.data,    # [1]
    ]

    with open(args.out, "wb") as f:
        for t in tensors:
            data = t.float().cpu().numpy().flatten()
            f.write(struct.pack(f"<{len(data)}f", *data))

    total = sum(t.numel() for t in tensors)
    print(f"Exported {total:,} floats ({total * 4 / 1024:.1f} KB) → {args.out}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--model", required=True)
    p.add_argument("--out",   required=True)
    p.add_argument("--l1",    type=int, default=256)
    p.add_argument("--l2",    type=int, default=32)
    export(p.parse_args())
