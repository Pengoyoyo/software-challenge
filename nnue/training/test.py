from pathlib import Path
import argparse

def search_folder(args):
    folder = Path(args.data_folder)
    for file in folder.iterdir():
        if file.suffix == ".bin":
            print(True)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--data-folder", required=True)
    p.add_argument("--val",    default=None)
    p.add_argument("--epochs", type=int, default=20)
    p.add_argument("--batch",  type=int, default=4096)
    p.add_argument("--lr",     type=float, default=1e-3)
    p.add_argument("--l1",     type=int, default=256)
    p.add_argument("--l2",     type=int, default=32)
    p.add_argument("--out",    default="model.pt")
    search_folder(p.parse_args())

