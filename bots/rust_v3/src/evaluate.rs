use crate::bitboard::{get_neighbor_masks, pop_lsb};
use crate::board::{
    get_tables, opponent, piece_owner, MoveList, Position, ONE, TWO,
};

// ─── NNUE (embedded when src/weights.bin is present at compile time) ──────────

#[cfg(has_nnue)]
static NNUE_BYTES: &[u8] = include_bytes!("weights.bin");

#[cfg(has_nnue)]
const NNUE_L1: usize = 128;
#[cfg(has_nnue)]
const NNUE_L2: usize = 16;
#[cfg(has_nnue)]
const NNUE_IN: usize = 800;

#[cfg(has_nnue)]
struct NnueWeights {
    l1_w: Vec<f32>, // [NNUE_IN * NNUE_L1]
    l1_b: Vec<f32>, // [NNUE_L1]
    l2_w: Vec<f32>, // [NNUE_L1 * NNUE_L2]
    l2_b: Vec<f32>, // [NNUE_L2]
    out_w: Vec<f32>, // [NNUE_L2]
    out_b: f32,
}

#[cfg(has_nnue)]
fn nnue_weights() -> &'static NnueWeights {
    use std::sync::OnceLock;
    static W: OnceLock<NnueWeights> = OnceLock::new();
    W.get_or_init(|| {
        let floats: Vec<f32> = NNUE_BYTES
            .chunks_exact(4)
            .map(|b| f32::from_le_bytes([b[0], b[1], b[2], b[3]]))
            .collect();
        let mut p = 0usize;

        // Reads n floats starting at p, advances p
        macro_rules! take {
            ($n:expr) => {{
                let v = floats[p..p + $n].to_vec();
                p += $n;
                v
            }};
        }

        // PyTorch exports [out, in]; transpose to [in, out] for fast accumulation
        let l1r = take!(NNUE_L1 * NNUE_IN);
        let l1_b = take!(NNUE_L1);
        let l2r = take!(NNUE_L2 * NNUE_L1);
        let l2_b = take!(NNUE_L2);
        let out_w = take!(NNUE_L2);
        let out_b = floats[p];

        let mut l1_w = vec![0.0f32; NNUE_IN * NNUE_L1];
        for i in 0..NNUE_L1 {
            for j in 0..NNUE_IN {
                l1_w[j * NNUE_L1 + i] = l1r[i * NNUE_IN + j];
            }
        }
        let mut l2_w = vec![0.0f32; NNUE_L1 * NNUE_L2];
        for i in 0..NNUE_L2 {
            for j in 0..NNUE_L1 {
                l2_w[j * NNUE_L2 + i] = l2r[i * NNUE_L1 + j];
            }
        }

        NnueWeights { l1_w, l1_b, l2_w, l2_b, out_w, out_b }
    })
}

#[cfg(has_nnue)]
fn run_nnue(board: &[u8; 100], player: u8) -> i32 {
    let w = nnue_weights();

    // Build L1 accumulator from board features (player-relative)
    let mut acc = [0.0f32; NNUE_L1];
    acc.copy_from_slice(&w.l1_b);

    for sq in 0..100usize {
        let piece = board[sq];
        if piece == 0 { continue; }
        let feat_type: usize = match player {
            1 => match piece { 1=>1, 2=>2, 3=>3, 4=>4, 5=>5, 6=>6, 7=>7, _=>continue },
            2 => match piece { 4=>1, 5=>2, 6=>3, 1=>4, 2=>5, 3=>6, 7=>7, _=>continue },
            _ => continue,
        };
        let feat = sq * 8 + feat_type;
        let row = &w.l1_w[feat * NNUE_L1..(feat + 1) * NNUE_L1];
        for (a, &ww) in acc.iter_mut().zip(row) {
            *a += ww;
        }
    }

    // L1 → L2
    let mut l2 = [0.0f32; NNUE_L2];
    l2.copy_from_slice(&w.l2_b);
    for (n, &a) in acc.iter().enumerate() {
        let act = a.clamp(0.0, 1.0);
        if act == 0.0 { continue; }
        let row = &w.l2_w[n * NNUE_L2..(n + 1) * NNUE_L2];
        for (o, &ww) in l2.iter_mut().zip(row) {
            *o += act * ww;
        }
    }

    // L2 → output
    let mut out = w.out_b;
    for (n, &l) in l2.iter().enumerate() {
        out += l.clamp(0.0, 1.0) * w.out_w[n];
    }

    // Invert tanh normalization: label = tanh(score/3000)
    (out.clamp(-0.9999, 0.9999).atanh() * 3000.0) as i32
}

