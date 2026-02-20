mod board;
mod evaluate;
mod search;
mod tt;

use std::ffi::c_int;
use std::time::{Duration, Instant};

use board::{Move, MoveList, Position, ONE, TWO};
use search::SearchEngine;

const DEFAULT_TIME_BUDGET_MS: u32 = 1700;

pub struct Engine {
    search: SearchEngine,
}

fn print_search_debug(turn: u16, team: u8, num_moves: usize, depths: &[search::DepthInfo]) {
    eprintln!("\n=== Zug {} ===", turn);
    eprintln!("Cython Search: {} moves, team={}", num_moves, team);
    for d in depths {
        eprintln!(
            "d{}: {} | {}n {}h {}nps {:.2}s",
            d.depth, d.score, d.delta_nodes, d.delta_tt_hits, d.nps, d.elapsed_s
        );
    }
}

impl Engine {
    fn new() -> Self {
        Self {
            search: SearchEngine::new(),
        }
    }

    fn choose_move(
        &mut self,
        board: [u8; 100],
        current_player: u8,
        turn: u16,
        time_ms: u32,
    ) -> Option<Move> {
        let player = if current_player == TWO { TWO } else { ONE };
        let mut pos = Position::from_encoded(board, player, turn);

        let budget_ms = if time_ms == 0 {
            DEFAULT_TIME_BUDGET_MS
        } else {
            time_ms
        };
        let deadline = Instant::now() + Duration::from_millis(budget_ms as u64);
        let result = self.search.search(&mut pos, deadline);
        print_search_debug(turn, player, result.num_moves, &result.depths);

        if let Some(best) = result.best_move {
            return Some(best);
        }

        let mut moves = MoveList::new();
        pos.generate_moves(&mut moves);
        if moves.len > 0 {
            Some(moves.moves[0])
        } else {
            None
        }
    }
}

#[no_mangle]
pub extern "C" fn engine_new() -> *mut Engine {
    Box::into_raw(Box::new(Engine::new()))
}

#[no_mangle]
pub unsafe extern "C" fn engine_free(ptr: *mut Engine) {
    if !ptr.is_null() {
        let _ = Box::from_raw(ptr);
    }
}

#[no_mangle]
pub unsafe extern "C" fn engine_choose_move(
    ptr: *mut Engine,
    board_ptr: *const u8,
    current_player: u8,
    turn: u16,
    time_ms: u32,
    out_from: *mut u8,
    out_to: *mut u8,
) -> c_int {
    if ptr.is_null() || board_ptr.is_null() || out_from.is_null() || out_to.is_null() {
        return 0;
    }

    let board_slice = std::slice::from_raw_parts(board_ptr, 100);
    let mut board = [0u8; 100];
    for (i, &v) in board_slice.iter().enumerate() {
        board[i] = if v <= 7 { v } else { 0 };
    }

    let engine = &mut *ptr;
    if let Some(mv) = engine.choose_move(board, current_player, turn, time_ms) {
        *out_from = mv.from;
        *out_to = mv.to;
        1
    } else {
        0
    }
}
