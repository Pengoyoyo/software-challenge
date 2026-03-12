#!/usr/bin/env python3
"""
Parse Piranhas 2026 server logs into value network training data.

Output: chunked .npz files with:
  boards: (N, 8, 10, 10) float32  -- board tensor, own/opp relative to player to move
  labels: (N,) float32             -- 1.0=win, 0.5=draw, 0.0=loss from player-to-move perspective
  turns:  (N,) int16               -- turn number (0-59)
"""

import argparse
import json
import re
import sys
from multiprocessing import Pool, cpu_count
from pathlib import Path

import numpy as np

try:
    from tqdm import tqdm
    HAS_TQDM = True
except ImportError:
    HAS_TQDM = False
    print("Warning: tqdm not installed, no progress bar", file=sys.stderr)

# ---------------------------------------------------------------------------
# Compiled regex patterns
# ---------------------------------------------------------------------------

RE_GAMESTATESTART = re.compile(
    r'GameState\(turn=(\d+),.*?board=(Board \[\[.+?\]\])\)'
)
RE_RESULT = re.compile(
    r'GameResult\(winner=.+?, scores=\[\[Siegpunkte=(\d+), Schwarmgr\S+=\d+\], '
    r'\[Siegpunkte=(\d+), Schwarmgr\S+=\d+\]\]\)'
)
RE_CELL = re.compile(r'\((\d+), (\d+)\) (Leer|Krake|red[123]|blue[123])')

CELL_CHANNEL = {
    'red1': 0, 'red2': 1, 'red3': 2,
    'blue1': 3, 'blue2': 4, 'blue3': 5,
    'Krake': 6,
    # 'Leer' -> nothing set (stays 0)
}

# ---------------------------------------------------------------------------
# Core parsing functions
# ---------------------------------------------------------------------------

def parse_result(text: str):
    """Return (one_score, two_score) as floats (1.0/0.5/0.0) or None if not found."""
    m = RE_RESULT.search(text)
    if not m:
        return None
    mapping = {'2': 1.0, '1': 0.5, '0': 0.0}
    return mapping[m.group(1)], mapping[m.group(2)]


def parse_board(board_text: str) -> np.ndarray:
    """
    Parse board string into a (7, 10, 10) float32 array.
    Channels 0-2: red1/2/3, channels 3-5: blue1/2/3, channel 6: Krake.
    x maps to columns (axis 2), y maps to rows (axis 1).
    """
    board = np.zeros((7, 10, 10), dtype=np.float32)
    for x_str, y_str, cell_type in RE_CELL.findall(board_text):
        ch = CELL_CHANNEL.get(cell_type)
        if ch is not None:
            board[ch, int(y_str), int(x_str)] = 1.0
    return board


def extract_board_states(text: str) -> list:
    """
    Extract (turn, board_text) pairs from a server log.
    Includes turn=0 from 'Starting Game' and turns 1+ from 'Current State:'.
    Deduplicates by turn number (first occurrence wins).
    """
    states = {}
    for line in text.splitlines():
        if 'GameState(turn=' not in line:
            continue
        m = RE_GAMESTATESTART.search(line)
        if not m:
            continue
        turn = int(m.group(1))
        if turn not in states:
            states[turn] = m.group(2)
    return sorted(states.items())


