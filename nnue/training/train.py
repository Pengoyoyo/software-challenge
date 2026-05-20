"""Train the NNUE model.

Usage:
    python training/train.py --data data/train.bin [--val data/val.bin]
                             [--epochs 20] [--batch 4096] [--lr 1e-3]
                             [--l1 256] [--l2 32] [--out model.pt]
"""

import argparse
import math
import torch
import torch.nn as nn
from torch.utils.data import DataLoader, random_split
from pathlib import Path

from dataset import PiranhaNNUEDataset
from model import NNUE

def search_folder(args):
    path = Path(args.data)
    if path.is_file():
        return [path]
    bins: list[Path] = []
    for file in path.glob("*.bin"):
        bins.append(file)
    return bins

def train(args):
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"Device: {device}")

    full_ds = PiranhaNNUEDataset(search_folder(args), wdl_lambda=args.wdl_lambda)

    if args.val:
        val_ds = PiranhaNNUEDataset([Path(args.val)])
        train_ds = full_ds
    else:
        val_size = max(1, int(0.05 * len(full_ds)))
        train_size = len(full_ds) - val_size
        train_ds, val_ds = random_split(full_ds, [train_size, val_size])

    train_loader = DataLoader(train_ds, batch_size=args.batch, shuffle=True, num_workers=4, pin_memory=True)
    val_loader   = DataLoader(val_ds,   batch_size=args.batch, shuffle=False, num_workers=2)

    model = NNUE(l1_size=args.l1, l2_size=args.l2).to(device)
    opt = torch.optim.Adam(model.parameters(), lr=args.lr)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=args.epochs)
    loss_fn = nn.MSELoss()

    best_val = math.inf
    for epoch in range(1, args.epochs + 1):
        model.train()
        total_loss = 0.0
        for X, y in train_loader:
            X, y = X.to(device), y.to(device)
            opt.zero_grad()
            pred = model(X)
            loss = loss_fn(pred, y)
            loss.backward()
            opt.step()
            total_loss += loss.item() * len(y)
        train_loss = total_loss / len(train_ds)

        model.eval()
        with torch.no_grad():
            val_loss = sum(loss_fn(model(X.to(device)), y.to(device)).item() * len(y) for X, y in val_loader) / len(val_ds)

        scheduler.step()
        marker = " *" if val_loss < best_val else ""
        if val_loss < best_val:
            best_val = val_loss
            torch.save(model.state_dict(), args.out)
        print(f"Epoch {epoch:3d}/{args.epochs}  train={train_loss:.6f}  val={val_loss:.6f}{marker}")

    print(f"\nBest val loss: {best_val:.6f} — saved to {args.out}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--data", required=True)
    p.add_argument("--val",        default=None)
    p.add_argument("--epochs",     type=int,   default=20)
    p.add_argument("--batch",      type=int,   default=4096)
    p.add_argument("--lr",         type=float, default=1e-3)
    p.add_argument("--l1",         type=int,   default=256)
    p.add_argument("--l2",         type=int,   default=32)
    p.add_argument("--wdl-lambda", type=float, default=0.5)
    p.add_argument("--out",        default="model.pt")
    train(p.parse_args())
