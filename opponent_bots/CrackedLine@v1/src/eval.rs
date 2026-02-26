use std::cmp::{max, min};
use std::sync::{OnceLock, RwLock};

use crate::movegen::{generate_capture_moves, generate_moves, has_legal_move};
use crate::state::{
    init_tables, neighbors_count_table, neighbors_table, opponent, piece_owner, Position, Undo,
    BLUE, MATE_SCORE, MAX_NEIGHBORS, NUM_SQUARES, RED, WIN_SCORE,
};

#[derive(Clone, Copy, Debug)]
pub struct EvalWeights {
    pub w_largest: i32,
    pub w_components: i32,
    pub w_spread: i32,
    pub w_count: i32,
    pub w_links: i32,
    pub w_center: i32,
    pub w_mobility: i32,
    pub w_mobility_targets: i32,

    pub w_late_largest: i32,
    pub w_late_components: i32,
    pub w_late_spread: i32,
    pub w_late_links: i32,
    pub w_late_mobility: i32,
    pub w_bridge_risk: i32,
    pub w_bridge_redundancy: i32,
    pub w_threat_in1: i32,
    pub w_threat_in2: i32,
    pub w_safe_capture: i32,
    pub w_no_move_pressure: i32,
    pub w_late_swarm_cohesion: i32,
    pub w_late_fragment_pressure: i32,
    pub w_late_disconnect_pressure: i32,
    pub w_race_connect1: i32,
    pub w_race_connect2: i32,
    pub w_race_disconnect1: i32,
    pub w_race_disconnect2: i32,
    pub w_race_side_to_move: i32,
    pub w_cut_pressure: i32,
    pub w_collapse_risk: i32,
    pub w_articulation_pressure: i32,
    pub w_round_end_tempo: i32,

    pub connect_bonus: i32,
}

impl Default for EvalWeights {
    fn default() -> Self {
        Self {
            w_largest: 340,
            w_components: 230,
            w_spread: 65,
            w_count: 115,
            w_links: 12,
            w_center: 6,
            w_mobility: 5,
            w_mobility_targets: 4,
            w_late_largest: 150,
            w_late_components: 110,
            w_late_spread: 80,
            w_late_links: 16,
            w_late_mobility: 10,
            w_bridge_risk: 32,
            w_bridge_redundancy: 20,
            w_threat_in1: 18_000,
            w_threat_in2: 9_500,
            w_safe_capture: 22,
            w_no_move_pressure: 2500,
            w_late_swarm_cohesion: 120,
            w_late_fragment_pressure: 48,
            w_late_disconnect_pressure: 7000,
            w_race_connect1: 12_000,
            w_race_connect2: 5200,
            w_race_disconnect1: 8600,
            w_race_disconnect2: 3600,
            w_race_side_to_move: 4000,
            w_cut_pressure: 24,
            w_collapse_risk: 180,
            w_articulation_pressure: 28,
            w_round_end_tempo: 2600,
            connect_bonus: 70_000,
        }
    }
}

fn center_scores() -> [i32; NUM_SQUARES] {
    let mut out = [0_i32; NUM_SQUARES];
    let mut square = 0_usize;
    while square < NUM_SQUARES {
        let x = (square % 10) as i32;
        let y = (square / 10) as i32;
        let dx2 = (2 * x - 9).abs();
        let dy2 = (2 * y - 9).abs();
        out[square] = 18 - max(dx2, dy2);
        square += 1;
    }
    out
}

static CENTER: OnceLock<[i32; NUM_SQUARES]> = OnceLock::new();
static WEIGHTS: OnceLock<RwLock<EvalWeights>> = OnceLock::new();

fn center() -> &'static [i32; NUM_SQUARES] {
    CENTER.get_or_init(center_scores)
}

fn weights_lock() -> &'static RwLock<EvalWeights> {
    WEIGHTS.get_or_init(|| RwLock::new(EvalWeights::default()))
}

pub fn set_eval_weights(weights: EvalWeights) {
    let mut guard = weights_lock().write().expect("eval weights poisoned");
    *guard = weights;
}

pub fn get_eval_weights() -> EvalWeights {
    *weights_lock().read().expect("eval weights poisoned")
}

pub const ROUND_CONNECT_NONE: u8 = 0;
pub const ROUND_CONNECT_BOTH: u8 = 3;

#[inline]
fn is_round_end_state(pos: &Position) -> bool {
    pos.player_to_move == RED && pos.turn > 0
}

pub fn round_end_connection_outcome(pos: &Position) -> u8 {
    if !is_round_end_state(pos) {
        return ROUND_CONNECT_NONE;
    }

    let red_connected = pos.is_connected(RED);
    let blue_connected = pos.is_connected(BLUE);

    match (red_connected, blue_connected) {
        (false, false) => ROUND_CONNECT_NONE,
        (true, false) => RED,
        (false, true) => BLUE,
        (true, true) => ROUND_CONNECT_BOTH,
    }
}

#[inline]
fn swarm_winner(pos: &Position) -> u8 {
    let red_swarm = pos.largest_component_value(RED);
    let blue_swarm = pos.largest_component_value(BLUE);

    if red_swarm > blue_swarm {
        return RED;
    }
    if blue_swarm > red_swarm {
        return BLUE;
    }

    0
}

