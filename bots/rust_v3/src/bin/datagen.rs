/// Data generator for NNUE training.
///
/// Plays self-play games from realistic randomized starting positions, searching
/// each position for `time_ms` ms. Writes samples in the binary format consumed
/// by nnue/training/dataset.py (108 bytes/sample):
///
///   board[100]: u8   piece at each square (0-7)
///   player[1]:  u8   side to move (1=ONE, 2=TWO)
///   turn[2]:    u16  LE
///   score[4]:   i32  LE (AB score from mover's POV)
///   outcome[1]: i8   +1=mover wins, 0=draw, -1=mover loses
///
/// Starting position mirrors real Piranhas 2026 rules:
///   ONE (red):  col 0 + col 9, rows 1-8 (16 fish, mixed sizes, symmetric)
///   TWO (blue): row 0 + row 9, cols 1-8 (16 fish, mixed sizes, symmetric)
///   Squids: 2 kraken in inner 6×6 (rows 2-7, cols 2-7), no shared row/col/diagonal
///
/// Usage:
///   cargo run --release --bin datagen -- <n_games> <time_ms> <out.bin>

use piranhas_bot_v2::board::{Position, Undo, MoveList, SQUID, ONE, TWO};
use piranhas_bot_v2::search::SearchEngine;
use std::fs::OpenOptions;
use std::io::{BufWriter, Write};
use std::time::{Duration, Instant};

// ─── splitmix64 RNG ───────────────────────────────────────────────────────────

struct Rng(u64);

impl Rng {
    fn new(seed: u64) -> Self { Self(seed) }
    fn next(&mut self) -> u64 {
        self.0 = self.0.wrapping_add(0x9e3779b97f4a7c15u64);
        let mut z = self.0;
        z = (z ^ (z >> 30)).wrapping_mul(0xbf58476d1ce4e5b9u64);
        z = (z ^ (z >> 27)).wrapping_mul(0x94d049bb133111ebu64);
        z ^ (z >> 31)
    }
    fn range(&mut self, n: u64) -> u64 { self.next() % n }
}

// ─── Fish size distribution ───────────────────────────────────────────────────

/// Returns a random fish size offset (0=S, 1=M, 2=L) biased toward small.
/// Roughly: 50% S, 30% M, 20% L — matches "tendenziell mehr kleine als große".
fn rand_size(rng: &mut Rng) -> u8 {
    match rng.range(10) {
        0..=4 => 0, // S
        5..=7 => 1, // M
        _     => 2, // L
    }
}

// ─── Realistic random starting position ──────────────────────────────────────

/// Build a starting position matching the real Piranhas 2026 rules:
/// - ONE fish on col 0 + col 9 (rows 1-8), symmetric mixed sizes
/// - TWO fish on row 0 + row 9 (cols 1-8), symmetric mixed sizes
/// - 2 squids in the inner 6×6, no shared row/col/diagonal
fn random_start(rng: &mut Rng) -> Position {
    let mut pos = Position::default();

    // ONE (red): cols 0 and 9, rows 1-8
    // Same size distribution on both columns (symmetric left↔right)
    for row in 1..=8usize {
        let size = rand_size(rng); // 0=S, 1=M, 2=L → piece value 1/2/3
        pos.board[row * 10 + 0] = 1 + size;
        pos.board[row * 10 + 9] = 1 + size;
    }

    // TWO (blue): rows 0 and 9, cols 1-8
    // Same size distribution on both rows (symmetric top↔bottom)
    for col in 1..=8usize {
        let size = rand_size(rng); // → piece value 4/5/6
        pos.board[0 * 10 + col] = 4 + size;
        pos.board[9 * 10 + col] = 4 + size;
    }

    // Place 2 squids in inner 6×6 (rows 2-7, cols 2-7)
    // Constraint: no shared row, column, or diagonal
    place_squids(&mut pos, rng);

    pos.recompute_caches();
    pos
}

