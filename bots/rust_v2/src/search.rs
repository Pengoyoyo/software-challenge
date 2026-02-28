use std::time::{Duration, Instant};

use socha::i_client_handler::ComCancelHandler;

use crate::board::{get_tables, opponent, piece_owner, Move, MoveList, Position, ONE};
use crate::evaluate::{evaluate, terminal_swarm_score, MATE_SCORE};
use crate::tt::{TranspositionTable, EXACT, LOWER, UPPER};

// ─── Constants ────────────────────────────────────────────────────────────────
const MAX_PLY: usize = 128;
const TT_MB: usize = 128; // bumped from 96

// Search pruning / reduction parameters
const ASP_WINDOW: i32 = 80;
const NMP_MIN_DEPTH: i32 = 3;
const NMP_R: i32 = 2;
const NMP_MIN_PIECES: u16 = 6;
const LMR_MIN_DEPTH: i32 = 3;
const LMR_MIN_IDX: usize = 3;
const LMP_BASE: usize = 8;
const LMP_SCALE: usize = 4;
const FUT_DEPTH: i32 = 2;
const FUT_MARGIN: i32 = 130;
const RFP_DEPTH: i32 = 3;
const RFP_MARGIN: i32 = 120;
const MULTICUT_DEPTH: i32 = 9;
const MULTICUT_MOVES: usize = 10;
const MULTICUT_REQUIRED: usize = 4;
const QSEARCH_CAP: i32 = 4;
const Q_DELTA: i32 = 160;

// Singular extension parameters
const SE_MIN_DEPTH: i32 = 6;
const SE_MARGIN_PER_DEPTH: i32 = 2;

// IIR: reduce depth by 1 when no TT move at PV/cut nodes
const IIR_MIN_DEPTH: i32 = 4;

// History bounds
const HIST_MAX: i16 = 16000;
const HIST_BONUS_CAP: i32 = 400;

// ─── LMR Table (log-based) ────────────────────────────────────────────────────

struct LmrTable {
    table: Box<[[i32; 256]; 65]>,
}

impl LmrTable {
    fn new() -> Self {
        let mut table = Box::new([[0i32; 256]; 65]);
        for depth in 1usize..=64 {
            for idx in 1usize..=255 {
                let r = 0.75 + (depth as f64).ln() * (idx as f64).ln() / 2.2;
                table[depth][idx] = r as i32;
            }
        }
        LmrTable { table }
    }

    #[inline(always)]
    fn get(&self, depth: i32, idx: usize) -> i32 {
        self.table[(depth as usize).min(64)][idx.min(255)]
    }
}

use std::sync::OnceLock;
static LMR_TABLE: OnceLock<LmrTable> = OnceLock::new();
fn lmr_table() -> &'static LmrTable {
    LMR_TABLE.get_or_init(LmrTable::new)
}

// ─── Time manager ─────────────────────────────────────────────────────────────

struct TimeManager {
    start: Instant,
    deadline: Instant,
    timed_out: bool,
    nodes: u64,
    check_mask: u64,
}

impl TimeManager {
    fn new(deadline: Instant) -> Self {
        TimeManager {
            start: Instant::now(),
            deadline,
            timed_out: false,
            nodes: 0,
            check_mask: 255,
        }
    }

    #[inline(always)]
    fn tick(&mut self) {
        self.nodes += 1;
        if (self.nodes & self.check_mask) != 0 {
            return;
        }
        let now = Instant::now();
        if now >= self.deadline {
            self.timed_out = true;
            return;
        }
        let rem_ns = (self.deadline - now).as_nanos() as u64;
        self.check_mask = if rem_ns < 30_000_000 {
            3
        } else if rem_ns < 50_000_000 {
            7
        } else if rem_ns < 80_000_000 {
            15
        } else if rem_ns < 160_000_000 {
            31
        } else if rem_ns < 320_000_000 {
            63
        } else if rem_ns < 600_000_000 {
            127
        } else {
            255
        };
    }

    #[inline(always)]
    fn timed_out(&self) -> bool {
        self.timed_out
    }