#[inline]
fn eval_terminal_from_winner(winner: u8, perspective: u8, turn: u16) -> i32 {
    if winner == 0 {
        return 0;
    }
    if winner == perspective {
        WIN_SCORE - turn as i32
    } else {
        -WIN_SCORE + turn as i32
    }
}

#[inline]
fn search_terminal_from_winner(winner: u8, perspective: u8, ply: i32) -> i32 {
    if winner == 0 {
        return 0;
    }
    if winner == perspective {
        MATE_SCORE - ply
    } else {
        -MATE_SCORE + ply
    }
}

#[inline]
fn terminal_swarm_eval_score(pos: &Position, perspective: u8) -> i32 {
    eval_terminal_from_winner(swarm_winner(pos), perspective, pos.turn)
}

fn shape_and_fragile(pos: &Position, player: u8) -> (i32, i32, i32) {
    let neighbors = neighbors_table();
    let counts = neighbors_count_table();
    let center_table = center();

    let mut center_sum = 0_i32;
    let mut links = 0_i32;
    let mut fragile_penalty = 0_i32;

    for square in 0..NUM_SQUARES {
        if piece_owner(pos.board[square]) != player {
            continue;
        }

        center_sum += center_table[square];

        let mut friendly = 0_i32;
        for i in 0..counts[square] as usize {
            let nb = neighbors[square][i] as i32;
            if nb < 0 {
                continue;
            }
            let nbs = nb as usize;
            if piece_owner(pos.board[nbs]) == player {
                friendly += 1;
                if nbs > square {
                    links += 1;
                }
            }
        }

        if friendly <= 1 {
            let fish_weight = pos.fish_value[square] as i32 + 1;
            fragile_penalty += (2 - friendly) * fish_weight * 2;
        }
    }

    (links, center_sum, fragile_penalty)
}

#[derive(Clone, Copy, Debug, Default)]
struct ComponentMetrics {
    components: i32,
    largest_value: i32,
    spread: i32,
}

fn component_metrics(pos: &Position, player: u8) -> ComponentMetrics {
    let piece_count = if player == RED {
        pos.red_count as i32
    } else {
        pos.blue_count as i32
    };
    if piece_count <= 0 {
        return ComponentMetrics::default();
    }

    let neighbors = neighbors_table();
    let counts = neighbors_count_table();

    let mut seen = [false; NUM_SQUARES];
    let mut stack = [0_usize; NUM_SQUARES];
    let mut centroids = [(0_i32, 0_i32); NUM_SQUARES];
    let mut centroid_count = 0_usize;

    let mut components = 0_i32;
    let mut largest_value = 0_i32;

    for square in 0..NUM_SQUARES {
        if piece_owner(pos.board[square]) != player || seen[square] {
            continue;
        }

        components += 1;
        let mut top = 0_usize;
        stack[top] = square;
        top += 1;
        seen[square] = true;

        let mut value_sum = 0_i32;
        let mut size = 0_i32;
        let mut sum_x = 0_i32;
        let mut sum_y = 0_i32;

        while top > 0 {
            top -= 1;
            let cur = stack[top];

            value_sum += pos.fish_value[cur] as i32;
            size += 1;
            sum_x += (cur % 10) as i32;
            sum_y += (cur / 10) as i32;

            for i in 0..counts[cur] as usize {
                let nb = neighbors[cur][i];
                if nb < 0 {
                    continue;
                }
                let nbs = nb as usize;
                if !seen[nbs] && piece_owner(pos.board[nbs]) == player {
                    seen[nbs] = true;
                    stack[top] = nbs;
                    top += 1;
                }
            }
        }

        largest_value = max(largest_value, value_sum);
        centroids[centroid_count] = ((sum_x + size / 2) / size, (sum_y + size / 2) / size);
        centroid_count += 1;
    }

    let spread = if centroid_count <= 1 {
        0
    } else {
        let mut gx = 0_i32;
        let mut gy = 0_i32;
        for (cx, cy) in centroids.iter().copied().take(centroid_count) {
            gx += cx;
            gy += cy;
        }
        gx /= centroid_count as i32;
        gy /= centroid_count as i32;

        let mut out = 0_i32;
        for (cx, cy) in centroids.iter().copied().take(centroid_count) {
            out += max((cx - gx).abs(), (cy - gy).abs());
        }
        out
    };

    ComponentMetrics {
        components,
        largest_value,
        spread,
    }
}

fn has_one_move_connect(pos: &mut Position, player: u8, max_checks: usize) -> bool {
    let piece_count = if player == RED {
        pos.red_count
    } else {
        pos.blue_count
    };
    if piece_count > 8 {
        return false;
    }

    let mut moves = Vec::new();
    generate_moves(pos, player, &mut moves);
    if moves.is_empty() {
        return false;
    }

    let center_table = center();
    let opp = opponent(player);

    moves.sort_by(|a, b| {
        let ac = if piece_owner(pos.board[a.to as usize]) == opp {
            1_i32
        } else {
            0_i32
        };
        let bc = if piece_owner(pos.board[b.to as usize]) == opp {
            1_i32
        } else {
            0_i32
        };
        if ac != bc {
            return bc.cmp(&ac);
        }

        let ca = center_table[a.to as usize];
        let cb = center_table[b.to as usize];
        if ca != cb {
            return cb.cmp(&ca);
        }

        if a.to != b.to {
            return b.to.cmp(&a.to);
        }
        b.from.cmp(&a.from)
    });

    for mv in moves.into_iter().take(max_checks) {
        let saved = pos.player_to_move;
        pos.player_to_move = player;

        let mut undo = Undo::default();
        let ok = pos.make_move(mv, &mut undo);
        if !ok {
            pos.player_to_move = saved;
            continue;
        }

        let connected = pos.is_connected(player);
        pos.unmake_move(&undo);
        pos.player_to_move = saved;

        if connected {
            return true;
        }
    }

    false
}

