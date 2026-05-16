/// Data generator for NNUE training.
///
/// Plays self-play games from randomized starting positions, searching each
/// position for `time_ms` ms. Writes samples in the binary format consumed
/// by nnue/training/dataset.py (107 bytes/sample):
///
///   board[100]: u8   piece at each square (0-7)
///   player[1]:  u8   side to move (1=ONE, 2=TWO)
///   turn[2]:    u16  LE
///   score[4]:   i32  LE (AB score from mover's POV)
///
/// Usage:
///   cargo run --release --bin datagen -- <n_games> <time_ms> <out.bin>
///
/// Example:
///   cargo run --release --bin datagen -- 2000 200 ../../../nnue/data/train.bin

use piranhas_bot_v2::board::{Position, Undo, MoveList};
use piranhas_bot_v2::search::SearchEngine;
use std::fs::OpenOptions;
use std::io::{BufWriter, Write};
use std::time::{Duration, Instant};

// ─── splitmix64 RNG (same as in board.rs) ────────────────────────────────────

struct Rng(u64);

impl Rng {
    fn new(seed: u64) -> Self {
        Self(seed)
    }
    fn next(&mut self) -> u64 {
        self.0 = self.0.wrapping_add(0x9e3779b97f4a7c15u64);
        let mut z = self.0;
        z = (z ^ (z >> 30)).wrapping_mul(0xbf58476d1ce4e5b9u64);
        z = (z ^ (z >> 27)).wrapping_mul(0x94d049bb133111ebu64);
        z ^ (z >> 31)
    }
    fn range(&mut self, n: u64) -> u64 {
        self.next() % n
    }
}

// ─── Standard starting position ──────────────────────────────────────────────

/// Build the standard Piranhas 2026 starting board.
/// ONE (red): columns 0 and 9, rows 1-8 → 16 fish (size 1=small)
/// TWO (blue): rows 0 and 9, cols 1-8 → 16 fish (size 4=small)
fn standard_start() -> Position {
    let mut pos = Position::default();

    // ONE fish on left/right columns (rows 1-8)
    for row in 1..=8usize {
        pos.board[row * 10 + 0] = 1; // ONE small
        pos.board[row * 10 + 9] = 1;
    }
    // TWO fish on top/bottom rows (cols 1-8)
    for col in 1..=8usize {
        pos.board[0 * 10 + col] = 4; // TWO small
        pos.board[9 * 10 + col] = 4;
    }

    pos.recompute_caches();
    pos
}

/// Make `n` random legal moves on `pos` (for exploration).
fn randomize_position(pos: &mut Position, n: usize, rng: &mut Rng) {
    for _ in 0..n {
        let mut ml = MoveList::new();
        pos.generate_moves_for(pos.player, &mut ml);
        if ml.len == 0 {
            break;
        }
        let idx = rng.range(ml.len as u64) as usize;
        let mv = ml.moves[idx];
        let mut undo = Undo::default();
        if !pos.make_move(mv, &mut undo) {
            break; // terminal
        }
    }
}

// ─── Sample serialization ─────────────────────────────────────────────────────

fn write_sample(out: &mut impl Write, pos: &Position, score: i32) -> std::io::Result<()> {
    out.write_all(&pos.board)?;
    out.write_all(&[pos.player])?;
    out.write_all(&pos.turn.to_le_bytes())?;
    out.write_all(&score.to_le_bytes())?;
    Ok(())
}

// ─── Main ─────────────────────────────────────────────────────────────────────

fn main() {
    let args: Vec<String> = std::env::args().collect();
    if args.len() < 4 {
        eprintln!("Usage: datagen <n_games> <time_ms> <out.bin>");
        std::process::exit(1);
    }
    let n_games: usize = args[1].parse().expect("n_games must be integer");
    let time_ms: u64   = args[2].parse().expect("time_ms must be integer");
    let out_path = &args[3];

    let skip_near_terminal: i32 = 500_000;

    let file = OpenOptions::new()
        .create(true)
        .append(true)
        .open(out_path)
        .expect("Cannot open output file");
    let mut out = BufWriter::new(file);

    let mut rng = Rng::new(
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos() as u64,
    );

    let mut total_samples = 0u64;
    let mut total_skipped = 0u64;

    let t0 = Instant::now();

    for game_idx in 0..n_games {
        // Fresh engine per game to avoid accumulated TT bias
        let mut engine = SearchEngine::new();

        let mut pos = standard_start();

        // Randomize: play 4-16 random moves to diversify positions
        let n_rand = 4 + rng.range(13) as usize;
        randomize_position(&mut pos, n_rand, &mut rng);

        // Play the game and save every (position, score) pair
        loop {
            // Terminal check
            {
                let mut ml = MoveList::new();
                pos.generate_moves_for(pos.player, &mut ml);
                if ml.len == 0 || pos.turn >= 120 {
                    break;
                }
            }

            // Search this position
            let deadline = Instant::now() + Duration::from_millis(time_ms);
            let result = engine.search(&mut pos, deadline);

            let score = result.depths.last().map(|d| d.score).unwrap_or(0);

            if result.best_move.is_none() {
                break;
            }

            if score.abs() < skip_near_terminal {
                write_sample(&mut out, &pos, score).expect("write failed");
                total_samples += 1;
            } else {
                total_skipped += 1;
            }

            // Play the best move
            let mv = result.best_move.unwrap();
            let mut undo = Undo::default();
            if !pos.make_move(mv, &mut undo) {
                break;
            }

            // Occasionally make a random alternative move (30% chance) for variety
            if rng.range(10) < 3 {
                let mut ml = MoveList::new();
                pos.generate_moves_for(pos.player, &mut ml);
                if ml.len > 1 {
                    let idx = rng.range(ml.len as u64) as usize;
                    let alt = ml.moves[idx];
                    let mut undo2 = Undo::default();
                    pos.make_move(alt, &mut undo2);
                }
            }
        }

        if (game_idx + 1) % 50 == 0 || game_idx + 1 == n_games {
            let elapsed = t0.elapsed().as_secs_f64();
            println!(
                "Game {:>5}/{} | samples={:>8} skipped={:>6} | {:.0}s elapsed",
                game_idx + 1,
                n_games,
                total_samples,
                total_skipped,
                elapsed,
            );
        }
    }

    out.flush().expect("flush failed");
    println!("\nDone. {} samples written to {}", total_samples, out_path);
}
