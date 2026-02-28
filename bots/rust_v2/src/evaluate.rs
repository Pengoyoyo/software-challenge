use crate::bitboard::{get_neighbor_masks, pop_lsb};
use crate::board::{
    get_tables, opponent, piece_owner, MoveList, Position, ONE, TWO,
};
use std::sync::OnceLock;

// ─── Score constants ──────────────────────────────────────────────────────────
pub const WIN_SCORE: i32 = 1_000_000;
pub const MATE_SCORE: i32 = 900_000;

// ─── Evaluation weights ───────────────────────────────────────────────────────
#[derive(Clone, Copy)]
struct EvalWeights {
    w_largest: i32,
    w_components: i32,
    w_spread: i32,
    w_material: i32,
    w_links: i32,
    w_center: i32,
    w_mobility: i32,
    w_late_largest: i32,
    w_late_components: i32,
    w_late_spread: i32,
    w_late_links: i32,
    w_late_mobility: i32,
    connect_bonus: i32,
}

const DEFAULT_WEIGHTS: EvalWeights = EvalWeights {
    // Deviations from C++ are intentional; see plan for rationale.
    w_largest: 380,
    w_components: 260,
    w_spread: 50,
    w_material: 130,
    w_links: 15,
    w_center: 4,
    w_mobility: 7,
    w_late_largest: 180,
    w_late_components: 130,
    w_late_spread: 90,
    w_late_links: 20,
    w_late_mobility: 12,
    connect_bonus: 85_000,
};

static EVAL_WEIGHTS: OnceLock<EvalWeights> = OnceLock::new();

fn parse_weights_list(raw: &str) -> Option<EvalWeights> {
    let parts: Vec<&str> = raw
        .split(',')
        .map(|s| s.trim())
        .filter(|s| !s.is_empty())
        .collect();
    if parts.len() != 13 {
        return None;
    }

    let mut vals = [0i32; 13];
    for (i, p) in parts.iter().enumerate() {
        let v = p.parse::<f64>().ok()?;
        if !v.is_finite() {
            return None;
        }
        vals[i] = v.round() as i32;
    }

    Some(EvalWeights {
        w_largest: vals[0],
        w_components: vals[1],
        w_spread: vals[2],
        w_material: vals[3],
        w_links: vals[4],
        w_center: vals[5],
        w_mobility: vals[6],
        w_late_largest: vals[7],
        w_late_components: vals[8],
        w_late_spread: vals[9],
        w_late_links: vals[10],
        w_late_mobility: vals[11],
        connect_bonus: vals[12],
    })
}

fn load_eval_weights() -> EvalWeights {
    for key in ["PIRANHAS_RSV2_EVAL_WEIGHTS", "PIRANHAS_RS_EVAL_WEIGHTS", "RUST_EVAL_WEIGHTS"] {
        if let Ok(raw) = std::env::var(key) {
            if let Some(parsed) = parse_weights_list(&raw) {
                return parsed;
            }
        }
    }
    DEFAULT_WEIGHTS
}

#[inline]
fn eval_weights() -> &'static EvalWeights {
    EVAL_WEIGHTS.get_or_init(load_eval_weights)
}

// ─── Terminal score at turn >= 60 ─────────────────────────────────────────────

pub fn terminal_swarm_score(pos: &Position, perspective: u8, ply: i32) -> i32 {
    let opp = opponent(perspective);

    let own_swarm = pos.largest_component_value(perspective);
    let opp_swarm = pos.largest_component_value(opp);

    if own_swarm > opp_swarm {
        return MATE_SCORE - ply;
    }
    if opp_swarm > own_swarm {
        return -MATE_SCORE + ply;
    }

    let own_val = pos.total_piece_value(perspective);
    let opp_val = pos.total_piece_value(opp);

    if own_val > opp_val {
        return MATE_SCORE - ply;
    }
    if opp_val > own_val {
        return -MATE_SCORE + ply;
    }

    let own_count = if perspective == ONE { pos.one_count } else { pos.two_count };
    let opp_count = if perspective == TWO { pos.one_count } else { pos.two_count };

    if own_count > opp_count {
        return MATE_SCORE - ply;
    }
    if opp_count > own_count {
        return -MATE_SCORE + ply;
    }

    0
}