fn has_one_move_disconnect(
    pos: &mut Position,
    attacker: u8,
    target: u8,
    max_checks: usize,
) -> bool {
    let target_count = if target == RED {
        pos.red_count
    } else {
        pos.blue_count
    };
    if target_count <= 1 || target_count > 12 {
        return false;
    }

    let base_components = pos.component_count(target);
    let base_largest = pos.largest_component_value(target);

    let mut moves = Vec::new();
    generate_moves(pos, attacker, &mut moves);
    if moves.is_empty() {
        return false;
    }

    let center_table = center();
    let target_player = target;
    moves.sort_by(|a, b| {
        let ac = if piece_owner(pos.board[a.to as usize]) == target_player {
            1_i32
        } else {
            0_i32
        };
        let bc = if piece_owner(pos.board[b.to as usize]) == target_player {
            1_i32
        } else {
            0_i32
        };
        if ac != bc {
            return bc.cmp(&ac);
        }
        center_table[b.to as usize].cmp(&center_table[a.to as usize])
    });

    for mv in moves.into_iter().take(max_checks) {
        let saved = pos.player_to_move;
        pos.player_to_move = attacker;

        let mut undo = Undo::default();
        if !pos.make_move(mv, &mut undo) {
            pos.player_to_move = saved;
            continue;
        }

        let new_components = pos.component_count(target);
        let new_largest = pos.largest_component_value(target);
        pos.unmake_move(&undo);
        pos.player_to_move = saved;

        if new_components > base_components || new_largest + 1 < base_largest {
            return true;
        }
    }

    false
}

fn has_two_move_connect(
    pos: &mut Position,
    player: u8,
    first_checks: usize,
    second_checks: usize,
) -> bool {
    let piece_count = if player == RED {
        pos.red_count
    } else {
        pos.blue_count
    };
    if piece_count > 10 {
        return false;
    }

    let mut moves = Vec::new();
    generate_moves(pos, player, &mut moves);
    if moves.is_empty() {
        return false;
    }

    let center_table = center();
    moves.sort_by(|a, b| {
        let ca = center_table[a.to as usize];
        let cb = center_table[b.to as usize];
        if ca != cb {
            return cb.cmp(&ca);
        }
        if a.to != b.to {
            return b.to.cmp(&a.to);
        }
        b.from.cmp(&a.from)
    });

    for mv in moves.into_iter().take(first_checks) {
        let saved = pos.player_to_move;
        pos.player_to_move = player;

        let mut undo = Undo::default();
        if !pos.make_move(mv, &mut undo) {
            pos.player_to_move = saved;
            continue;
        }

        let in_two = has_one_move_connect(pos, player, second_checks);
        pos.unmake_move(&undo);
        pos.player_to_move = saved;

        if in_two {
            return true;
        }
    }

    false
}

fn has_two_move_disconnect(
    pos: &mut Position,
    attacker: u8,
    target: u8,
    first_checks: usize,
    second_checks: usize,
) -> bool {
    let target_count = if target == RED {
        pos.red_count
    } else {
        pos.blue_count
    };
    if target_count <= 1 || target_count > 14 {
        return false;
    }

    let mut moves = Vec::new();
    generate_moves(pos, attacker, &mut moves);
    if moves.is_empty() {
        return false;
    }

    let center_table = center();
    let target_player = target;
    moves.sort_by(|a, b| {
        let ac = if piece_owner(pos.board[a.to as usize]) == target_player {
            1_i32
        } else {
            0_i32
        };
        let bc = if piece_owner(pos.board[b.to as usize]) == target_player {
            1_i32
        } else {
            0_i32
        };
        if ac != bc {
            return bc.cmp(&ac);
        }
        center_table[b.to as usize].cmp(&center_table[a.to as usize])
    });

    for mv in moves.into_iter().take(first_checks) {
        let saved = pos.player_to_move;
        pos.player_to_move = attacker;

        let mut undo = Undo::default();
        if !pos.make_move(mv, &mut undo) {
            pos.player_to_move = saved;
            continue;
        }

        let in_two = has_one_move_disconnect(pos, attacker, target, second_checks);
        pos.unmake_move(&undo);
        pos.player_to_move = saved;

        if in_two {
            return true;
        }
    }

    false
}

