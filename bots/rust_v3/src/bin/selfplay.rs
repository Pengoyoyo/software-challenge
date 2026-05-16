use piranhas_bot_v2::board::{Position, Undo, ONE, TWO};
use piranhas_bot_v2::search::SearchEngine;
use std::time::{Duration, Instant};

struct GameResult {
    winner: Option<u8>, // ONE, TWO, or None for draw
    red_swarm: i32,
    blue_swarm: i32,
    turns: u16,
    red_time_ms: f64,
    blue_time_ms: f64,
}

fn play_one_game(red_time_ms: u64, blue_time_ms: u64, verbose: bool) -> GameResult {
    let mut pos = Position::default();

    // Simple starting position: 4x1 and 4x2 fish spread on the board
    // Row 0: Red (ONE), Row 9: Blue (TWO)
    // (Not a legal SC start but fine for self-play testing)
    for col in [1, 3, 5, 7] {
        pos.board[col] = 1;        // Red size-1
    }
    for col in [1, 3, 5, 7] {
        pos.board[90 + col] = 4;   // Blue size-1
    }
    pos.recompute_caches();

    let mut red_engine = SearchEngine::new();
    let mut blue_engine = SearchEngine::new();

    let mut red_total_time = 0.0f64;
    let mut blue_total_time = 0.0f64;

    while pos.turn < 60 {
        let current = pos.player;
        let is_red = current == ONE;
        let time_budget = if is_red { red_time_ms } else { blue_time_ms };
        let deadline = Instant::now() + Duration::from_millis(time_budget);
        let engine = if is_red { &mut red_engine } else { &mut blue_engine };

        let t0 = Instant::now();
        let result = engine.search(&mut pos, deadline);
        let elapsed = t0.elapsed().as_secs_f64() * 1000.0;

        if is_red {
            red_total_time += elapsed;
        } else {
            blue_total_time += elapsed;
        }

        let mv = match result.best_move {
            Some(m) => m,
            None => {
                // No legal moves — game ends
                break;
            }
        };

        let mut undo = Undo::default();
        if !pos.make_move(mv, &mut undo) {
            break;
        }

        if verbose {
            println!(
                "Turn {}: {} from ({},{}) to ({},{}) | depth={} score={} | t={:.1}ms",
                pos.turn,
                if is_red { "R" } else { "B" },
                mv.from % 10,
                mv.from / 10,
                mv.to % 10,
                mv.to / 10,
                result.depths.last().map(|d| d.depth).unwrap_or(0),
                result.depths.last().map(|d| d.score).unwrap_or(0),
                elapsed
            );
        }

        // Early termination if connected
        if pos.is_connected(ONE) || pos.is_connected(TWO) {
            if verbose { println!("Connected school formed at turn {}", pos.turn); }
        }
    }

    // Final scoring
    let red_swarm = pos.largest_component_value(ONE);
    let blue_swarm = pos.largest_component_value(TWO);

    let winner = if red_swarm > blue_swarm {
        Some(ONE)
    } else if blue_swarm > red_swarm {
        Some(TWO)
    } else {
        // Tiebreaker: who formed a complete swarm first
        match (pos.connected_since[0], pos.connected_since[1]) {
            (Some(r), Some(b)) if r < b => Some(ONE),
            (Some(r), Some(b)) if b < r => Some(TWO),
            _ => None,
        }
    };

    GameResult {
        winner,
        red_swarm,
        blue_swarm,
        turns: pos.turn,
        red_time_ms: red_total_time,
        blue_time_ms: blue_total_time,
    }
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let n_games = args.get(1).and_then(|s| s.parse::<usize>().ok()).unwrap_or(10);
    let red_time = args.get(2).and_then(|s| s.parse::<u64>().ok()).unwrap_or(1000);
    let blue_time = args.get(3).and_then(|s| s.parse::<u64>().ok()).unwrap_or(1000);
    let verbose = args.iter().any(|s| s == "-v");

    println!("=== Self-Play: {} games ===", n_games);
    println!("Red time: {}ms, Blue time: {}ms", red_time, blue_time);
    println!();

    let mut red_wins = 0usize;
    let mut blue_wins = 0usize;
    let mut draws = 0usize;
    let mut total_red_swarm = 0i32;
    let mut total_blue_swarm = 0i32;
    let mut total_turns = 0u64;

    for i in 0..n_games {
        let result = play_one_game(red_time, blue_time, verbose);

        match result.winner {
            Some(ONE) => red_wins += 1,
            Some(TWO) => blue_wins += 1,
            _ => draws += 1,
        }

        total_red_swarm += result.red_swarm;
        total_blue_swarm += result.blue_swarm;
        total_turns += result.turns as u64;

        println!(
            "Game {:>3}: Winner={:>4} | Red swarm={:>3} Blue swarm={:>3} | Turns={:>2} | Red={:.0}ms Blue={:.0}ms",
            i + 1,
            match result.winner {
                Some(ONE) => "RED",
                Some(TWO) => "BLUE",
                _ => "DRAW",
            },
            result.red_swarm,
            result.blue_swarm,
            result.turns,
            result.red_time_ms,
            result.blue_time_ms,
        );
    }

    println!();
    println!("=== Results ===");
    println!("Red wins:  {} ({:.1}%)", red_wins, red_wins as f64 * 100.0 / n_games as f64);
    println!("Blue wins: {} ({:.1}%)", blue_wins, blue_wins as f64 * 100.0 / n_games as f64);
    println!("Draws:     {} ({:.1}%)", draws, draws as f64 * 100.0 / n_games as f64);
    println!(
        "Avg swarm: Red={:.1} Blue={:.1}",
        total_red_swarm as f64 / n_games as f64,
        total_blue_swarm as f64 / n_games as f64
    );
    println!("Avg turns: {:.1}", total_turns as f64 / n_games as f64);
}