// ─── Shape links + center sum (bitboard-based) ────────────────────────────────

fn shape_links(pos: &Position, player: u8, center_sum: &mut i32) -> i32 {
    let t = get_tables();
    let nb = get_neighbor_masks();
    let pieces_bb = if player == ONE { pos.bb_one } else { pos.bb_two };
    *center_sum = 0;
    let mut links = 0i32;

    let mut bits = pieces_bb;
    while bits != 0 {
        let sq = pop_lsb(&mut bits);
        *center_sum += t.center[sq] as i32;
        // Count neighbors that are own pieces with higher index (avoid double-counting edges)
        let higher_own = nb[sq] & pieces_bb & !((1u128 << (sq + 1)) - 1);
        links += higher_own.count_ones() as i32;
    }

    links
}

// ─── One-move connect check ───────────────────────────────────────────────────

pub fn has_one_move_connect(pos: &mut Position, player: u8, max_checks: usize) -> bool {
    let count = if player == ONE { pos.one_count } else { pos.two_count };
    if count > 8 {
        return false;
    }

    let mut moves = MoveList::new();
    // Temporarily set player to generate moves for them
    let saved_player = pos.player;
    pos.player = player;
    pos.generate_moves(&mut moves);
    pos.player = saved_player;

    if moves.len == 0 {
        return false;
    }

    let opp = opponent(player);
    let t = get_tables();

    // Sort: captures first, then by center score
    let mv_slice = &mut moves.moves[..moves.len];
    mv_slice.sort_unstable_by(|a, b| {
        let ac = (piece_owner(pos.board[a.to as usize]) == opp) as i32;
        let bc = (piece_owner(pos.board[b.to as usize]) == opp) as i32;
        if ac != bc {
            return bc.cmp(&ac); // higher capture priority first
        }
        let ca = t.center[a.to as usize] as i32;
        let cb = t.center[b.to as usize] as i32;
        cb.cmp(&ca) // higher center first
    });

    let checks = max_checks.min(moves.len);
    for i in 0..checks {
        let mv = moves.moves[i];
        let mut undo = crate::board::Undo::default();

        pos.player = player;
        let ok = pos.make_move(mv, &mut undo);
        if !ok {
            pos.player = saved_player;
            continue;
        }

        let connected = pos.is_connected(player);
        pos.unmake_move(&undo);
        pos.player = saved_player;

        if connected {
            return true;
        }
    }

    false
}

// ─── Main evaluation ─────────────────────────────────────────────────────────