def encode_game(server_log_path: Path):
    """
    Parse one server log file and return a list of sample dicts, or None on failure.
    Each sample: {'board': (8,10,10) float32, 'label': float32, 'turn': int}
    """
    try:
        text = server_log_path.read_text(encoding='utf-8', errors='replace')

        result = parse_result(text)
        if result is None:
            return None

        one_score, two_score = result
        states = extract_board_states(text)

        samples = []
        for turn, board_text in states:
            board7 = parse_board(board_text)

            # Rotate channels so own/opp is relative to player to move.
            # Even turns: ONE (red, channels 0-2) moves → already own.
            # Odd turns:  TWO (blue, channels 3-5) moves → swap red/blue.
            if turn % 2 == 0:
                label = one_score
                board_rel = board7
            else:
                label = two_score
                # swap channels: [blue0,blue1,blue2, red0,red1,red2, krake]
                board_rel = np.concatenate([board7[3:6], board7[0:3], board7[6:7]], axis=0)

            # Add turn-progress channel (broadcast scalar)
            turn_plane = np.full((1, 10, 10), turn / 60.0, dtype=np.float32)
            board_final = np.concatenate([board_rel, turn_plane], axis=0)  # (8,10,10)

            samples.append({
                'board': board_final,
                'label': np.float32(label),
                'turn': turn,
            })

        return samples

    except Exception as e:
        print(f"Error processing {server_log_path.name}: {e}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------

def save_chunk(output_dir: Path, chunk_idx: int, boards, labels, turns):
    path = output_dir / f"chunk_{chunk_idx:04d}.npz"
    np.savez_compressed(
        path,
        boards=np.array(boards, dtype=np.float32),
        labels=np.array(labels, dtype=np.float32),
        turns=np.array(turns, dtype=np.int16),
    )
    return path


def _encode_game_worker(path):
    """Top-level wrapper for multiprocessing (must be picklable)."""
    return encode_game(path)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description='Parse Piranhas server logs into value network training data.'
    )
    parser.add_argument('--input-dir', required=True, type=Path,
                        help='Directory containing game_*_server.log files')
    parser.add_argument('--output-dir', required=True, type=Path,
                        help='Output directory for .npz chunks')
    parser.add_argument('--chunk-size', type=int, default=50000,
                        help='Samples per output chunk (default: 50000)')
    parser.add_argument('--workers', type=int, default=cpu_count(),
                        help='Parallel workers (default: all CPUs)')
    parser.add_argument('--dry-run', action='store_true',
                        help='Process only 10 games and print summary')
    args = parser.parse_args()

    # Discover log files
    game_files = sorted(args.input_dir.glob('game_*_server.log'))
    if not game_files:
        print(f"No server log files found in {args.input_dir}", file=sys.stderr)
        sys.exit(1)

    if args.dry_run:
        game_files = game_files[:10]
        print(f"Dry run: processing {len(game_files)} games")

    args.output_dir.mkdir(parents=True, exist_ok=True)

    total_games = len(game_files)
    print(f"Found {total_games} games, using {args.workers} workers")

    # Processing loop
    boards_buf, labels_buf, turns_buf = [], [], []
    chunk_idx = 0
    stats = {'processed': 0, 'skipped_no_result': 0, 'failed': 0, 'total_samples': 0}

    iter_fn = tqdm(total=total_games, unit='games') if HAS_TQDM else None

    with Pool(args.workers) as pool:
        for samples in pool.imap_unordered(_encode_game_worker, game_files, chunksize=4):
            if iter_fn:
                iter_fn.update(1)

            if samples is None:
                stats['skipped_no_result'] += 1
                continue

            stats['processed'] += 1
            stats['total_samples'] += len(samples)

            for s in samples:
                boards_buf.append(s['board'])
                labels_buf.append(s['label'])
                turns_buf.append(s['turn'])

            if len(boards_buf) >= args.chunk_size:
                path = save_chunk(args.output_dir, chunk_idx, boards_buf, labels_buf, turns_buf)
                if not args.dry_run:
                    print(f"\nSaved {path.name} ({len(boards_buf)} samples)")
                chunk_idx += 1
                boards_buf.clear()
                labels_buf.clear()
                turns_buf.clear()

    if iter_fn:
        iter_fn.close()

    # Save remainder
    if boards_buf:
        path = save_chunk(args.output_dir, chunk_idx, boards_buf, labels_buf, turns_buf)
        print(f"Saved {path.name} ({len(boards_buf)} samples)")
        chunk_idx += 1

    # Write metadata
    metadata = {
        'total_games': total_games,
        'processed_games': stats['processed'],
        'skipped_no_result': stats['skipped_no_result'],
        'total_samples': stats['total_samples'],
        'chunks': chunk_idx,
        'chunk_size': args.chunk_size,
        'tensor_shape': [8, 10, 10],
        'channels': [
            'own_small', 'own_medium', 'own_large',
            'opp_small', 'opp_medium', 'opp_large',
            'squid', 'turn_progress',
        ],
        'labels': '1.0=win, 0.5=draw, 0.0=loss (from player-to-move perspective)',
    }
    meta_path = args.output_dir / 'metadata.json'
    meta_path.write_text(json.dumps(metadata, indent=2))

    print(f"\nDone.")
    print(f"  Games processed : {stats['processed']}/{total_games}")
    print(f"  Skipped (no result): {stats['skipped_no_result']}")
    print(f"  Total samples   : {stats['total_samples']}")
    print(f"  Chunks saved    : {chunk_idx}")
    print(f"  Metadata        : {meta_path}")

    if args.dry_run and boards_buf == [] and chunk_idx == 0:
        print("\nDry run: no chunks written (fewer than chunk-size samples).")
        print("Sample count:", stats['total_samples'])
        print("Label distribution:",
              f"wins={sum(1 for l in labels_buf if l == 1.0)}, "
              f"draws={sum(1 for l in labels_buf if l == 0.5)}, "
              f"losses={sum(1 for l in labels_buf if l == 0.0)}")


if __name__ == '__main__':
    main()