fn bridge_profile(pos: &Position, player: u8) -> (i32, i32) {
    let neighbors = neighbors_table();
    let counts = neighbors_count_table();

    let mut bridge_risk = 0_i32;
    let mut bridge_redundancy = 0_i32;

    for square in 0..NUM_SQUARES {
        if piece_owner(pos.board[square]) != player {
            continue;
        }

        let fish_weight = pos.fish_value[square] as i32 + 1;
        let mut friendly_nbs = [usize::MAX; 8];
        let mut friendly_count = 0_usize;

        for i in 0..counts[square] as usize {
            let nb = neighbors[square][i];
            if nb < 0 {
                continue;
            }
            let nbs = nb as usize;
            if piece_owner(pos.board[nbs]) == player {
                friendly_nbs[friendly_count] = nbs;
                friendly_count += 1;
            }
        }

        if friendly_count == 2 {
            let a = friendly_nbs[0];
            let b = friendly_nbs[1];
            let mut adjacent = false;
            for i in 0..counts[a] as usize {
                if neighbors[a][i] >= 0 && neighbors[a][i] as usize == b {
                    adjacent = true;
                    break;
                }
            }
            if adjacent {
                bridge_redundancy += fish_weight;
            } else {
                bridge_risk += fish_weight * 2;
            }
        } else if friendly_count >= 3 {
            bridge_redundancy += (friendly_count as i32 - 2) * fish_weight;
        }
    }

    (bridge_risk, bridge_redundancy)
}

fn articulation_dfs(
    u: usize,
    pos: &Position,
    player: u8,
    neighbors: &[[i8; MAX_NEIGHBORS]; NUM_SQUARES],
    counts: &[u8; NUM_SQUARES],
    disc: &mut [i32; NUM_SQUARES],
    low: &mut [i32; NUM_SQUARES],
    parent: &mut [usize; NUM_SQUARES],
    is_articulation: &mut [bool; NUM_SQUARES],
    time: &mut i32,
) {
    disc[u] = *time;
    low[u] = *time;
    *time += 1;

    let mut children = 0_i32;
    for i in 0..counts[u] as usize {
        let nb = neighbors[u][i];
        if nb < 0 {
            continue;
        }
        let v = nb as usize;
        if piece_owner(pos.board[v]) != player {
            continue;
        }

        if disc[v] == -1 {
            parent[v] = u;
            children += 1;
            articulation_dfs(
                v,
                pos,
                player,
                neighbors,
                counts,
                disc,
                low,
                parent,
                is_articulation,
                time,
            );
            low[u] = min(low[u], low[v]);

            if parent[u] == usize::MAX {
                if children > 1 {
                    is_articulation[u] = true;
                }
            } else if low[v] >= disc[u] {
                is_articulation[u] = true;
            }
        } else if v != parent[u] {
            low[u] = min(low[u], disc[v]);
        }
    }
}

fn articulation_mass(pos: &Position, player: u8) -> i32 {
    let piece_count = if player == RED {
        pos.red_count as i32
    } else {
        pos.blue_count as i32
    };
    if piece_count <= 2 {
        return 0;
    }

    let neighbors = neighbors_table();
    let counts = neighbors_count_table();
    let mut disc = [-1_i32; NUM_SQUARES];
    let mut low = [0_i32; NUM_SQUARES];
    let mut parent = [usize::MAX; NUM_SQUARES];
    let mut is_articulation = [false; NUM_SQUARES];
    let mut time = 0_i32;

    for square in 0..NUM_SQUARES {
        if piece_owner(pos.board[square]) != player || disc[square] != -1 {
            continue;
        }
        articulation_dfs(
            square,
            pos,
            player,
            neighbors,
            counts,
            &mut disc,
            &mut low,
            &mut parent,
            &mut is_articulation,
            &mut time,
        );
    }

    let mut mass = 0_i32;
    for square in 0..NUM_SQUARES {
        if !is_articulation[square] {
            continue;
        }

        let mut friendly_deg = 0_i32;
        for i in 0..counts[square] as usize {
            let nb = neighbors[square][i];
            if nb < 0 {
                continue;
            }
            if piece_owner(pos.board[nb as usize]) == player {
                friendly_deg += 1;
            }
        }
        let fish_weight = pos.fish_value[square] as i32 + 1;
        mass += fish_weight * max(1, 4 - friendly_deg);
    }

    mass
}

fn safe_capture_profile(pos: &mut Position, player: u8, max_checks: usize) -> i32 {
    let mut captures = Vec::new();
    generate_capture_moves(pos, player, &mut captures);
    if captures.is_empty() {
        return 0;
    }

    captures.sort_by(|a, b| {
        let av = pos.fish_value[a.to as usize];
        let bv = pos.fish_value[b.to as usize];
        if av != bv {
            return bv.cmp(&av);
        }
        if a.to != b.to {
            return b.to.cmp(&a.to);
        }
        b.from.cmp(&a.from)
    });

    let base_components = pos.component_count(player);
    let base_largest = pos.largest_component_value(player);
    let mut score = 0_i32;
    for mv in captures.into_iter().take(max_checks) {
        let captured = pos.fish_value[mv.to as usize] as i32;
        let saved = pos.player_to_move;
        pos.player_to_move = player;

        let mut undo = Undo::default();
        if !pos.make_move(mv, &mut undo) {
            pos.player_to_move = saved;
            continue;
        }

        let after_components = pos.component_count(player);
        let after_largest = pos.largest_component_value(player);
        if after_components <= base_components {
            score += captured * 2;
            if after_largest >= base_largest {
                score += 2;
            }
        } else {
            score -= captured * 2 + max(0, after_components - base_components) * 3;
            if after_largest + 1 < base_largest {
                score -= 3;
            }
        }

        pos.unmake_move(&undo);
        pos.player_to_move = saved;
    }

    score
}