// ─── Score constants ──────────────────────────────────────────────────────────
pub const MATE_SCORE: i32 = 900_000;

// ─── Evaluation weights (hand-crafted fallback) ───────────────────────────────
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
}

const DEFAULT_WEIGHTS: EvalWeights = EvalWeights {
    w_largest: 543,
    w_components: 160,
    w_spread: 58,
    w_material: 232,
    w_links: 23,
    w_center: 6,
    w_mobility: 4,
    w_late_largest: 155,
    w_late_components: 172,
    w_late_spread: 130,
    w_late_links: 47,
    w_late_mobility: 12,
};

#[inline]
fn eval_weights() -> &'static EvalWeights {
    &DEFAULT_WEIGHTS
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

    // Tiebreaker: who formed a complete school first?
    let own_idx = if perspective == ONE { 0 } else { 1 };
    let opp_idx = if opp == ONE { 0 } else { 1 };
    match (pos.connected_since[own_idx], pos.connected_since[opp_idx]) {
        (Some(o), Some(b)) if o < b => return MATE_SCORE - ply,
        (Some(o), Some(b)) if b < o => return -MATE_SCORE + ply,
        (Some(_), None) => return MATE_SCORE - ply,
        (None, Some(_)) => return -MATE_SCORE + ply,
        _ => {}
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

// ─── Shape links + center sum (bitboard-based, hand-crafted eval only) ───────

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

pub fn has_one_move_connect(pos: &Position, player: u8, max_checks: usize) -> bool {
    let count = if player == ONE { pos.one_count } else { pos.two_count };
    if count > 8 {
        return false;
    }

    let mut moves = MoveList::new();
    pos.generate_moves_for(player, &mut moves);

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
        let mut test_pos = pos.clone();
        let mut undo = crate::board::Undo::default();

        test_pos.player = player;
        let ok = test_pos.make_move(mv, &mut undo);
        if !ok {
            continue;
        }

        let connected = test_pos.is_connected(player);

        if connected {
            return true;
        }
    }

    false
}

// ─── Shared terminal/early-exit checks ───────────────────────────────────────

fn eval_terminals(pos: &Position, perspective: u8) -> Option<i32> {
    let opp = opponent(perspective);

    if pos.turn >= 60 {
        let red_swarm = pos.largest_component_value(ONE);
        let blue_swarm = pos.largest_component_value(TWO);

        if red_swarm > blue_swarm {
            return Some(if perspective == ONE {
                MATE_SCORE - pos.turn as i32
            } else {
                -MATE_SCORE + pos.turn as i32
            });
        }
        if blue_swarm > red_swarm {
            return Some(if perspective == TWO {
                MATE_SCORE - pos.turn as i32
            } else {
                -MATE_SCORE + pos.turn as i32
            });
        }

        match (pos.connected_since[0], pos.connected_since[1]) {
            (Some(r), Some(b)) if r < b => {
                return Some(if perspective == ONE { MATE_SCORE - pos.turn as i32 } else { -MATE_SCORE + pos.turn as i32 });
            }
            (Some(r), Some(b)) if b < r => {
                return Some(if perspective == TWO { MATE_SCORE - pos.turn as i32 } else { -MATE_SCORE + pos.turn as i32 });
            }
            (Some(_), None) => {
                return Some(if perspective == ONE { MATE_SCORE - pos.turn as i32 } else { -MATE_SCORE + pos.turn as i32 });
            }
            (None, Some(_)) => {
                return Some(if perspective == TWO { MATE_SCORE - pos.turn as i32 } else { -MATE_SCORE + pos.turn as i32 });
            }
            _ => {}
        }

        if pos.total_piece_value(ONE) > pos.total_piece_value(TWO) {
            return Some(if perspective == ONE {
                MATE_SCORE - pos.turn as i32
            } else {
                -MATE_SCORE + pos.turn as i32
            });
        }
        if pos.total_piece_value(TWO) > pos.total_piece_value(ONE) {
            return Some(if perspective == TWO {
                MATE_SCORE - pos.turn as i32
            } else {
                -MATE_SCORE + pos.turn as i32
            });
        }
        return Some(0);
    }

    let own_count = if perspective == ONE { pos.one_count } else { pos.two_count };
    let opp_count = if perspective == ONE { pos.two_count } else { pos.one_count };

    if own_count == 0 && opp_count > 0 {
        return Some(-MATE_SCORE + pos.turn as i32);
    }
    if opp_count == 0 && own_count > 0 {
        return Some(MATE_SCORE - pos.turn as i32);
    }

    let own_total = pos.total_piece_value(perspective);
    let opp_total = pos.total_piece_value(opp);
    let own_largest = pos.largest_component_value(perspective);
    let opp_largest = pos.largest_component_value(opp);

    if own_total > 0 && own_largest == own_total {
        return Some(MATE_SCORE - pos.turn as i32);
    }
    if opp_total > 0 && opp_largest == opp_total {
        return Some(-MATE_SCORE + pos.turn as i32);
    }

    None
}