pub fn evaluate(pos: &mut Position, perspective: u8, depth_hint: i32) -> i32 {
    let w = eval_weights();
    let opp = opponent(perspective);

    // Terminal: game over at turn 60
    if pos.turn >= 60 {
        let red_swarm = pos.largest_component_value(ONE);
        let blue_swarm = pos.largest_component_value(TWO);

        if red_swarm > blue_swarm {
            return if perspective == ONE {
                WIN_SCORE - pos.turn as i32
            } else {
                -WIN_SCORE + pos.turn as i32
            };
        }
        if blue_swarm > red_swarm {
            return if perspective == TWO {
                WIN_SCORE - pos.turn as i32
            } else {
                -WIN_SCORE + pos.turn as i32
            };
        }
        if pos.total_piece_value(ONE) > pos.total_piece_value(TWO) {
            return if perspective == ONE {
                WIN_SCORE - pos.turn as i32
            } else {
                -WIN_SCORE + pos.turn as i32
            };
        }
        if pos.total_piece_value(TWO) > pos.total_piece_value(ONE) {
            return if perspective == TWO {
                WIN_SCORE - pos.turn as i32
            } else {
                -WIN_SCORE + pos.turn as i32
            };
        }
        return 0;
    }

    let own_count = if perspective == ONE { pos.one_count } else { pos.two_count };
    let opp_count = if perspective == ONE { pos.two_count } else { pos.one_count };

    // One side eliminated
    if own_count == 0 && opp_count > 0 {
        return -WIN_SCORE + pos.turn as i32;
    }
    if opp_count == 0 && own_count > 0 {
        return WIN_SCORE - pos.turn as i32;
    }

    let own_total = pos.total_piece_value(perspective);
    let opp_total = pos.total_piece_value(opp);
    let own_largest = pos.largest_component_value(perspective);
    let opp_largest = pos.largest_component_value(opp);

    // Win if already connected
    if own_total > 0 && own_largest == own_total {
        return WIN_SCORE - pos.turn as i32;
    }
    if opp_total > 0 && opp_largest == opp_total {
        return -WIN_SCORE + pos.turn as i32;
    }

    let own_components = pos.component_count(perspective);
    let opp_components = pos.component_count(opp);
    let own_spread = pos.component_spread(perspective);
    let opp_spread = pos.component_spread(opp);

    let mut own_center = 0i32;
    let mut opp_center = 0i32;
    let own_links = shape_links(pos, perspective, &mut own_center);
    let opp_links = shape_links(pos, opp, &mut opp_center);

    // ── Tapered phase (fixed-point, 0=opening/256=endgame) ────────────────────
    // Phase increases as pieces are captured and as the turn count rises.
    let total_pieces = (own_count + opp_count) as i32;
    let piece_phase = (((16 - total_pieces) * 256) / 12).clamp(0, 256); // 0 at 16 pieces, 256 at ≤4
    let turn_phase  = (((pos.turn as i32 - 20) * 256) / 40).clamp(0, 256); // 0 before turn 20, 256 at turn 60
    let eg_256 = piece_phase.max(turn_phase); // endgame factor 0–256

    let allow_expensive = depth_hint <= 3;
    let need_mobility = allow_expensive
        && (eg_256 > 80 || own_components <= 3 || opp_components <= 3);

    let mut own_mobility = 0i32;
    let mut opp_mobility = 0i32;
    if need_mobility {
        let saved = pos.player;
        let mut tmp = MoveList::new();

        pos.player = perspective;
        pos.generate_moves(&mut tmp);
        own_mobility = tmp.len as i32;

        pos.player = opp;
        pos.generate_moves(&mut tmp);
        opp_mobility = tmp.len as i32;

        pos.player = saved;
    }

    // Base score (always applied)
    let mut score = 0i32;
    score += w.w_largest    * (own_largest  - opp_largest);
    score += w.w_components * (opp_components - own_components);
    score += w.w_spread     * (opp_spread   - own_spread);
    score += w.w_material   * (own_total    - opp_total);
    score += w.w_links      * (own_links    - opp_links);
    score += w.w_center     * (own_center   - opp_center);
    if need_mobility {
        score += w.w_mobility * (own_mobility - opp_mobility);
    }

    // Endgame bonus (tapered: scaled by eg_256/256)
    score += (w.w_late_largest    * (own_largest  - opp_largest)    * eg_256) / 256;
    score += (w.w_late_components * (opp_components - own_components) * eg_256) / 256;
    score += (w.w_late_spread     * (opp_spread   - own_spread)     * eg_256) / 256;
    score += (w.w_late_links      * (own_links    - opp_links)      * eg_256) / 256;
    if need_mobility {
        score += (w.w_late_mobility * (own_mobility - opp_mobility) * eg_256) / 256;
    }

    // One-move connect bonus (only in endgame-ish positions)
    if allow_expensive && eg_256 > 100 {
        if own_components <= 2 && own_count <= 8 {
            if has_one_move_connect(pos, perspective, 6) {
                score += w.connect_bonus;
            }
        }
        if opp_components <= 2 && opp_count <= 8 {
            if has_one_move_connect(pos, opp, 6) {
                score -= w.connect_bonus;
            }
        }
    }

    score
}