    fn remaining_ns(&self) -> u64 {
        let now = Instant::now();
        if now >= self.deadline { 0 } else { (self.deadline - now).as_nanos() as u64 }
    }

    fn elapsed_ns(&self) -> u64 {
        self.start.elapsed().as_nanos() as u64
    }

    fn can_start_next_iter(&self, prev_iter_ns: u64, fail_events: i32, best_move_changes: i32) -> bool {
        let rem = self.remaining_ns();
        if rem <= 10_000_000 { return false; }
        let mut safety = 12_000_000u64;
        safety += fail_events.max(0) as u64 * 2_000_000;
        safety += best_move_changes.max(0) as u64 * 2_000_000;
        let mut predicted = prev_iter_ns.max(25_000_000);
        let growth = 1500i64
            + (fail_events.max(0) as i64 * 80 + best_move_changes.max(0) as i64 * 60).min(500);
        predicted = (predicted as i64 * growth / 1000).max(15_000_000) as u64;
        rem > predicted + safety
    }
}

// ─── Public result types ──────────────────────────────────────────────────────

pub struct DepthInfo {
    pub depth: i32,
    pub score: i32,
    pub delta_nodes: u64,
    pub delta_tt_hits: u64,
    pub nps: u64,
    pub elapsed_s: f64,
}

pub struct SearchResult {
    pub best_move: Option<Move>,
    pub num_moves: usize,
    pub depths: Vec<DepthInfo>,
}

// ─── Search engine ────────────────────────────────────────────────────────────

pub struct SearchEngine {
    pub tt: TranspositionTable,
    history: [[i16; 100]; 100], // i16 for smaller cache footprint
    killers: [[u16; 2]; MAX_PLY],
    counter_moves: [[u16; 100]; 100],
    tt_hits: u64,
}

impl SearchEngine {
    pub fn new() -> Self {
        SearchEngine {
            tt: TranspositionTable::new(TT_MB),
            history: [[0; 100]; 100],
            killers: [[0; 2]; MAX_PLY],
            counter_moves: [[0; 100]; 100],
            tt_hits: 0,
        }
    }

    fn decay_histories(&mut self) {
        for row in self.history.iter_mut() {
            for v in row.iter_mut() {
                *v /= 2;
            }
        }
    }

    #[inline]
    fn apply_history_bonus(&mut self, from: usize, to: usize, bonus: i32) {
        let h = &mut self.history[from][to];
        *h = (*h as i32 + bonus).clamp(-(HIST_MAX as i32), HIST_MAX as i32) as i16;
    }

    #[inline]
    fn apply_history_malus(&mut self, from: usize, to: usize, malus: i32) {
        let h = &mut self.history[from][to];
        *h = (*h as i32 - malus).clamp(-(HIST_MAX as i32), HIST_MAX as i32) as i16;
    }

    // ── Move ordering score ────────────────────────────────────────────────────

    fn score_move(&self, pos: &Position, mv: Move, player: u8,
        tt_move: u16, k1: u16, k2: u16, counter: u16) -> i32
    {
        let t = get_tables();
        let encoded = mv.encode();
        let opp = opponent(player);
        let mut score = 0i32;

        if tt_move != 0 && tt_move == encoded { return 10_000_000; }

        if piece_owner(pos.board[mv.to as usize]) == opp {
            score += 5_000_000 + 50_000 * pos.fish_value[mv.to as usize] as i32;
        }
        if k1 != 0 && k1 == encoded { score += 2_000_000; }
        else if k2 != 0 && k2 == encoded { score += 1_500_000; }
        if counter != 0 && counter == encoded { score += 1_200_000; }

        score += 800 * pos.local_connectivity_swing(mv, player);
        score += self.history[mv.from as usize][mv.to as usize] as i32;
        score += 180 * t.center[mv.to as usize] as i32;
        score -= mv.from as i32 * 3;
        score -= mv.to as i32;
        score
    }