// ─── Hand-crafted evaluation ──────────────────────────────────────────────────

fn hand_crafted_eval(pos: &Position, perspective: u8, depth_hint: i32) -> i32 {
    let opp = opponent(perspective);

    let own_count = if perspective == ONE { pos.one_count } else { pos.two_count };
    let opp_count = if perspective == ONE { pos.two_count } else { pos.one_count };
    let own_total = pos.total_piece_value(perspective);
    let opp_total = pos.total_piece_value(opp);
    let own_largest = pos.largest_component_value(perspective);
    let opp_largest = pos.largest_component_value(opp);

    let own_components = pos.component_count(perspective);
    let opp_components = pos.component_count(opp);
    let own_spread = pos.component_spread(perspective);
    let opp_spread = pos.component_spread(opp);

    let mut own_center = 0i32;
    let mut opp_center = 0i32;
    let own_links = shape_links(pos, perspective, &mut own_center);
    let opp_links = shape_links(pos, opp, &mut opp_center);

    let total_pieces = (own_count + opp_count) as i32;
    let piece_phase = (((16 - total_pieces) * 256) / 12).clamp(0, 256);
    let turn_phase  = (((pos.turn as i32 - 20) * 256) / 40).clamp(0, 256);
    let eg_256 = piece_phase.max(turn_phase);

    let allow_expensive = depth_hint <= 3;
    let need_mobility = allow_expensive
        && (eg_256 > 80 || own_components <= 3 || opp_components <= 3);

    let mut own_mobility = 0i32;
    let mut opp_mobility = 0i32;
    if need_mobility {
        let mut tmp = MoveList::new();
        pos.generate_moves_for(perspective, &mut tmp);
        own_mobility = tmp.len as i32;
        pos.generate_moves_for(opp, &mut tmp);
        opp_mobility = tmp.len as i32;
    }

    let w = eval_weights();
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
    score += (w.w_late_largest    * (own_largest  - opp_largest)    * eg_256) / 256;
    score += (w.w_late_components * (opp_components - own_components) * eg_256) / 256;
    score += (w.w_late_spread     * (opp_spread   - own_spread)     * eg_256) / 256;
    score += (w.w_late_links      * (own_links    - opp_links)      * eg_256) / 256;
    if need_mobility {
        score += (w.w_late_mobility * (own_mobility - opp_mobility) * eg_256) / 256;
    }
    score
}

// ─── Main evaluation ─────────────────────────────────────────────────────────

pub fn evaluate(pos: &Position, perspective: u8, depth_hint: i32, use_nnue: bool) -> i32 {
    if let Some(s) = eval_terminals(pos, perspective) {
        return s;
    }

    #[cfg(has_nnue)]
    if use_nnue {
        return run_nnue(&pos.board, perspective);
    }

    hand_crafted_eval(pos, perspective, depth_hint)
}
