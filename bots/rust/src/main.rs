mod board;
mod evaluate;
mod search;
mod tt;

use std::time::{Duration, Instant};

use chrono::Local;
use socha::error::ComError;
use socha::internal::{ComMessage, GameResult, GameState, RoomMessage};
use socha::neutral::{Direction, Move as SochaMove, Team};
use socha::socha_com::ComHandler;

use board::{Move, Position};
use search::SearchEngine;

// ─── Helpers ──────────────────────────────────────────────────────────────────

fn ts() -> String {
    Local::now().format("%Y-%m-%d %H:%M:%S%.3f").to_string()
        .replacen('.', ",", 1)  // "15:27:02.734" → "15:27:02,734"
}

fn info(msg: &str) {
    eprintln!("{}: INFO - {}", ts(), msg);
}

fn dir_symbol(dir: &Direction) -> &'static str {
    match dir {
        Direction::UP        => "UP ↑",
        Direction::UpRight   => "UpRight ↗",
        Direction::Right     => "Right →",
        Direction::DownRight => "DownRight ↘",
        Direction::Down      => "Down ↓",
        Direction::DownLeft  => "DownLeft ↙",
        Direction::Left      => "Left ←",
        Direction::UpLeft    => "UpLeft ↖",
    }
}

fn team_str(t: Team) -> &'static str {
    match t { Team::One => "Team One (int: 1)", Team::Two => "Team Two (int: 2)" }
}

// ─── Logic ────────────────────────────────────────────────────────────────────

struct Logic {
    game_state: GameState,
    our_team: Option<Team>,
    engine: SearchEngine,
    move_number: u32,
}

impl Logic {
    fn new() -> Self {
        Logic {
            game_state: GameState::default(),
            our_team: None,
            engine: SearchEngine::new(),
            move_number: 0,
        }
    }

    fn on_gamestate_update(&mut self, state: GameState) {
        self.game_state = state;
    }

    fn calculate_move(&mut self) -> SochaMove {
        let t0 = Instant::now();
        self.move_number += 1;

        // Detect our team once
        if self.our_team.is_none() {
            let team = self.game_state.current_team();
            self.our_team = Some(team);
            eprintln!("Team: {}", team_str(team));
        }

        let mut pos = Position::from_game_state(&self.game_state);

        eprintln!("\n=== Zug {} ===", self.move_number);

        let deadline = Instant::now() + Duration::from_millis(1700);
        let result = self.engine.search(&mut pos, deadline);

        eprintln!("Rust Search: {} moves, team={}", result.num_moves, pos.player);
        for d in &result.depths {
            eprintln!(
                "d{}: {} | {}n {}h {}nps {:.2}s",
                d.depth, d.score, d.delta_nodes, d.delta_tt_hits, d.nps, d.elapsed_s
            );
        }

        let internal_mv = result.best_move.unwrap_or_else(|| {
            let mut ml = board::MoveList::new();
            pos.generate_moves(&mut ml);
            if ml.len > 0 { ml.moves[0] } else { Move::default() }
        });

        let socha_mv = pos.to_socha_move(internal_mv);
        let elapsed = t0.elapsed().as_secs_f64();

        eprintln!("-> ({}, {}) ({})", socha_mv.from.0, socha_mv.from.1, dir_symbol(&socha_mv.dir));
        info(&format!(
            "Calculated Move von ({}, {}) in Richtung ({}) after {:.3} seconds.",
            socha_mv.from.0, socha_mv.from.1, dir_symbol(&socha_mv.dir), elapsed
        ));

        socha_mv
    }

    fn on_game_result(&mut self, res: &GameResult) {
        info(&format!("Spiel beendet: {:#?}", res));
        std::process::exit(0);
    }

    fn on_welcome_message(&mut self) {
        info("Joining game");
    }

    fn on_game_joined(&mut self, room_id: &str) {
        info(&format!("Room joined: {}", room_id));
    }
}

// ─── Main ─────────────────────────────────────────────────────────────────────

fn main() -> Result<(), ComError> {
    // Suppress the socha internal logger (we print our own output)
    simple_logging::log_to_stderr(log::LevelFilter::Off);

    let args: Vec<String> = std::env::args().collect();
    let mut host = "localhost".to_string();
    let mut port = 13050u16;
    let mut reservation: Option<String> = None;

    let mut i = 1;
    while i < args.len() {
        match args[i].as_str() {
            "-h" | "--host" => { i += 1; if let Some(v) = args.get(i) { host = v.clone(); } }
            "-p" | "--port" => { i += 1; if let Some(v) = args.get(i) { if let Ok(n) = v.parse::<u16>() { port = n; } } }
            "-r" | "--reservation" => { i += 1; if let Some(v) = args.get(i) { reservation = Some(v.clone()); } }
            _ => {}
        }
        i += 1;
    }

    let addr = format!("{}:{}", host, port);

    eprintln!("==================================================");
    eprintln!("Rust Bot gestartet");
    eprintln!("==================================================");
    info("Starting...");
    info(&format!("Connecting to {}", addr));

    let mut logic = Logic::new();

    let mut com = ComHandler::join(&addr, reservation.as_deref())?;

    loop {
        match com.try_for_com_message()? {
            Some(com_message) => match com_message {
                ComMessage::Joined(joined) => {
                    logic.on_game_joined(&joined.room_id);
                }
                ComMessage::Left(left) => {
                    info(&format!("Left room: {}", left.room_id));
                    break;
                }
                ComMessage::Room(room_msg) => match *room_msg {
                    RoomMessage::Memento(state) => {
                        logic.on_gamestate_update(*state);
                    }
                    RoomMessage::WelcomeMessage => {
                        logic.on_welcome_message();
                    }
                    RoomMessage::MoveRequest => {
                        let request_start = Instant::now();
                        let mv = logic.calculate_move();
                        let calc_elapsed_s = request_start.elapsed().as_secs_f64();

                        let send_start = Instant::now();
                        com.send_move(mv.from.0, mv.from.1, mv.dir)?;
                        let network_elapsed_s = send_start.elapsed().as_secs_f64();
                        let total_elapsed_s = request_start.elapsed().as_secs_f64();

                        info(&format!(
                            "Sent Move von ({}, {}) in Richtung ({}) | calc {:.3}s | network {:.6}s | total {:.3}s",
                            mv.from.0,
                            mv.from.1,
                            dir_symbol(&mv.dir),
                            calc_elapsed_s,
                            network_elapsed_s,
                            total_elapsed_s
                        ));
                    }
                    RoomMessage::Result(result) => {
                        logic.on_game_result(&result);
                    }
                },
                ComMessage::Admin(_) => {}
            },
            None => std::thread::sleep(Duration::from_millis(2)),
        }
    }

    Ok(())
}