    fn order_moves(&self, pos: &Position, player: u8, moves: &mut MoveList,
        scores: &mut [i32; 256], tt_move: u16, k1: u16, k2: u16, counter: u16)
    {
        for i in 0..moves.len {
            scores[i] = self.score_move(pos, moves.moves[i], player, tt_move, k1, k2, counter);
        }
        for i in 1..moves.len {
            let mv = moves.moves[i];
            let sc = scores[i];
            let mut j = i;
            while j > 0 && scores[j - 1] < sc {
                scores[j] = scores[j - 1];
                moves.moves[j] = moves.moves[j - 1];
                j -= 1;
            }
            moves.moves[j] = mv;
            scores[j] = sc;
        }
    }

    // ── Public search entry point ──────────────────────────────────────────────

    pub fn search(&mut self, pos: &mut Position, deadline: Instant) -> SearchResult {
        self.tt.new_search();
        self.decay_histories();
        self.tt_hits = 0;
        let _ = lmr_table(); // warm up

        let mut timer = TimeManager::new(deadline);
        let mut root_moves = MoveList::new();
        pos.generate_moves(&mut root_moves);
        let num_moves = root_moves.len;
        if root_moves.len == 0 {
            return SearchResult { best_move: None, num_moves: 0, depths: Vec::new() };
        }

        let mut best_move: Option<Move> = Some(root_moves.moves[0]);
        let mut prev_score = 0i32;
        let mut prev_iter_ns = 0u64;
        let mut recent_fail = 0i32;
        let mut recent_changes = 0i32;
        let mut depth_infos: Vec<DepthInfo> = Vec::new();

        'outer: for depth in 1i32..=64 {
            if depth > 1 && !timer.can_start_next_iter(prev_iter_ns, recent_fail, recent_changes) {
                break;
            }
            let iter_start_ns = timer.elapsed_ns();
            let nodes_before = timer.nodes;
            let tt_hits_before = self.tt_hits;

            let (mut alpha, mut beta) = if depth >= 3 {
                (prev_score - ASP_WINDOW, prev_score + ASP_WINDOW)
            } else {
                (-MATE_SCORE, MATE_SCORE)
            };
            let mut asp_window = ASP_WINDOW;
            let mut iter_best = root_moves.moves[0];
            let mut iter_score = -MATE_SCORE;

            loop {
                let tt_move = self.tt.best_move(pos.hash);
                let mut scores = [0i32; 256];
                self.order_moves(pos, pos.player, &mut root_moves, &mut scores,
                    tt_move, self.killers[0][0], self.killers[0][1], 0);

                let player = pos.player;
                let opp = opponent(player);
                let opp_threat = depth <= 3
                    && pos.component_count(opp) <= 2
                    && crate::evaluate::has_one_move_connect(pos, opp, 4);

                let mut local_alpha = alpha;
                let mut local_best = -MATE_SCORE;
                let mut local_best_move = root_moves.moves[0];

                for i in 0..root_moves.len {
                    timer.tick();
                    if timer.timed_out() { break 'outer; }

                    let mv = root_moves.moves[i];
                    let quiet = piece_owner(pos.board[mv.to as usize]) != opp;
                    let mut undo = crate::board::Undo::default();
                    if !pos.make_move(mv, &mut undo) { continue; }

                    // Immediate win at root
                    if pos.is_connected(player) {
                        pos.unmake_move(&undo);
                        let score = MATE_SCORE - 1;
                        if score > local_best { local_best = score; local_best_move = mv; }
                        if score > local_alpha { local_alpha = score; }
                        break;
                    }

                    let mut extension = 0i32;
                    if depth <= 4 && pos.is_connected(player) { extension = 1; }
                    if depth <= 3 && opp_threat && !crate::evaluate::has_one_move_connect(pos, opp, 3) {
                        extension = extension.max(1);
                    }
                    let child_depth = (depth - 1 + extension).max(0);

                    let score = if i == 0 {
                        -self.search_node::<true>(pos, &mut timer, child_depth, -beta, -local_alpha, 1, false, true, mv.encode(), 0)
                    } else {
                        let mut s = -self.search_node::<false>(pos, &mut timer, child_depth, -local_alpha - 1, -local_alpha, 1, true, true, mv.encode(), 0);
                        if s > local_alpha && s < beta && !timer.timed_out() {
                            s = -self.search_node::<true>(pos, &mut timer, child_depth, -beta, -local_alpha, 1, false, true, mv.encode(), 0);
                        }
                        s
                    };
                    pos.unmake_move(&undo);
                    if timer.timed_out() { break 'outer; }

                    if score > local_best { local_best = score; local_best_move = mv; }
                    if score > local_alpha { local_alpha = score; }
                    if local_alpha >= beta {
                        if quiet {
                            self.killers[0][1] = self.killers[0][0];
                            self.killers[0][0] = mv.encode();
                            let bonus = (depth * depth).min(HIST_BONUS_CAP);
                            self.apply_history_bonus(mv.from as usize, mv.to as usize, bonus);
                        }
                        break;
                    }
                }
                if timer.timed_out() { break 'outer; }

                iter_score = local_best;
                iter_best = local_best_move;

                if iter_score <= alpha {
                    asp_window *= 2;
                    alpha = (iter_score - asp_window).max(-MATE_SCORE);
                    beta = beta.min(alpha + asp_window * 2);
                } else if iter_score >= beta {
                    asp_window *= 2;
                    beta = (iter_score + asp_window).min(MATE_SCORE);
                    alpha = alpha.max(beta - asp_window * 2);
                } else {
                    break;
                }
            }
            if timer.timed_out() { break; }

            if iter_best != best_move.unwrap_or_default() { recent_changes += 1; }
            best_move = Some(iter_best);
            prev_score = iter_score;

            let iter_elapsed_ns = (timer.elapsed_ns() - iter_start_ns).max(1);
            let delta_nodes = timer.nodes.saturating_sub(nodes_before);
            let delta_tt = self.tt_hits.saturating_sub(tt_hits_before);
            let nps = delta_nodes * 1_000_000_000 / iter_elapsed_ns;
            depth_infos.push(DepthInfo {
                depth, score: iter_score, delta_nodes, delta_tt_hits: delta_tt,
                nps, elapsed_s: iter_elapsed_ns as f64 / 1e9,
            });

            if let Some(p) = root_moves.as_slice().iter().position(|m| *m == iter_best) {
                root_moves.moves[..root_moves.len].rotate_left(p);
            }
            prev_iter_ns = iter_elapsed_ns;
            recent_fail = 0;
            if iter_score.abs() >= MATE_SCORE - 128 { break; }
        }

        SearchResult { best_move, num_moves, depths: depth_infos }
    }

    // ── Pondering ─────────────────────────────────────────────────────────────

    pub fn search_ponder(&mut self, pos: &mut Position, cancel: &ComCancelHandler) {
        self.tt.new_search();
        let far_future = Instant::now() + Duration::from_secs(1000);
        let mut timer = TimeManager::new(far_future);
        let mut root_moves = MoveList::new();
        pos.generate_moves(&mut root_moves);
        if root_moves.len == 0 { return; }
        let mut prev_score = 0i32;

        for depth in 1i32..=64 {
            if cancel.is_cancelled() { break; }
            let (mut alpha, mut beta) = if depth >= 3 {
                (prev_score - ASP_WINDOW, prev_score + ASP_WINDOW)
            } else {
                (-MATE_SCORE, MATE_SCORE)
            };
            let mut asp_window = ASP_WINDOW;
            let mut iter_score = -MATE_SCORE;
            let mut iter_best = root_moves.moves[0];

            'asp: loop {
                let tt_move = self.tt.best_move(pos.hash);
                let mut scores = [0i32; 256];
                self.order_moves(pos, pos.player, &mut root_moves, &mut scores,
                    tt_move, self.killers[0][0], self.killers[0][1], 0);

                let player = pos.player;
                let opp = opponent(player);
                let opp_threat = depth <= 3
                    && pos.component_count(opp) <= 2
                    && crate::evaluate::has_one_move_connect(pos, opp, 4);
                let mut local_alpha = alpha;
                let mut local_best = -MATE_SCORE;
                let mut local_best_move = root_moves.moves[0];

                for i in 0..root_moves.len {
                    if cancel.is_cancelled() { break 'asp; }
                    let mv = root_moves.moves[i];
                    let mut undo = crate::board::Undo::default();
                    if !pos.make_move(mv, &mut undo) { continue; }

                    let mut extension = 0i32;
                    if depth <= 4 && pos.is_connected(player) { extension = 1; }
                    if depth <= 3 && opp_threat && !crate::evaluate::has_one_move_connect(pos, opp, 3) {
                        extension = extension.max(1);
                    }
                    let child_depth = (depth - 1 + extension).max(0);

                    let score = if i == 0 {
                        -self.search_node::<true>(pos, &mut timer, child_depth, -beta, -local_alpha, 1, false, true, mv.encode(), 0)
                    } else {
                        let mut s = -self.search_node::<false>(pos, &mut timer, child_depth, -local_alpha - 1, -local_alpha, 1, true, true, mv.encode(), 0);
                        if s > local_alpha && s < beta {
                            s = -self.search_node::<true>(pos, &mut timer, child_depth, -beta, -local_alpha, 1, false, true, mv.encode(), 0);
                        }
                        s
                    };
                    pos.unmake_move(&undo);

                    if score > local_best { local_best = score; local_best_move = mv; }
                    if score > local_alpha { local_alpha = score; }
                    if local_alpha >= beta { break; }
                }
                if cancel.is_cancelled() { break 'asp; }

                iter_score = local_best;
                iter_best = local_best_move;

                if iter_score <= alpha {
                    asp_window *= 2;
                    alpha = (iter_score - asp_window).max(-MATE_SCORE);
                    beta = beta.min(alpha + asp_window * 2);
                } else if iter_score >= beta {
                    asp_window *= 2;
                    beta = (iter_score + asp_window).min(MATE_SCORE);
                    alpha = alpha.max(beta - asp_window * 2);
                } else { break; }
            }
            if cancel.is_cancelled() { break; }
            prev_score = iter_score;
            if let Some(p) = root_moves.as_slice().iter().position(|m| *m == iter_best) {
                root_moves.moves[..root_moves.len].rotate_left(p);
            }
            if iter_score.abs() >= MATE_SCORE - 128 { break; }
        }
    }

    // ── Negamax (PVS) with all improvements ───────────────────────────────────

    fn search_node<const PV: bool>(
        &mut self,
        pos: &mut Position,
        timer: &mut TimeManager,
        mut depth: i32,
        mut alpha: i32,
        beta: i32,
        ply: usize,
        cut_node: bool,
        allow_null: bool,
        prev_move: u16,
        excluded_move: u16, // singular search: skip this move (0 = none)
    ) -> i32 {
        timer.tick();
        if timer.timed_out() { return evaluate(pos, pos.player, depth); }
        if ply >= MAX_PLY - 1 { return evaluate(pos, pos.player, depth); }

        let player = pos.player;
        let opp = opponent(player);

        if pos.turn >= 60 { return terminal_swarm_score(pos, player, ply as i32); }

        let own_count = if player == ONE { pos.one_count } else { pos.two_count };
        let opp_count = if player == ONE { pos.two_count } else { pos.one_count };

        if own_count == 0 && opp_count > 0 { return -MATE_SCORE + ply as i32; }
        if opp_count == 0 && own_count > 0 { return  MATE_SCORE - ply as i32; }
        if pos.is_connected(opp)           { return -MATE_SCORE + ply as i32; }

        if depth <= 0 { return self.quiescence(pos, timer, alpha, beta, ply, 0); }

        // ── TT probe ──────────────────────────────────────────────────────────
        let key = pos.hash;
        let (tt_hit, tt_move_raw) = self.tt.probe(key, depth, alpha, beta);
        if let Some(score) = tt_hit {
            self.tt_hits += 1;
            if !PV && excluded_move == 0 { return score; }
        }
        let tt_move = if tt_move_raw != 0 { tt_move_raw } else { self.tt.best_move(key) };

        // ── Static eval ───────────────────────────────────────────────────────
        let static_eval = evaluate(pos, player, depth);

        // ── Reverse Futility Pruning ──────────────────────────────────────────
        if !PV && excluded_move == 0 && depth <= RFP_DEPTH && static_eval - RFP_MARGIN * depth >= beta {
            return static_eval;
        }

        let own_components = pos.component_count(player);
        let opp_components = pos.component_count(opp);

        // ── Null Move Pruning ─────────────────────────────────────────────────
        if allow_null && !PV && excluded_move == 0 && depth >= NMP_MIN_DEPTH
            && own_count > NMP_MIN_PIECES && own_components > 2 && opp_components > 2
            && static_eval >= beta
        {
            let saved = pos.make_null_move();
            let nm_depth = (depth - 1 - NMP_R - depth / 6).max(0);
            let nm_score = -self.search_node::<false>(pos, timer, nm_depth, -beta, -beta + 1, ply + 1, true, false, 0, 0);
            pos.unmake_null_move(saved);

            if nm_score >= beta {
                if depth >= NMP_MIN_DEPTH + 3 {
                    let verify = self.search_node::<false>(pos, timer, nm_depth, beta - 1, beta, ply, false, false, prev_move, 0);
                    if verify >= beta { return verify; }
                } else {
                    return nm_score;
                }
            }
        }

        // ── IIR: reduce depth when no TT move ────────────────────────────────
        if tt_move == 0 && depth >= IIR_MIN_DEPTH && (PV || cut_node) {
            depth -= 1;
            if depth <= 0 { return self.quiescence(pos, timer, alpha, beta, ply, 0); }
        }

        // ── Move generation ───────────────────────────────────────────────────
        let mut moves = MoveList::new();
        pos.generate_moves(&mut moves);
        if moves.len == 0 { return static_eval - 2_000; }

        let ply_clamped = ply.min(MAX_PLY - 1);
        let k1 = self.killers[ply_clamped][0];
        let k2 = self.killers[ply_clamped][1];
        let counter = if prev_move != 0 {
            let pm = Move::decode(prev_move);
            self.counter_moves[pm.from as usize][pm.to as usize]
        } else { 0 };

        let mut scores = [0i32; 256];
        self.order_moves(pos, player, &mut moves, &mut scores, tt_move, k1, k2, counter);

        // ── Multi-Cut ─────────────────────────────────────────────────────────
        if !PV && cut_node && depth >= MULTICUT_DEPTH && static_eval >= beta - 220 && moves.len >= 12 {
            let mc_depth = (depth - 4).max(0);
            let mut hits = 0;
            for i in 0..MULTICUT_MOVES.min(moves.len) {
                let mv = moves.moves[i];
                if excluded_move != 0 && mv.encode() == excluded_move { continue; }
                let mut undo = crate::board::Undo::default();
                if !pos.make_move(mv, &mut undo) { continue; }
                let score = -self.search_node::<false>(pos, timer, mc_depth, -beta, -beta + 1, ply + 1, true, true, mv.encode(), 0);
                pos.unmake_move(&undo);
                if score >= beta { hits += 1; if hits >= MULTICUT_REQUIRED { return beta; } }
                if timer.timed_out() { break; }
            }
        }

        let original_alpha = alpha;
        let mut best_score = -MATE_SCORE;
        let mut best_move = moves.moves[0];

        let opp_threat = depth <= 3 && opp_components <= 2
            && crate::evaluate::has_one_move_connect(pos, opp, 3);

        // Track quiet moves for history malus
        let mut quiets_searched = [0u16; 64];
        let mut n_quiets = 0usize;

        let mut actual_idx = 0usize; // index excluding skipped moves
        for idx in 0..moves.len {
            if timer.timed_out() { break; }

            let mv = moves.moves[idx];
            if excluded_move != 0 && mv.encode() == excluded_move { continue; }

            let quiet = piece_owner(pos.board[mv.to as usize]) != opp;

            // Futility Pruning
            if !PV && quiet && depth <= FUT_DEPTH && actual_idx >= 3
                && static_eval + FUT_MARGIN * depth <= alpha
            {
                actual_idx += 1;
                continue;
            }
            // Late Move Pruning
            if !PV && quiet && depth <= 2 && actual_idx >= LMP_BASE + LMP_SCALE * depth as usize {
                break;
            }

            let mut undo = crate::board::Undo::default();
            if !pos.make_move(mv, &mut undo) { actual_idx += 1; continue; }

            // Immediate win check
            if pos.is_connected(player) {
                pos.unmake_move(&undo);
                let win = MATE_SCORE - ply as i32;
                self.tt.store(key, depth, win, LOWER, mv.encode());
                return win;
            }

            // Track quiet for malus
            if quiet && n_quiets < 64 {
                quiets_searched[n_quiets] = mv.encode();
                n_quiets += 1;
            }

            // ── Singular Extension ─────────────────────────────────────────
            let mut extension = 0i32;

            if !PV && depth >= SE_MIN_DEPTH && excluded_move == 0
                && mv.encode() == tt_move && tt_move != 0
            {
                let singular_beta = static_eval - depth * SE_MARGIN_PER_DEPTH;
                let se_depth = (depth - 1) / 2;
                pos.unmake_move(&undo);
                let se_score = self.search_node::<false>(
                    pos, timer, se_depth, singular_beta - 1, singular_beta,
                    ply, cut_node, allow_null, prev_move, tt_move,
                );
                if !pos.make_move(mv, &mut undo) { actual_idx += 1; continue; }
                // Re-check win after remake
                if pos.is_connected(player) {
                    pos.unmake_move(&undo);
                    let win = MATE_SCORE - ply as i32;
                    self.tt.store(key, depth, win, LOWER, mv.encode());
                    return win;
                }
                if se_score < singular_beta { extension = 1; }
            } else {
                // Connect extension
                if depth <= 4 && pos.is_connected(player) { extension = 1; }
                // Threat extension
                if depth <= 3 && opp_threat && !crate::evaluate::has_one_move_connect(pos, opp, 2) {
                    extension = extension.max(1);
                }
            }

            let full_depth = (depth - 1 + extension).max(0);

            // LMR (log-based table)
            let mut reduction = 0i32;
            if !PV && quiet && depth >= LMR_MIN_DEPTH && actual_idx >= LMR_MIN_IDX {
                reduction = lmr_table().get(depth, actual_idx);
                if self.history[mv.from as usize][mv.to as usize] > 0 { reduction -= 1; }
                if cut_node { reduction += 1; }
                reduction = reduction.clamp(0, full_depth - 1);
            }

            let score = if actual_idx == 0 {
                -self.search_node::<PV>(pos, timer, full_depth, -beta, -alpha, ply + 1, false, true, mv.encode(), 0)
            } else {
                let reduced = (full_depth - reduction).max(0);
                let mut s = -self.search_node::<false>(pos, timer, reduced, -alpha - 1, -alpha, ply + 1, true, true, mv.encode(), 0);
                if s > alpha && (PV || reduction > 0) && s < beta {
                    s = -self.search_node::<PV>(pos, timer, full_depth, -beta, -alpha, ply + 1, false, true, mv.encode(), 0);
                }
                s
            };

            pos.unmake_move(&undo);

            if score > best_score { best_score = score; best_move = mv; }
            if score > alpha {
                alpha = score;
                if prev_move != 0 {
                    let pm = Move::decode(prev_move);
                    self.counter_moves[pm.from as usize][pm.to as usize] = mv.encode();
                }
            }
            if alpha >= beta {
                if quiet {
                    let kp = &mut self.killers[ply_clamped];
                    kp[1] = kp[0]; kp[0] = mv.encode();
                    let bonus = (depth * depth).min(HIST_BONUS_CAP);
                    self.apply_history_bonus(mv.from as usize, mv.to as usize, bonus);
                    // History malus for all previously searched quiets
                    for i in 0..n_quiets {
                        let qm = Move::decode(quiets_searched[i]);
                        if quiets_searched[i] != mv.encode() {
                            self.apply_history_malus(qm.from as usize, qm.to as usize, bonus);
                        }
                    }
                }
                break;
            }
            actual_idx += 1;
        }

        if best_score <= -MATE_SCORE { return static_eval; }

        let bound = if best_score <= original_alpha { UPPER }
                    else if best_score >= beta       { LOWER }
                    else                             { EXACT };
        self.tt.store(key, depth, best_score, bound, best_move.encode());
        best_score
    }

    // ── Quiescence search ─────────────────────────────────────────────────────

    fn quiescence(&mut self, pos: &mut Position, timer: &mut TimeManager,
        mut alpha: i32, beta: i32, ply: usize, qdepth: i32) -> i32
    {
        timer.tick();
        if timer.timed_out() { return evaluate(pos, pos.player, 0); }

        let player = pos.player;
        let opp = opponent(player);

        if pos.turn >= 60 { return terminal_swarm_score(pos, player, ply as i32); }
        if pos.is_connected(opp) { return -MATE_SCORE + ply as i32; }

        let stand_pat = evaluate(pos, player, 0);
        if stand_pat >= beta { return stand_pat; }
        if stand_pat > alpha { alpha = stand_pat; }
        if qdepth >= QSEARCH_CAP { return stand_pat; }

        let t = get_tables();
        let mut noisy: [(i32, Move); 256] = [(0, Move::default()); 256];
        let mut noisy_len = 0usize;

        let mut caps = MoveList::new();
        pos.generate_captures(&mut caps);
        for mv in caps.as_slice() {
            let cap_val = pos.fish_value[mv.to as usize] as i32;
            let swing = pos.local_connectivity_swing(*mv, player);
            noisy[noisy_len] = (3_000_000 + cap_val * 120_000 + swing * 4_000 + t.center[mv.to as usize] as i32, *mv);
            noisy_len += 1;
        }

        if qdepth < 2 && timer.remaining_ns() > 50_000_000 {
            let base_components = pos.component_count(player);
            let mut all_moves = MoveList::new();
            pos.generate_moves(&mut all_moves);
            for mv in all_moves.as_slice() {
                timer.tick();
                if timer.timed_out() { break; }
                if piece_owner(pos.board[mv.to as usize]) == opp { continue; }
                let swing = pos.local_connectivity_swing(*mv, player);
                if swing <= 0 { continue; }
                let mut undo = crate::board::Undo::default();
                if !pos.make_move(*mv, &mut undo) { continue; }
                let new_components = pos.component_count(player);
                pos.unmake_move(&undo);
                if new_components >= base_components { continue; }
                let comp_gain = base_components - new_components;
                noisy[noisy_len] = (1_200_000 + comp_gain * 180_000 + swing * 8_000 + t.center[mv.to as usize] as i32, *mv);
                noisy_len += 1;
            }
        }

        if noisy_len == 0 { return stand_pat; }
        noisy[..noisy_len].sort_unstable_by(|a, b| b.0.cmp(&a.0));

        for (_, mv) in noisy[..noisy_len].iter() {
            let capture = piece_owner(pos.board[mv.to as usize]) == opp;
            if !capture && stand_pat + Q_DELTA < alpha { continue; }
            let mut undo = crate::board::Undo::default();
            if !pos.make_move(*mv, &mut undo) { continue; }
            let score = if pos.is_connected(player) {
                MATE_SCORE - ply as i32
            } else {
                -self.quiescence(pos, timer, -beta, -alpha, ply + 1, qdepth + 1)
            };
            pos.unmake_move(&undo);
            if score >= beta { return score; }
            if score > alpha { alpha = score; }
            if timer.timed_out() { break; }
        }
        alpha
    }
}

impl Default for SearchEngine {
    fn default() -> Self { Self::new() }
}