fn place_squids(pos: &mut Position, rng: &mut Rng) {
    // All 36 squares in the inner 6×6
    let inner: Vec<(usize, usize)> = (2..=7usize)
        .flat_map(|r| (2..=7usize).map(move |c| (r, c)))
        .collect();

    // First squid: pick any inner square
    let i1 = rng.range(inner.len() as u64) as usize;
    let (r1, c1) = inner[i1];

    // Second squid: must not share row, col, or diagonal with first
    let candidates: Vec<(usize, usize)> = inner.iter()
        .filter(|&&(r, c)| {
            r != r1
            && c != c1
            && (r as i32 - r1 as i32).abs() != (c as i32 - c1 as i32).abs()
        })
        .copied()
        .collect();

    pos.board[r1 * 10 + c1] = SQUID;

    if !candidates.is_empty() {
        let i2 = rng.range(candidates.len() as u64) as usize;
        let (r2, c2) = candidates[i2];
        pos.board[r2 * 10 + c2] = SQUID;
    }
}

// ─── Random playout for position diversity ────────────────────────────────────

/// Play `n` random legal moves to reach mid-game positions.
fn randomize_position(pos: &mut Position, n: usize, rng: &mut Rng) {
    for _ in 0..n {
        let mut ml = MoveList::new();
        pos.generate_moves_for(pos.player, &mut ml);
        if ml.len == 0 { break; }
        let idx = rng.range(ml.len as u64) as usize;
        let mut undo = Undo::default();
        if !pos.make_move(ml.moves[idx], &mut undo) { break; }
    }
}

// ─── Sample types ─────────────────────────────────────────────────────────────

struct PendingSample {
    board: [u8; 100],
    player: u8,
    turn: u16,
    score: i32,
}

fn write_sample(out: &mut impl Write, s: &PendingSample, outcome: i8) -> std::io::Result<()> {
    out.write_all(&s.board)?;
    out.write_all(&[s.player])?;
    out.write_all(&s.turn.to_le_bytes())?;
    out.write_all(&s.score.to_le_bytes())?;
    out.write_all(&[outcome as u8])?;
    Ok(())
}

// ─── Winner determination ─────────────────────────────────────────────────────

fn determine_winner(pos: &Position) -> Option<u8> {
    let red_swarm  = pos.largest_component_value(ONE);
    let blue_swarm = pos.largest_component_value(TWO);

    if red_swarm > blue_swarm {
        Some(ONE)
    } else if blue_swarm > red_swarm {
        Some(TWO)
    } else {
        match (pos.connected_since[0], pos.connected_since[1]) {
            (Some(r), Some(b)) if r < b => Some(ONE),
            (Some(r), Some(b)) if b < r => Some(TWO),
            _ => None,
        }
    }
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

    if let Some(parent) = std::path::Path::new(out_path).parent() {
        std::fs::create_dir_all(parent).expect("Cannot create output directory");
    }

    let file = OpenOptions::new()
        .create(true).append(true)
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
        let mut engine = SearchEngine::new();

        // Realistic start + 4-16 random moves for mid-game diversity
        let mut pos = random_start(&mut rng);
        let n_rand = 4 + rng.range(13) as usize;
        randomize_position(&mut pos, n_rand, &mut rng);

        let mut pending: Vec<PendingSample> = Vec::new();

        loop {
            let mut ml = MoveList::new();
            pos.generate_moves_for(pos.player, &mut ml);
            if ml.len == 0 || pos.turn >= 120 { break; }

            let deadline = Instant::now() + Duration::from_millis(time_ms);
            let result = engine.search(&mut pos, deadline);
            let score = result.depths.last().map(|d| d.score).unwrap_or(0);

            if result.best_move.is_none() { break; }

            if score.abs() < skip_near_terminal {
                pending.push(PendingSample {
                    board: pos.board,
                    player: pos.player,
                    turn: pos.turn,
                    score,
                });
            } else {
                total_skipped += 1;
            }

            let mut undo = Undo::default();
            if !pos.make_move(result.best_move.unwrap(), &mut undo) { break; }
        }

        // Determine winner and write all buffered samples with outcome label
        let winner = determine_winner(&pos);
        for s in &pending {
            let outcome: i8 = match winner {
                Some(p) if p == s.player =>  1,
                Some(_)                  => -1,
                None                     =>  0,
            };
            write_sample(&mut out, s, outcome).expect("write failed");
            total_samples += 1;
        }

        if (game_idx + 1) % 50 == 0 || game_idx + 1 == n_games {
            println!(
                "Game {:>5}/{} | samples={:>8} skipped={:>6} | {:.0}s elapsed",
                game_idx + 1, n_games, total_samples, total_skipped,
                t0.elapsed().as_secs_f64(),
            );
        }
    }

    out.flush().expect("flush failed");
    println!("\nDone. {} samples written to {}", total_samples, out_path);
}