fn capture_cut_pressure(pos: &mut Position, attacker: u8, target: u8, max_checks: usize) -> i32 {
    let mut captures = Vec::new();
    generate_capture_moves(pos, attacker, &mut captures);
    if captures.is_empty() {
        return 0;
    }

    let neighbors = neighbors_table();
    let counts = neighbors_count_table();
    let mut target_criticality = [0_i32; NUM_SQUARES];
    for square in 0..NUM_SQUARES {
        if piece_owner(pos.board[square]) != target {
            continue;
        }

        let mut friendly_nbs = [usize::MAX; 8];
        let mut friendly_count = 0_usize;
        for i in 0..counts[square] as usize {
            let nb = neighbors[square][i];
            if nb < 0 {
                continue;
            }
            let nbs = nb as usize;
            if piece_owner(pos.board[nbs]) == target {
                friendly_nbs[friendly_count] = nbs;
                friendly_count += 1;
            }
        }

        let mut critical = (pos.fish_value[square] as i32 + 1) * max(0, 3 - friendly_count as i32);
        if friendly_count == 2 {
            let a = friendly_nbs[0];
            let b = friendly_nbs[1];
            let mut adjacent = false;
            for i in 0..counts[a] as usize {
                if neighbors[a][i] >= 0 && neighbors[a][i] as usize == b {
                    adjacent = true;
                    break;
                }
            }
            if !adjacent {
                critical += 4;
            }
        }
        target_criticality[square] = critical;
    }

    captures.sort_by(|a, b| {
        let asq = a.to as usize;
        let bsq = b.to as usize;
        let av = target_criticality[asq] * 10 + pos.fish_value[asq] as i32;
        let bv = target_criticality[bsq] * 10 + pos.fish_value[bsq] as i32;
        if av != bv {
            return bv.cmp(&av);
        }
        if a.to != b.to {
            return b.to.cmp(&a.to);
        }
        b.from.cmp(&a.from)
    });

    let base_components = pos.component_count(target);
    let base_largest = pos.largest_component_value(target);
    let base_own_components = pos.component_count(attacker);

    let mut score = 0_i32;
    for mv in captures.into_iter().take(max_checks) {
        let to_sq = mv.to as usize;
        let critical = target_criticality[to_sq];
        let saved = pos.player_to_move;
        pos.player_to_move = attacker;

        let mut undo = Undo::default();
        if !pos.make_move(mv, &mut undo) {
            pos.player_to_move = saved;
            continue;
        }

        let after_target_components = pos.component_count(target);
        let after_target_largest = pos.largest_component_value(target);
        let after_own_components = pos.component_count(attacker);
        pos.unmake_move(&undo);
        pos.player_to_move = saved;

        let target_split_gain = max(0, after_target_components - base_components) * 6
            + max(0, base_largest - after_target_largest);
        let own_split_penalty = max(0, after_own_components - base_own_components) * 5;
        score += critical + target_split_gain - own_split_penalty;
    }

    score
}

#[inline]
fn collapse_risk(
    mobility: i32,
    target_diversity: i32,
    components: i32,
    fragment_value: i32,
    connected: bool,
) -> i32 {
    let mut risk = 0_i32;

    risk += if mobility <= 1 {
        12
    } else if mobility <= 2 {
        8
    } else if mobility <= 4 {
        4
    } else {
        0
    };

    risk += if target_diversity <= 1 {
        7
    } else if target_diversity <= 2 {
        4
    } else if target_diversity <= 4 {
        2
    } else {
        0
    };

    if components >= 3 {
        risk += (components - 2) * 2;
    }
    if fragment_value >= 4 {
        risk += min(10, fragment_value / 2);
    }
    if connected && mobility >= 3 {
        risk = max(0, risk - 2);
    }

    risk
}

#[inline]
fn blend_weight(early: i32, late: i32, late_mix_permille: i32) -> i32 {
    let late_mix = late_mix_permille.clamp(0, 1000);
    ((early * (1000 - late_mix)) + (late * late_mix)) / 1000
}

