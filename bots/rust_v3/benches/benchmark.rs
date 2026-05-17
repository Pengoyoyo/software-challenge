use piranhas_bot_v2::board::{MoveList, Position, Undo, ONE};
use piranhas_bot_v2::evaluate::evaluate;
use piranhas_bot_v2::search::SearchEngine;
use std::time::{Duration, Instant};

fn setup_midgame() -> Position {
    let mut pos = Position::default();
    // Midgame-ish position
    pos.board[11] = 1; pos.board[13] = 2; pos.board[33] = 1;
    pos.board[55] = 2; pos.board[24] = 4; pos.board[44] = 5;
    pos.board[64] = 4; pos.board[75] = 5;
    pos.recompute_caches();
    pos
}

fn bench_eval(n: usize) -> f64 {
    let pos = setup_midgame();
    let t0 = Instant::now();
    for _ in 0..n {
        let _ = evaluate(&pos, ONE, 5, true);
    }
    t0.elapsed().as_secs_f64() * 1000.0
}

fn bench_movegen(n: usize) -> f64 {
    let pos = setup_midgame();
    let t0 = Instant::now();
    for _ in 0..n {
        let mut ml = MoveList::new();
        pos.generate_moves(&mut ml);
    }
    t0.elapsed().as_secs_f64() * 1000.0
}

fn bench_make_unmake(n: usize) -> f64 {
    let mut pos = setup_midgame();
    let mut ml = MoveList::new();
    pos.generate_moves(&mut ml);
    let mv = ml.moves[0];
    let t0 = Instant::now();
    for _ in 0..n {
        let mut undo = Undo::default();
        pos.make_move(mv, &mut undo);
        pos.unmake_move(&undo);
    }
    t0.elapsed().as_secs_f64() * 1000.0
}

fn bench_search(depth_ms: u64) -> (i32, u64, u64) {
    let mut pos = setup_midgame();
    let deadline = Instant::now() + Duration::from_millis(depth_ms);
    let mut engine = SearchEngine::new();
    let t0 = Instant::now();
    let result = engine.search(&mut pos, deadline);
    let elapsed_ms = (t0.elapsed().as_secs_f64() * 1000.0) as u64;
    let nodes = result.depths.iter().map(|d| d.delta_nodes).sum::<u64>();
    let depth = result.depths.last().map(|d| d.depth).unwrap_or(0);
    (depth, nodes, elapsed_ms)
}

fn main() {
    println!("=== Piranhas Bot Benchmarks ===\n");

    let eval_n = 100_000;
    let eval_ms = bench_eval(eval_n);
    println!(
        "EVAL:    {:>10} calls in {:>7.2} ms | {:.1} ns/call",
        eval_n,
        eval_ms,
        eval_ms * 1_000_000.0 / eval_n as f64
    );

    let mg_n = 1_000_000;
    let mg_ms = bench_movegen(mg_n);
    let mg_per_s = mg_n as f64 / (mg_ms / 1000.0);
    println!(
        "MOVEGEN: {:>10} calls in {:>7.2} ms | {:.1} M calls/s",
        mg_n,
        mg_ms,
        mg_per_s / 1_000_000.0
    );

    let mu_n = 1_000_000;
    let mu_ms = bench_make_unmake(mu_n);
    let mu_per_s = mu_n as f64 / (mu_ms / 1000.0);
    println!(
        "MAKE+UNMAKE: {:>6} calls in {:>7.2} ms | {:.1} M calls/s",
        mu_n,
        mu_ms,
        mu_per_s / 1_000_000.0
    );

    println!();
    for &budget_ms in &[500, 1000, 2000] {
        let (depth, nodes, elapsed) = bench_search(budget_ms);
        let nps = if elapsed > 0 { nodes * 1000 / elapsed } else { 0 };
        println!(
            "SEARCH ({:>4}ms): depth={:>2} | {:>10} nodes | {:>6} ms | {:>8} nps",
            budget_ms, depth, nodes, elapsed, nps
        );
    }
}