pub fn evaluate_hce(pos: &mut Position, perspective: u8, depth_hint: i32) -> i32 {
    init_tables();

    let opp = opponent(perspective);
    let weights = get_eval_weights();

    let round_connect_outcome = round_end_connection_outcome(pos);
    if round_connect_outcome == RED || round_connect_outcome == BLUE {
        return eval_terminal_from_winner(round_connect_outcome, perspective, pos.turn);
    }
    if round_connect_outcome == ROUND_CONNECT_BOTH {
        return terminal_swarm_eval_score(pos, perspective);
    }

    if pos.turn >= 60 {
        return terminal_swarm_eval_score(pos, perspective);
    }

    let own_count = if perspective == RED {
        pos.red_count as i32
    } else {
        pos.blue_count as i32
    };
    let opp_count = if opp == RED {
        pos.red_count as i32
    } else {
        pos.blue_count as i32
    };

    let own_total_value = pos.total_piece_value(perspective);
    let opp_total_value = pos.total_piece_value(opp);

    if own_count == 0 && opp_count > 0 {
        return -WIN_SCORE + pos.turn as i32;
    }
    if opp_count == 0 && own_count > 0 {
        return WIN_SCORE - pos.turn as i32;
    }

    let (own_links, own_center, own_fragile) = shape_and_fragile(pos, perspective);
    let (opp_links, opp_center, opp_fragile) = shape_and_fragile(pos, opp);

    // Fast path for deeper nodes: avoid repeated component traversals on hot paths.
    if depth_hint >= 3 && pos.turn < 56 {
        let mut fast_score = 0_i32;
        fast_score += weights.w_count * (own_total_value - opp_total_value);
        fast_score += weights.w_links * (own_links - opp_links);
        fast_score += weights.w_center * (own_center - opp_center);
        fast_score += 26 * (opp_fragile - own_fragile);
        fast_score += 16 * (own_count - opp_count);
        fast_score += 14 * ((own_links * 2 - own_count) - (opp_links * 2 - opp_count));
        return min(WIN_SCORE, max(-WIN_SCORE, fast_score));
    }

    let own_metrics = component_metrics(pos, perspective);
    let opp_metrics = component_metrics(pos, opp);
    let own_largest = own_metrics.largest_value;
    let opp_largest = opp_metrics.largest_value;
    let own_connected = own_total_value > 0 && own_largest == own_total_value;
    let opp_connected = opp_total_value > 0 && opp_largest == opp_total_value;
    let mut pending_connectivity_threat = 0_i32;

    // Mid-round connectivity is not an immediate win by rules, but still a strong threat.
    if !is_round_end_state(pos) {
        if own_connected && !opp_connected {
            let pending = if pos.player_to_move == opp {
                weights.connect_bonus / 3
            } else {
                weights.connect_bonus / 2
            };
            let mut fast_score = 0_i32;
            fast_score += weights.w_count * (own_total_value - opp_total_value);
            fast_score += weights.w_links * (own_links - opp_links);
            fast_score += weights.w_center * (own_center - opp_center);
            fast_score += 30 * (opp_fragile - own_fragile);
            fast_score += pending;
            pending_connectivity_threat += pending;
            if depth_hint <= 1 && !has_legal_move(pos, opp) {
                return WIN_SCORE - pos.turn as i32;
            }
            if depth_hint >= 2 && pos.turn < 56 {
                return min(WIN_SCORE, max(-WIN_SCORE, fast_score));
            }
        } else if opp_connected && !own_connected {
            let pending = if pos.player_to_move == perspective {
                weights.connect_bonus / 3
            } else {
                weights.connect_bonus / 2
            };
            let mut fast_score = 0_i32;
            fast_score += weights.w_count * (own_total_value - opp_total_value);
            fast_score += weights.w_links * (own_links - opp_links);
            fast_score += weights.w_center * (own_center - opp_center);
            fast_score += 30 * (opp_fragile - own_fragile);
            fast_score -= pending;
            pending_connectivity_threat -= pending;
            if depth_hint <= 1 && !has_legal_move(pos, perspective) {
                return -WIN_SCORE + pos.turn as i32;
            }
            if depth_hint >= 2 && pos.turn < 56 {
                return min(WIN_SCORE, max(-WIN_SCORE, fast_score));
            }
        }
    }

    let own_components = own_metrics.components;
    let opp_components = opp_metrics.components;
    let own_fragment_value = max(0, own_total_value - own_largest);
    let opp_fragment_value = max(0, opp_total_value - opp_largest);
    let own_cohesion_permille = if own_total_value > 0 {
        (own_largest * 1000) / own_total_value
    } else {
        1000
    };
    let opp_cohesion_permille = if opp_total_value > 0 {
        (opp_largest * 1000) / opp_total_value
    } else {
        1000
    };

    let own_spread = own_metrics.spread;
    let opp_spread = opp_metrics.spread;

    let late_phase = pos.turn >= 40 || (own_count + opp_count) <= 12;
    let allow_expensive = depth_hint <= 0;
    let late_mix_permille = ((pos.turn as i32 - 18).clamp(0, 42) * 1000) / 42;
    let w_largest = blend_weight(weights.w_largest, weights.w_late_largest, late_mix_permille);
    let w_components = blend_weight(
        weights.w_components,
        weights.w_late_components,
        late_mix_permille,
    );
    let w_spread = blend_weight(weights.w_spread, weights.w_late_spread, late_mix_permille);
    let w_links = blend_weight(weights.w_links, weights.w_late_links, late_mix_permille);
    let w_mobility = blend_weight(
        weights.w_mobility,
        weights.w_late_mobility,
        late_mix_permille,
    );

    let force_mobility_probe =
        late_phase || own_count <= 6 || opp_count <= 6 || own_connected || opp_connected;
    let need_mobility = force_mobility_probe
        && (late_phase || (own_count + opp_count) <= 16)
        && (own_components <= 3 || opp_components <= 3);

    let mut own_mobility = 0_i32;
    let mut opp_mobility = 0_i32;
    let mut own_target_diversity = 0_i32;
    let mut opp_target_diversity = 0_i32;
    let mut own_has_move = true;
    let mut opp_has_move = true;

    if force_mobility_probe {
        own_has_move = has_legal_move(pos, perspective);
        opp_has_move = has_legal_move(pos, opp);

        if !own_has_move && opp_has_move {
            return -WIN_SCORE + pos.turn as i32;
        }
        if !opp_has_move && own_has_move {
            return WIN_SCORE - pos.turn as i32;
        }
    }

    if need_mobility {
        let mut tmp = Vec::new();
        let mut target_map = [false; NUM_SQUARES];

        generate_moves(pos, perspective, &mut tmp);
        own_mobility = tmp.len() as i32;
        for mv in tmp.iter().copied() {
            target_map[mv.to as usize] = true;
        }
        own_target_diversity = target_map.iter().filter(|&&v| v).count() as i32;

        tmp.clear();
        target_map.fill(false);
        generate_moves(pos, opp, &mut tmp);
        opp_mobility = tmp.len() as i32;
        for mv in tmp.iter().copied() {
            target_map[mv.to as usize] = true;
        }
        opp_target_diversity = target_map.iter().filter(|&&v| v).count() as i32;
    }

    let mut own_bridge_risk = 0_i32;
    let mut own_bridge_redundancy = 0_i32;
    let mut opp_bridge_risk = 0_i32;
    let mut opp_bridge_redundancy = 0_i32;
    if allow_expensive || late_phase || own_count <= 12 || opp_count <= 12 {
        let (a, b) = bridge_profile(pos, perspective);
        own_bridge_risk = a;
        own_bridge_redundancy = b;
        let (c, d) = bridge_profile(pos, opp);
        opp_bridge_risk = c;
        opp_bridge_redundancy = d;
    }

    let mut own_connect_in1 = 0_i32;
    let mut opp_connect_in1 = 0_i32;
    let mut own_connect_in2 = 0_i32;
    let mut opp_connect_in2 = 0_i32;
    let mut own_disconnect_in1 = 0_i32;
    let mut opp_disconnect_in1 = 0_i32;
    let mut own_disconnect_in2 = 0_i32;
    let mut opp_disconnect_in2 = 0_i32;
    if own_count <= 10 && opp_count <= 10 && depth_hint <= 1 {
        if has_one_move_connect(pos, perspective, 4) {
            own_connect_in1 = 1;
        }
        if has_one_move_connect(pos, opp, 4) {
            opp_connect_in1 = 1;
        }
        if has_one_move_disconnect(pos, perspective, opp, 4) {
            own_disconnect_in1 = 1;
        }
        if has_one_move_disconnect(pos, opp, perspective, 4) {
            opp_disconnect_in1 = 1;
        }
    }
    if own_count <= 10 && opp_count <= 10 && depth_hint <= 0 {
        if has_two_move_connect(pos, perspective, 3, 3) {
            own_connect_in2 = 1;
        }
        if has_two_move_connect(pos, opp, 3, 3) {
            opp_connect_in2 = 1;
        }
        if has_two_move_disconnect(pos, perspective, opp, 3, 3) {
            own_disconnect_in2 = 1;
        }
        if has_two_move_disconnect(pos, opp, perspective, 3, 3) {
            opp_disconnect_in2 = 1;
        }
    }

    let mut safe_capture_delta = 0_i32;
    if allow_expensive && late_phase && (own_count + opp_count) <= 20 {
        let own_safe = safe_capture_profile(pos, perspective, 4);
        let opp_safe = safe_capture_profile(pos, opp, 4);
        safe_capture_delta = own_safe - opp_safe;
    }

    let mut cut_pressure_delta = 0_i32;
    if allow_expensive && late_phase && (own_count + opp_count) <= 22 {
        let own_cut = capture_cut_pressure(pos, perspective, opp, 4);
        let opp_cut = capture_cut_pressure(pos, opp, perspective, 4);
        cut_pressure_delta = own_cut - opp_cut;
    }

    let mut own_articulation = 0_i32;
    let mut opp_articulation = 0_i32;
    if allow_expensive || late_phase || own_count <= 10 || opp_count <= 10 {
        own_articulation = articulation_mass(pos, perspective);
        opp_articulation = articulation_mass(pos, opp);
    }

    let half_moves_left = max(0, 60 - pos.turn as i32);
    let race_active = late_phase || half_moves_left <= 18 || own_count <= 10 || opp_count <= 10;
    let race_urgency_permille = ((24 - half_moves_left).clamp(0, 24) * 1000) / 24;
    let mut race_delta = 0_i32;
    if race_active {
        race_delta += weights.w_race_connect1 * (own_connect_in1 - opp_connect_in1);
        race_delta += weights.w_race_connect2 * (own_connect_in2 - opp_connect_in2);
        race_delta += weights.w_race_disconnect1 * (own_disconnect_in1 - opp_disconnect_in1);
        race_delta += weights.w_race_disconnect2 * (own_disconnect_in2 - opp_disconnect_in2);

        if pos.player_to_move == perspective && (own_connect_in1 + own_disconnect_in1) > 0 {
            race_delta += weights.w_race_side_to_move;
        }
        if pos.player_to_move == opp && (opp_connect_in1 + opp_disconnect_in1) > 0 {
            race_delta -= weights.w_race_side_to_move;
        }
    }

    let mut round_end_tempo = 0_i32;
    if race_active {
        let side_to_move = pos.player_to_move;
        let stm_tactical = if side_to_move == perspective {
            own_connect_in1 + own_disconnect_in1
        } else {
            opp_connect_in1 + opp_disconnect_in1
        };
        let non_stm_tactical = if side_to_move == perspective {
            opp_connect_in1 + opp_disconnect_in1
        } else {
            own_connect_in1 + own_disconnect_in1
        };

        // Blue has the final half-move before round-end. This raises tactical tempo value.
        round_end_tempo = if side_to_move == BLUE {
            stm_tactical * 2 - non_stm_tactical
        } else {
            stm_tactical - non_stm_tactical * 2
        };
    }

    let mut score = 0_i32;
    score += w_largest * (own_largest - opp_largest);
    score += w_components * (opp_components - own_components);
    score += w_spread * (opp_spread - own_spread);
    score += weights.w_count * (own_total_value - opp_total_value);
    score += w_links * (own_links - opp_links);
    score += weights.w_center * (own_center - opp_center);
    score += 28 * (opp_fragile - own_fragile);
    score += weights.w_bridge_risk * (opp_bridge_risk - own_bridge_risk);
    score += weights.w_bridge_redundancy * (own_bridge_redundancy - opp_bridge_redundancy);
    score += weights.w_threat_in1 * (own_connect_in1 - opp_connect_in1);
    score += weights.w_threat_in2 * (own_connect_in2 - opp_connect_in2);
    score += weights.w_safe_capture * safe_capture_delta;
    score += weights.w_cut_pressure * cut_pressure_delta;
    score += weights.w_articulation_pressure * (opp_articulation - own_articulation);
    score += pending_connectivity_threat;
    if race_active {
        score += ((race_delta as i64 * (700 + race_urgency_permille as i64)) / 1000) as i32;
        score += weights.w_round_end_tempo * round_end_tempo;
    }

    if need_mobility {
        score += w_mobility * (own_mobility - opp_mobility);
        score += weights.w_mobility_targets * (own_target_diversity - opp_target_diversity);
        if own_mobility <= 2 {
            score -= weights.w_no_move_pressure * (3 - own_mobility);
        }
        if opp_mobility <= 2 {
            score += weights.w_no_move_pressure * (3 - opp_mobility);
        }
        let own_risk = collapse_risk(
            own_mobility,
            own_target_diversity,
            own_components,
            own_fragment_value,
            own_connected,
        );
        let opp_risk = collapse_risk(
            opp_mobility,
            opp_target_diversity,
            opp_components,
            opp_fragment_value,
            opp_connected,
        );
        score += weights.w_collapse_risk * (opp_risk - own_risk);
    }
    if force_mobility_probe {
        if own_has_move && !opp_has_move {
            score += weights.connect_bonus / 3;
        } else if opp_has_move && !own_has_move {
            score -= weights.connect_bonus / 3;
        }
    }

    if allow_expensive && !late_phase {
        if own_components <= 3 && own_count <= 10 && has_one_move_connect(pos, perspective, 2) {
            score += weights.connect_bonus / 4;
        }
        if opp_components <= 3 && opp_count <= 10 && has_one_move_connect(pos, opp, 2) {
            score -= weights.connect_bonus / 4;
        }
        if depth_hint <= 0
            && own_count <= 12
            && opp_count <= 12
            && has_one_move_disconnect(pos, perspective, opp, 3)
        {
            score += weights.connect_bonus / 6;
        }
        if depth_hint <= 0
            && own_count <= 12
            && opp_count <= 12
            && has_one_move_disconnect(pos, opp, perspective, 3)
        {
            score -= weights.connect_bonus / 6;
        }
    }

    if late_phase {
        score += 20 * (opp_fragile - own_fragile);
        score +=
            weights.w_late_swarm_cohesion * (own_cohesion_permille - opp_cohesion_permille) / 10;
        score += weights.w_late_fragment_pressure * (opp_fragment_value - own_fragment_value);

        if allow_expensive
            && own_components <= 2
            && own_count <= 8
            && has_one_move_connect(pos, perspective, 4)
        {
            score += weights.connect_bonus;
        }
        if allow_expensive
            && opp_components <= 2
            && opp_count <= 8
            && has_one_move_connect(pos, opp, 4)
        {
            score -= weights.connect_bonus;
        }
        if allow_expensive
            && own_count <= 12
            && opp_count <= 12
            && has_one_move_disconnect(pos, perspective, opp, 5)
        {
            score += weights.connect_bonus / 3 + weights.w_late_disconnect_pressure;
        }
        if allow_expensive
            && own_count <= 12
            && opp_count <= 12
            && has_one_move_disconnect(pos, opp, perspective, 5)
        {
            score -= weights.connect_bonus / 3 + weights.w_late_disconnect_pressure;
        }
    }

    min(WIN_SCORE, max(-WIN_SCORE, score))
}

pub fn terminal_swarm_score(pos: &Position, perspective: u8, ply: i32) -> i32 {
    search_terminal_from_winner(swarm_winner(pos), perspective, ply)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::state::{make_piece, EMPTY};

    fn empty_position() -> Position {
        let mut pos = Position::default();
        pos.board = [EMPTY; NUM_SQUARES];
        pos.player_to_move = RED;
        pos.turn = 0;
        pos.recompute_caches();
        pos
    }

    #[test]
    fn articulation_mass_detects_fragile_chain() {
        init_tables();

        let mut fragile = empty_position();
        fragile.board[44] = make_piece(BLUE, 1);
        fragile.board[45] = make_piece(BLUE, 3);
        fragile.board[46] = make_piece(BLUE, 1);
        fragile.recompute_caches();

        let mut robust = empty_position();
        robust.board[44] = make_piece(BLUE, 1);
        robust.board[45] = make_piece(BLUE, 3);
        robust.board[55] = make_piece(BLUE, 1);
        robust.recompute_caches();

        assert!(articulation_mass(&fragile, BLUE) > articulation_mass(&robust, BLUE));
    }
}
