use std::cmp::{max, min};
use std::collections::HashMap;
use std::fs::File;
use std::io::{BufRead, BufReader, Read};

use crate::eval::{
    evaluate_hce, round_end_connection_outcome, set_eval_weights, terminal_swarm_score,
    EvalWeights, ROUND_CONNECT_BOTH,
};
use crate::movegen::{generate_capture_moves, generate_moves, has_legal_move};
use crate::state::{
    init_tables, neighbors_count_table, neighbors_table, opponent, piece_owner, piece_value, Move,
    NullUndo, Position, Undo, BLUE, MATE_SCORE, MAX_PLY, NUM_SQUARES, RED, WIN_SCORE,
};
use crate::time_manager::TimeManager;
use crate::tt::{Bound, TranspositionTable};

fn center_table() -> &'static [i32; NUM_SQUARES] {
    static CENTER: std::sync::OnceLock<[i32; NUM_SQUARES]> = std::sync::OnceLock::new();
    CENTER.get_or_init(|| {
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
    })
}

#[inline]
fn clamp_score(score: i32) -> i32 {
    max(-WIN_SCORE, min(WIN_SCORE, score))
}

#[inline]
fn local_connectivity_swing(position: &Position, mv: Move, player: u8) -> i32 {
    let neighbors = neighbors_table();
    let counts = neighbors_count_table();

    let from = mv.from as usize;
    let to = mv.to as usize;

    let mut from_neighbors = 0_i32;
    for i in 0..counts[from] as usize {
        let square = neighbors[from][i] as i32;
        if square >= 0 && piece_owner(position.board[square as usize]) == player {
            from_neighbors += 1;
        }
    }

    let mut to_neighbors = 0_i32;
    for i in 0..counts[to] as usize {
        let square = neighbors[to][i] as i32;
        if square < 0 {
            continue;
        }
        let square = square as usize;
        if square == from || piece_owner(position.board[square]) == player {
            to_neighbors += 1;
        }
    }

    to_neighbors - from_neighbors
}

#[inline]
fn promote_move_front(moves: &mut [Move], encoded: u16) -> bool {
    if encoded == 0 || moves.is_empty() {
        return false;
    }

    for idx in 0..moves.len() {
        if moves[idx].encode() != encoded {
            continue;
        }

        if idx == 0 {
            return true;
        }

        let preferred = moves[idx];
        for j in (1..=idx).rev() {
            moves[j] = moves[j - 1];
        }
        moves[0] = preferred;
        return true;
    }

    false
}

fn parse_u64(token: &str) -> Option<u64> {
    if token.is_empty() {
        return None;
    }

    if token.len() > 2 && (token.starts_with("0x") || token.starts_with("0X")) {
        u64::from_str_radix(&token[2..], 16).ok()
    } else {
        token.parse::<u64>().ok()
    }
}

fn parse_u16(token: &str) -> Option<u16> {
    if token.is_empty() {
        return None;
    }

    let value = if token.len() > 2 && (token.starts_with("0x") || token.starts_with("0X")) {
        u32::from_str_radix(&token[2..], 16).ok()?
    } else {
        token.parse::<u32>().ok()?
    };

    if value > u16::MAX as u32 {
        return None;
    }

    Some(value as u16)
}

#[derive(Clone, Debug)]
pub struct EngineConfig {
    pub max_depth: i32,
    pub aspiration_window: i32,

    pub null_move_min_depth: i32,
    pub null_move_reduction: i32,
    pub null_move_min_pieces: i32,

    pub lmr_min_depth: i32,
    pub lmr_min_index: i32,

    pub lmp_base: i32,
    pub lmp_scale: i32,

    pub futility_depth: i32,
    pub futility_margin: i32,

    pub reverse_futility_depth: i32,
    pub reverse_futility_margin: i32,

    pub multicut_depth: i32,
    pub multicut_reduction: i32,
    pub multicut_moves: i32,
    pub multicut_required: i32,

    pub qsearch_depth_cap: i32,
    pub q_delta_margin: i32,

    pub tt_mb: usize,
    pub preserve_tt_across_moves: bool,
    pub decay_histories: bool,
    pub history_decay_permille: i32,
    pub enable_reply_cache: bool,
    pub reply_cache_limit: i32,
    pub enable_pv_precalc: bool,
    pub precalc_ply_limit: i32,
    pub precalc_pv_length: i32,
    pub enable_anti_shuffle: bool,
    pub anti_shuffle_window: i32,
    pub anti_shuffle_penalty: i32,
    pub enable_subtree_reuse: bool,
    pub subtree_reuse_depth_min: i32,
    pub enable_opening_book: bool,
    pub book_max_ply: i32,
    pub book_path: String,
    pub policy_cache_path: String,
    pub policy_cache_turn_max: i32,
    pub book_max_mb: usize,
    pub book_force_confidence: i32,
    pub book_hint_confidence: i32,
    pub book_min_samples: i32,
    pub enable_verification_search: bool,
    pub verification_depth_margin: i32,
    pub enable_singular_extension: bool,
    pub singular_depth_min: i32,
    pub qsearch_top_k: i32,

    pub eval_weights: EvalWeights,
}

impl Default for EngineConfig {
    fn default() -> Self {
        Self {
            max_depth: 64,
            aspiration_window: 110,

            null_move_min_depth: 3,
            null_move_reduction: 4,
            null_move_min_pieces: 6,

            lmr_min_depth: 2,
            lmr_min_index: 2,

            lmp_base: 4,
            lmp_scale: 1,

            futility_depth: 2,
            futility_margin: 130,

            reverse_futility_depth: 3,
            reverse_futility_margin: 120,

            multicut_depth: 7,
            multicut_reduction: 2,
            multicut_moves: 10,
            multicut_required: 1,

            qsearch_depth_cap: 8,
            q_delta_margin: 160,

            tt_mb: 96,
            preserve_tt_across_moves: true,
            decay_histories: true,
            history_decay_permille: 850,
            enable_reply_cache: true,
            reply_cache_limit: 16_384,
            enable_pv_precalc: true,
            precalc_ply_limit: 30,
            precalc_pv_length: 16,
            enable_anti_shuffle: false,
            anti_shuffle_window: 16,
            anti_shuffle_penalty: 45,
            enable_subtree_reuse: true,
            subtree_reuse_depth_min: 4,
            enable_opening_book: true,
            book_max_ply: 16,
            book_path: String::from("artifacts/opening_book.bin"),
            policy_cache_path: String::from("artifacts/opening_policy_cache.bin"),
            policy_cache_turn_max: 14,
            book_max_mb: 256,
            book_force_confidence: 85,
            book_hint_confidence: 65,
            book_min_samples: 6,
            enable_verification_search: false,
            verification_depth_margin: 2,
            enable_singular_extension: false,
            singular_depth_min: 10,
            qsearch_top_k: 8,

            eval_weights: EvalWeights::default(),
        }
    }
}

#[derive(Clone, Debug, Default)]
pub struct SearchStats {
    pub nodes: u64,
    pub qnodes: u64,
    pub tt_probes: u64,
    pub tt_hits: u64,
    pub eval_calls: u64,
    pub reply_cache_hits: u64,
    pub anti_shuffle_hits: u64,
    pub subtree_reuse_hits: u64,
    pub book_hits: u64,
    pub book_forced_hits: u64,
    pub book_hint_hits: u64,
    pub verification_nodes: u64,
    pub singular_extensions: u64,
    pub no_move_terminal_hits: u64,
    pub fail_high: i32,
    pub fail_low: i32,
    pub best_move_changes: i32,
    pub completed_depth: i32,
}

#[derive(Clone, Debug, Default)]
pub struct IterationTrace {
    pub depth: i32,
    pub score: i32,
    pub nodes_delta: u64,
    pub tt_hits_delta: u64,
    pub elapsed_ns_delta: u64,
    pub nps_iter: u64,
}

#[derive(Clone, Debug, Default)]
pub struct SearchResult {
    pub has_move: bool,
    pub best_move: Move,
    pub score: i32,
    pub depth: i32,
    pub elapsed_ns: u64,
    pub legal_root_count: usize,
    pub team: u8,
    pub iterations: Vec<IterationTrace>,
    pub stats: SearchStats,
}

#[derive(Clone, Copy, Debug, Default)]
struct SubtreeReuseEntry {
    mv: u16,
    depth: u8,
}

#[derive(Clone, Copy, Debug, Default)]
struct PolicyCacheEntry {
    best_move: u16,
    alt_move_1: u16,
    alt_move_2: u16,
    score_cp: i16,
    searched_depth: u8,
    samples: u16,
    confidence: u8,
}

pub struct SearchEngine {
    config: EngineConfig,
    tt: TranspositionTable,

    history: [[i32; NUM_SQUARES]; NUM_SQUARES],
    killers: [[u16; 2]; MAX_PLY],
    counter_moves: [[u16; NUM_SQUARES]; NUM_SQUARES],

    stats: SearchStats,
    root_best_move: Move,
    root_best_score: i32,
    last_root_hash: u64,
    last_turn: u16,
    recent_hashes: [u64; 256],
    recent_hash_count: usize,
    reply_cache: HashMap<u64, u16>,
    subtree_reuse: HashMap<u64, SubtreeReuseEntry>,
    opening_book: HashMap<u64, u16>,
    policy_cache: HashMap<u64, PolicyCacheEntry>,
    opening_book_loaded_path: String,
    opening_book_ready: bool,
    policy_cache_loaded_path: String,
    policy_cache_ready: bool,
}

impl Default for SearchEngine {
    fn default() -> Self {
        Self::new()
    }
}

impl SearchEngine {
    pub fn new() -> Self {
        let config = EngineConfig::default();
        set_eval_weights(config.eval_weights);

        Self {
            tt: TranspositionTable::new(config.tt_mb),
            config,
            history: [[0; NUM_SQUARES]; NUM_SQUARES],
            killers: [[0; 2]; MAX_PLY],
            counter_moves: [[0; NUM_SQUARES]; NUM_SQUARES],
            stats: SearchStats::default(),
            root_best_move: Move::default(),
            root_best_score: -WIN_SCORE,
            last_root_hash: 0,
            last_turn: 0,
            recent_hashes: [0; 256],
            recent_hash_count: 0,
            reply_cache: HashMap::new(),
            subtree_reuse: HashMap::new(),
            opening_book: HashMap::new(),
            policy_cache: HashMap::new(),
            opening_book_loaded_path: String::new(),
            opening_book_ready: false,
            policy_cache_loaded_path: String::new(),
            policy_cache_ready: false,
        }
    }

    pub fn set_config(&mut self, config: EngineConfig) {
        let old_tt_mb = self.config.tt_mb;
        let old_reply_limit = self.config.reply_cache_limit;
        let old_book_path = self.config.book_path.clone();
        let old_policy_path = self.config.policy_cache_path.clone();

        self.config = config;

        if old_tt_mb != self.config.tt_mb {
            self.tt.resize(self.config.tt_mb);
        }

        if old_reply_limit != self.config.reply_cache_limit {
            self.reply_cache.clear();
            self.subtree_reuse.clear();
        }

        if old_book_path != self.config.book_path {
            self.opening_book.clear();
            self.opening_book_loaded_path.clear();
            self.opening_book_ready = false;
        }
        if old_policy_path != self.config.policy_cache_path {
            self.policy_cache.clear();
            self.policy_cache_loaded_path.clear();
            self.policy_cache_ready = false;
        }

        set_eval_weights(self.config.eval_weights);
    }

    pub fn config(&self) -> &EngineConfig {
        &self.config
    }

    fn decay_histories(&mut self) {
        let permille = self.config.history_decay_permille.clamp(0, 1000) as i64;
        for from in 0..NUM_SQUARES {
            for to in 0..NUM_SQUARES {
                self.history[from][to] = ((self.history[from][to] as i64 * permille) / 1000) as i32;
            }
        }
    }

    fn reset_game_memory_if_needed(&mut self, position: &Position) {
        if position.turn == 0 || (self.last_turn > 0 && position.turn + 2 < self.last_turn) {
            self.recent_hash_count = 0;
            self.reply_cache.clear();
            self.subtree_reuse.clear();
        }
        self.last_turn = position.turn;
        self.last_root_hash = position.hash;
    }

    fn push_recent_hash(&mut self, hash: u64) {
        if self.recent_hash_count > 0 && self.recent_hashes[self.recent_hash_count - 1] == hash {
            return;
        }

        if self.recent_hash_count < self.recent_hashes.len() {
            self.recent_hashes[self.recent_hash_count] = hash;
            self.recent_hash_count += 1;
            return;
        }

        for i in 1..self.recent_hashes.len() {
            self.recent_hashes[i - 1] = self.recent_hashes[i];
        }
        self.recent_hashes[self.recent_hashes.len() - 1] = hash;
    }

    fn recent_hash_distance(&self, hash: u64, window: i32) -> i32 {
        if self.recent_hash_count == 0 || window <= 0 {
            return 0;
        }

        let lookback = min(window as usize, self.recent_hash_count);
        for i in 1..=lookback {
            if self.recent_hashes[self.recent_hash_count - i] == hash {
                return i as i32;
            }
        }

        0
    }

    fn lookup_reply_move(&self, hash: u64) -> u16 {
        if !self.config.enable_reply_cache || self.reply_cache.is_empty() {
            return 0;
        }
        *self.reply_cache.get(&hash).unwrap_or(&0)
    }

    fn store_reply_move(&mut self, hash: u64, mv: u16) {
        if !self.config.enable_reply_cache || mv == 0 {
            return;
        }

        let limit = max(128, self.config.reply_cache_limit) as usize;
        if self.reply_cache.len() >= limit {
            self.reply_cache.clear();
        }

        self.reply_cache.insert(hash, mv);
    }

    fn lookup_subtree_reuse(&self, hash: u64, min_depth: i32) -> u16 {
        if !self.config.enable_subtree_reuse || self.subtree_reuse.is_empty() {
            return 0;
        }

        let Some(entry) = self.subtree_reuse.get(&hash) else {
            return 0;
        };

        if (entry.depth as i32) < max(0, min_depth) {
            return 0;
        }

        entry.mv
    }

    fn store_subtree_reuse(&mut self, hash: u64, mv: u16, depth: i32) {
        if !self.config.enable_subtree_reuse || mv == 0 {
            return;
        }

        if depth < self.config.subtree_reuse_depth_min {
            return;
        }

        const MAX_SUBTREE_REUSE: usize = 32_768;
        if self.subtree_reuse.len() >= MAX_SUBTREE_REUSE {
            self.subtree_reuse.clear();
        }

        self.subtree_reuse.insert(
            hash,
            SubtreeReuseEntry {
                mv,
                depth: depth.clamp(0, 255) as u8,
            },
        );
    }

    fn ensure_opening_book_loaded(&mut self) {
        if !self.config.enable_opening_book {
            return;
        }

        if self.opening_book_ready && self.opening_book_loaded_path == self.config.book_path {
            return;
        }

        self.opening_book.clear();
        self.opening_book_loaded_path = self.config.book_path.clone();
        self.opening_book_ready = true;

        if self.config.book_path.is_empty() {
            return;
        }

        let mut file = match File::open(&self.config.book_path) {
            Ok(file) => file,
            Err(_) => return,
        };
        if self.config.book_max_mb > 0 {
            let max_bytes = (self.config.book_max_mb as u64).saturating_mul(1024 * 1024);
            if file
                .metadata()
                .map(|m| m.len() > max_bytes)
                .unwrap_or(false)
            {
                return;
            }
        }

        let mut magic = [0_u8; 4];
        let read_magic = file.read(&mut magic).unwrap_or(0);
        if read_magic == magic.len() && magic == *b"PBK1" {
            let mut record = [0_u8; 10];
            loop {
                match file.read_exact(&mut record) {
                    Ok(_) => {
                        let mut key = 0_u64;
                        for i in 0..8 {
                            key |= (record[i] as u64) << (8 * i);
                        }
                        let mv = (record[8] as u16) | ((record[9] as u16) << 8);
                        if mv != 0 {
                            self.opening_book.insert(key, mv);
                        }
                    }
                    Err(_) => break,
                }
            }
            return;
        }

        let file = match File::open(&self.config.book_path) {
            Ok(file) => file,
            Err(_) => return,
        };
        let reader = BufReader::new(file);

        for line in reader.lines().map_while(Result::ok) {
            let trimmed = line.trim();
            if trimmed.is_empty() || trimmed.starts_with('#') {
                continue;
            }

            let mut parts = trimmed.split_whitespace();
            let Some(hash_token) = parts.next() else {
                continue;
            };
            let Some(move_token) = parts.next() else {
                continue;
            };

            let Some(key) = parse_u64(hash_token) else {
                continue;
            };
            let Some(mv) = parse_u16(move_token) else {
                continue;
            };

            if mv != 0 {
                self.opening_book.insert(key, mv);
            }
        }
    }

    fn ensure_policy_cache_loaded(&mut self) {
        if !self.config.enable_opening_book {
            return;
        }

        if self.policy_cache_ready && self.policy_cache_loaded_path == self.config.policy_cache_path
        {
            return;
        }

        self.policy_cache.clear();
        self.policy_cache_loaded_path = self.config.policy_cache_path.clone();
        self.policy_cache_ready = true;

        if self.config.policy_cache_path.is_empty() {
            return;
        }

        let mut file = match File::open(&self.config.policy_cache_path) {
            Ok(file) => file,
            Err(_) => return,
        };
        if self.config.book_max_mb > 0 {
            let max_bytes = (self.config.book_max_mb as u64).saturating_mul(1024 * 1024);
            if file
                .metadata()
                .map(|m| m.len() > max_bytes)
                .unwrap_or(false)
            {
                return;
            }
        }

        let mut magic = [0_u8; 4];
        if file.read_exact(&mut magic).is_err() || magic != *b"OPC1" {
            return;
        }

        let mut record = [0_u8; 20];
        while file.read_exact(&mut record).is_ok() {
            let mut key = 0_u64;
            for i in 0..8 {
                key |= (record[i] as u64) << (8 * i);
            }

            let best_move = u16::from_le_bytes([record[8], record[9]]);
            if best_move == 0 {
                continue;
            }

            let entry = PolicyCacheEntry {
                best_move,
                alt_move_1: u16::from_le_bytes([record[10], record[11]]),
                alt_move_2: u16::from_le_bytes([record[12], record[13]]),
                score_cp: i16::from_le_bytes([record[14], record[15]]),
                searched_depth: record[16],
                samples: u16::from_le_bytes([record[17], record[18]]),
                confidence: record[19],
            };
            self.policy_cache.insert(key, entry);
        }
    }

    #[inline]
    fn lookup_opening_book(&self, hash: u64) -> u16 {
        if !self.config.enable_opening_book || self.opening_book.is_empty() {
            return 0;
        }
        *self.opening_book.get(&hash).unwrap_or(&0)
    }

    #[inline]
    fn lookup_policy_cache(&self, hash: u64) -> Option<PolicyCacheEntry> {
        if !self.config.enable_opening_book || self.policy_cache.is_empty() {
            return None;
        }
        self.policy_cache.get(&hash).copied()
    }

    fn extract_pv(&self, mut position: Position, max_len: i32, out_pv: &mut Vec<Move>) {
        out_pv.clear();
        if max_len <= 0 {
            return;
        }

        for _ in 0..max_len {
            let encoded = self.tt.best_move(position.hash);
            if encoded == 0 {
                break;
            }

            let mv = Move::decode(encoded);
            let mut undo = Undo::default();
            if !position.make_move(mv, &mut undo) {
                break;
            }

            out_pv.push(mv);
        }
    }

    fn store_pv_replies(&mut self, root_position: &Position, pv: &[Move]) {
        if !self.config.enable_pv_precalc || !self.config.enable_reply_cache || pv.is_empty() {
            return;
        }

        self.store_reply_move(root_position.hash, pv[0].encode());

        let mut position = root_position.clone();
        for (i, mv) in pv.iter().copied().enumerate() {
            let mut undo = Undo::default();
            if !position.make_move(mv, &mut undo) {
                break;
            }

            let future_ply = position.turn as i32;
            if future_ply > self.config.precalc_ply_limit {
                break;
            }

            if (i & 1) == 1 && (i + 1) < pv.len() {
                self.store_reply_move(position.hash, pv[i + 1].encode());
            }
        }
    }

    fn store_pv_subtree_reuse(&mut self, root_position: &Position, pv: &[Move], root_depth: i32) {
        if !self.config.enable_subtree_reuse || pv.is_empty() {
            return;
        }

        self.store_subtree_reuse(root_position.hash, pv[0].encode(), root_depth);

        let mut position = root_position.clone();
        for (i, mv) in pv.iter().copied().enumerate() {
            let mut undo = Undo::default();
            if !position.make_move(mv, &mut undo) {
                break;
            }

            let remaining_depth = root_depth - (i as i32 + 1);
            if remaining_depth < self.config.subtree_reuse_depth_min {
                break;
            }

            if (i & 1) == 1 && (i + 1) < pv.len() {
                self.store_subtree_reuse(position.hash, pv[i + 1].encode(), remaining_depth);
            }
        }
    }

    fn static_eval(&mut self, position: &mut Position, perspective: u8, depth_hint: i32) -> i32 {
        self.stats.eval_calls += 1;
        evaluate_hce(position, perspective, depth_hint)
    }

    fn has_quick_one_move_connect(
        &mut self,
        position: &mut Position,
        player: u8,
        checks: i32,
    ) -> bool {
        let piece_count = if player == RED {
            position.red_count
        } else {
            position.blue_count
        };
        if piece_count > 8 {
            return false;
        }

        let mut moves = Vec::new();
        generate_moves(position, player, &mut moves);
        if moves.is_empty() {
            return false;
        }

        let opp = opponent(player);
        let center = center_table();

        moves.sort_by(|a, b| {
            let ac = if piece_owner(position.board[a.to as usize]) == opp {
                1_i32
            } else {
                0_i32
            };
            let bc = if piece_owner(position.board[b.to as usize]) == opp {
                1_i32
            } else {
                0_i32
            };
            if ac != bc {
                return bc.cmp(&ac);
            }
            center[b.to as usize].cmp(&center[a.to as usize])
        });

        for mv in moves.into_iter().take(checks as usize) {
            let mut undo = Undo::default();
            let saved = position.player_to_move;
            position.player_to_move = player;
            if !position.make_move(mv, &mut undo) {
                position.player_to_move = saved;
                continue;
            }

            let connected = position.is_connected(player);
            position.unmake_move(&undo);
            position.player_to_move = saved;

            if connected {
                return true;
            }
        }

        false
    }

    #[inline]
    fn is_quiet(&self, position: &Position, mv: Move, player: u8) -> bool {
        piece_owner(position.board[mv.to as usize]) != opponent(player)
    }

    fn score_move(
        &self,
        position: &Position,
        mv: Move,
        player: u8,
        tt_move: u16,
        killer1: u16,
        killer2: u16,
        counter_move: u16,
        ply: i32,
    ) -> i32 {
        let encoded = mv.encode();
        let opp = opponent(player);
        let center = center_table();
        let is_capture = piece_owner(position.board[mv.to as usize]) == opp;

        let mut score = 0_i32;

        if tt_move != 0 && tt_move == encoded {
            score += 10_000_000;
        }

        if is_capture {
            score += 5_000_000 + 50_000 * piece_value(position.board[mv.to as usize]) as i32;
        }

        if killer1 != 0 && killer1 == encoded {
            score += 2_000_000;
        } else if killer2 != 0 && killer2 == encoded {
            score += 1_500_000;
        }

        if counter_move != 0 && counter_move == encoded {
            score += 1_200_000;
        }

        if is_capture {
            score += 950 * local_connectivity_swing(position, mv, player);
        } else if ply <= 2 {
            score += 350 * local_connectivity_swing(position, mv, player);
        }
        score += self.history[mv.from as usize][mv.to as usize];
        score += 180 * center[mv.to as usize];

        score -= mv.from as i32 * 3;
        score -= mv.to as i32;
        score -= ply;

        score
    }

    fn order_moves(
        &self,
        position: &Position,
        moves: &mut [Move],
        player: u8,
        tt_move: u16,
        killer1: u16,
        killer2: u16,
        counter_move: u16,
        ply: i32,
    ) {
        let mut scored: Vec<(i32, Move)> = Vec::with_capacity(moves.len());

        for mv in moves.iter().copied() {
            scored.push((
                self.score_move(
                    position,
                    mv,
                    player,
                    tt_move,
                    killer1,
                    killer2,
                    counter_move,
                    ply,
                ),
                mv,
            ));
        }

        scored.sort_unstable_by(|a, b| b.0.cmp(&a.0));

        for (idx, (_, mv)) in scored.into_iter().enumerate() {
            moves[idx] = mv;
        }
    }

    #[inline]
    fn adjust_history(&mut self, from: usize, to: usize, delta: i32) {
        let value = self.history[from][to].saturating_add(delta);
        self.history[from][to] = value.clamp(-300_000, 300_000);
    }

    #[inline]
    fn is_tactical_quiet(&self, position: &Position, mv: Move, player: u8, depth: i32) -> bool {
        if !self.is_quiet(position, mv, player) {
            return false;
        }
        let center = center_table();
        if center[mv.to as usize] >= 14 {
            return true;
        }

        depth <= 3 && local_connectivity_swing(position, mv, player) >= 2
    }

    pub fn search(&mut self, position: Position, deadline_ns: u64) -> SearchResult {
        self.search_with_root_moves(position, deadline_ns, None)
    }

    pub fn search_with_root_moves(
        &mut self,
        mut position: Position,
        deadline_ns: u64,
        root_legal_moves: Option<&[Move]>,
    ) -> SearchResult {
        init_tables();
        position.recompute_caches();
        self.reset_game_memory_if_needed(&position);
        self.push_recent_hash(position.hash);

        self.stats = SearchStats::default();

        if self.config.decay_histories {
            self.decay_histories();
        }
        if !self.config.preserve_tt_across_moves {
            self.tt.clear();
        }
        self.tt.new_search();

        let mut timer = TimeManager::new(deadline_ns);

        let mut generated_root_moves = Vec::new();
        generate_moves(
            &position,
            position.player_to_move,
            &mut generated_root_moves,
        );
        let mut root_moves = generated_root_moves.clone();

        if let Some(external_root) = root_legal_moves {
            let mut legal_map: HashMap<u16, Move> =
                HashMap::with_capacity(generated_root_moves.len());
            for mv in generated_root_moves.iter().copied() {
                legal_map.insert(mv.encode(), mv);
            }

            let mut filtered = Vec::with_capacity(external_root.len());
            for mv in external_root.iter().copied() {
                let encoded = mv.encode();
                if let Some(canonical) = legal_map.get(&encoded).copied() {
                    filtered.push(canonical);
                }
            }

            if !filtered.is_empty() {
                root_moves = filtered;
            }
        }

        let legal_root_count = root_moves.len();
        let team = position.player_to_move;
        let mut iteration_traces: Vec<IterationTrace> = Vec::new();

        let mut policy_forced_move = 0_u16;
        let mut policy_hint_move = 0_u16;
        if self.config.enable_opening_book
            && position.turn as i32 <= self.config.policy_cache_turn_max
        {
            self.ensure_policy_cache_loaded();
            if let Some(entry) = self.lookup_policy_cache(position.hash) {
                let enough_samples = entry.samples as i32 >= max(1, self.config.book_min_samples);
                if enough_samples {
                    let confidence = entry.confidence as i32;
                    if confidence >= self.config.book_force_confidence
                        && promote_move_front(&mut root_moves, entry.best_move)
                    {
                        policy_forced_move = entry.best_move;
                        self.stats.book_forced_hits += 1;
                        self.root_best_move = Move::decode(policy_forced_move);
                        self.root_best_score = entry.score_cp as i32;
                        self.stats.completed_depth = entry.searched_depth as i32;
                    } else if confidence >= self.config.book_hint_confidence {
                        let mut hinted = false;
                        hinted |= promote_move_front(&mut root_moves, entry.best_move);
                        hinted |= promote_move_front(&mut root_moves, entry.alt_move_1);
                        hinted |= promote_move_front(&mut root_moves, entry.alt_move_2);
                        if hinted {
                            policy_hint_move = entry.best_move;
                            self.stats.book_hint_hits += 1;
                        }
                    }
                }
            }
        }

        let mut book_hint_move = 0_u16;
        if self.config.enable_opening_book && position.turn as i32 <= self.config.book_max_ply {
            self.ensure_opening_book_loaded();
            let candidate = self.lookup_opening_book(position.hash);
            if candidate != 0 && promote_move_front(&mut root_moves, candidate) {
                book_hint_move = candidate;
                self.stats.book_hits += 1;
            }
        }

        let mut subtree_hint_move =
            self.lookup_subtree_reuse(position.hash, self.config.subtree_reuse_depth_min);
        if subtree_hint_move != 0 && promote_move_front(&mut root_moves, subtree_hint_move) {
            self.stats.subtree_reuse_hits += 1;
        } else {
            subtree_hint_move = 0;
        }

        let mut reply_hint_move = self.lookup_reply_move(position.hash);
        if reply_hint_move != 0 && promote_move_front(&mut root_moves, reply_hint_move) {
            self.stats.reply_cache_hits += 1;
        } else {
            reply_hint_move = 0;
        }

        if root_moves.is_empty() {
            return SearchResult {
                has_move: false,
                best_move: Move::default(),
                score: -WIN_SCORE,
                depth: 0,
                elapsed_ns: timer.elapsed_ns(),
                legal_root_count,
                team,
                iterations: iteration_traces,
                stats: self.stats.clone(),
            };
        }

        if policy_forced_move != 0 {
            let mut result = SearchResult {
                has_move: true,
                best_move: Move::decode(policy_forced_move),
                score: clamp_score(self.root_best_score),
                depth: self.stats.completed_depth,
                elapsed_ns: timer.elapsed_ns(),
                legal_root_count,
                team,
                iterations: iteration_traces,
                stats: self.stats.clone(),
            };
            self.stats.nodes = timer.nodes();
            result.stats.nodes = self.stats.nodes;
            return result;
        }

        self.root_best_move = root_moves[0];
        self.root_best_score = -MATE_SCORE;

        let mut previous_score = 0_i32;
        let mut recent_iteration_costs: Vec<u64> = Vec::new();
        let mut recent_fail_events = 0_i32;
        let mut recent_best_move_changes = 0_i32;

        for depth in 1..=self.config.max_depth {
            if depth > 1
                && !timer.can_start_next_iteration(
                    recent_iteration_costs.as_slice(),
                    recent_fail_events,
                    recent_best_move_changes,
                )
            {
                break;
            }

            let fail_before = self.stats.fail_high + self.stats.fail_low;
            let changes_before = self.stats.best_move_changes;
            let iter_start = timer.elapsed_ns();
            let nodes_before = timer.nodes();
            let tt_hits_before = self.stats.tt_hits;

            let mut alpha = -MATE_SCORE;
            let mut beta = MATE_SCORE;
            let mut asp = self.config.aspiration_window;

            if depth >= 3 {
                alpha = max(-MATE_SCORE, previous_score - asp);
                beta = min(MATE_SCORE, previous_score + asp);
            }

            let mut iteration_best = self.root_best_move;
            let mut iteration_score = -MATE_SCORE;

            let mut aspiration_done = false;
            while !aspiration_done {
                let mut ordered = root_moves.clone();
                let tt_root_move = self.tt.best_move(position.hash);
                self.order_moves(
                    &position,
                    &mut ordered,
                    position.player_to_move,
                    tt_root_move,
                    self.killers[0][0],
                    self.killers[0][1],
                    0,
                    0,
                );

                if reply_hint_move != 0 && reply_hint_move != tt_root_move {
                    promote_move_front(&mut ordered, reply_hint_move);
                }
                if subtree_hint_move != 0 && subtree_hint_move != tt_root_move {
                    promote_move_front(&mut ordered, subtree_hint_move);
                }
                if book_hint_move != 0 && book_hint_move != tt_root_move {
                    promote_move_front(&mut ordered, book_hint_move);
                }
                if policy_hint_move != 0 && policy_hint_move != tt_root_move {
                    promote_move_front(&mut ordered, policy_hint_move);
                }

                let mut root_move_limit = ordered.len();
                if depth >= 10 && recent_best_move_changes <= 1 {
                    root_move_limit = min(root_move_limit, 20);
                }
                if depth >= 12 && recent_best_move_changes == 0 {
                    root_move_limit = min(root_move_limit, 14);
                }
                if depth >= 14 && recent_best_move_changes == 0 {
                    root_move_limit = min(root_move_limit, 10);
                }
                if depth >= 16 && recent_best_move_changes == 0 {
                    root_move_limit = min(root_move_limit, 7);
                }

                let mut local_best = -MATE_SCORE;
                let mut local_best_move = ordered[0];
                let mut local_alpha = alpha;
                let mut incumbent_score = -MATE_SCORE;

                let player = position.player_to_move;
                let opp = opponent(player);
                let opp_threat = false;

                for (idx, mv) in ordered.iter().copied().take(root_move_limit).enumerate() {
                    timer.tick();
                    if timer.timed_out() {
                        break;
                    }

                    let quiet = self.is_quiet(&position, mv, player);
                    let tactical_quiet = self.is_tactical_quiet(&position, mv, player, depth);

                    let mut undo = Undo::default();
                    if !position.make_move(mv, &mut undo) {
                        continue;
                    }

                    let mut extension = 0_i32;
                    if depth <= 2
                        && (position.red_count as i32 + position.blue_count as i32) <= 14
                        && position.is_connected(player)
                    {
                        extension = 1;
                    }
                    if depth <= 3
                        && opp_threat
                        && !self.has_quick_one_move_connect(&mut position, opp, 3)
                    {
                        extension = max(extension, 1);
                    }

                    let child_depth = max(0, depth - 1 + extension);

                    let score = if idx == 0 {
                        -self.search_node::<true>(
                            &mut position,
                            child_depth,
                            -beta,
                            -local_alpha,
                            1,
                            false,
                            true,
                            mv.encode(),
                            &mut timer,
                        )
                    } else {
                        let mut score = -self.search_node::<false>(
                            &mut position,
                            child_depth,
                            -local_alpha - 1,
                            -local_alpha,
                            1,
                            true,
                            true,
                            mv.encode(),
                            &mut timer,
                        );
                        if score > local_alpha && score < beta {
                            score = -self.search_node::<true>(
                                &mut position,
                                child_depth,
                                -beta,
                                -local_alpha,
                                1,
                                false,
                                true,
                                mv.encode(),
                                &mut timer,
                            );
                        }
                        score
                    };

                    position.unmake_move(&undo);

                    if timer.timed_out() {
                        break;
                    }

                    if score > local_best {
                        local_best = score;
                        local_best_move = mv;
                    }
                    if mv == self.root_best_move {
                        incumbent_score = score;
                    }

                    if score > local_alpha {
                        local_alpha = score;
                    } else if quiet {
                        self.adjust_history(mv.from as usize, mv.to as usize, -max(1, depth / 3));
                    }

                    if local_alpha >= beta {
                        if quiet {
                            self.killers[0][1] = self.killers[0][0];
                            self.killers[0][0] = mv.encode();
                            let mut bonus = depth * depth;
                            if tactical_quiet {
                                bonus += depth * depth / 2;
                            }
                            self.adjust_history(mv.from as usize, mv.to as usize, bonus);
                        }
                        break;
                    }
                }

                if timer.timed_out() {
                    break;
                }

                iteration_best = local_best_move;
                iteration_score = local_best;
                if depth >= 8
                    && iteration_best != self.root_best_move
                    && incumbent_score > -MATE_SCORE
                {
                    let hysteresis = 12 + depth * 2;
                    if iteration_score < incumbent_score + hysteresis {
                        iteration_best = self.root_best_move;
                        iteration_score = incumbent_score;
                    }
                }

                if iteration_score <= alpha {
                    self.stats.fail_low += 1;
                    asp *= 2;
                    alpha = max(-MATE_SCORE, iteration_score - asp);
                    beta = min(beta, alpha + asp * 2);
                    continue;
                }

                if iteration_score >= beta {
                    self.stats.fail_high += 1;
                    asp *= 2;
                    beta = min(MATE_SCORE, iteration_score + asp);
                    alpha = max(alpha, beta - asp * 2);
                    continue;
                }

                aspiration_done = true;
            }

            if timer.timed_out() {
                break;
            }

            if iteration_best != self.root_best_move {
                self.stats.best_move_changes += 1;
            }

            self.root_best_move = iteration_best;
            self.root_best_score = iteration_score;
            previous_score = iteration_score;

            root_moves.retain(|mv| *mv != self.root_best_move);
            root_moves.insert(0, self.root_best_move);

            self.stats.completed_depth = depth;
            let iter_elapsed = timer.elapsed_ns().saturating_sub(iter_start);
            let iter_nodes = timer.nodes().saturating_sub(nodes_before);
            let iter_tt_hits = self.stats.tt_hits.saturating_sub(tt_hits_before);
            let iter_nps = if iter_elapsed > 0 {
                (iter_nodes.saturating_mul(1_000_000_000)) / iter_elapsed
            } else {
                0
            };
            iteration_traces.push(IterationTrace {
                depth,
                score: iteration_score,
                nodes_delta: iter_nodes,
                tt_hits_delta: iter_tt_hits,
                elapsed_ns_delta: iter_elapsed,
                nps_iter: iter_nps,
            });

            let measured_iter_ns = max(1, iter_elapsed as i32) as u64;
            recent_iteration_costs.push(measured_iter_ns);
            if recent_iteration_costs.len() > 6 {
                recent_iteration_costs.remove(0);
            }
            recent_fail_events = max(
                0,
                (self.stats.fail_high + self.stats.fail_low) - fail_before,
            );
            recent_best_move_changes = max(0, self.stats.best_move_changes - changes_before);

            if self.root_best_score.abs() >= MATE_SCORE - 128 {
                break;
            }
        }

        let mut result = SearchResult {
            has_move: true,
            best_move: self.root_best_move,
            score: clamp_score(self.root_best_score),
            depth: self.stats.completed_depth,
            elapsed_ns: timer.elapsed_ns(),
            legal_root_count,
            team,
            iterations: iteration_traces,
            stats: self.stats.clone(),
        };

        self.stats.nodes = timer.nodes();
        result.stats.nodes = self.stats.nodes;

        if result.has_move {
            self.tt.store(
                position.hash,
                max(1, result.depth),
                self.root_best_score,
                Bound::Exact,
                self.root_best_move.encode(),
            );
        }

        if result.has_move && (self.config.enable_pv_precalc || self.config.enable_subtree_reuse) {
            let mut pv = Vec::new();
            let pv_len = max(self.config.precalc_pv_length, self.config.book_max_ply * 2);
            self.extract_pv(position.clone(), max(2, pv_len), &mut pv);
            if pv.is_empty() {
                pv.push(self.root_best_move);
            }

            if self.config.enable_pv_precalc
                && position.turn as i32 <= self.config.precalc_ply_limit
            {
                self.store_pv_replies(&position, &pv);
            }
            if self.config.enable_subtree_reuse {
                self.store_pv_subtree_reuse(&position, &pv, max(1, result.depth));
            }
        }

        result
    }

    #[allow(clippy::too_many_arguments)]
    fn search_node<const PV: bool>(
        &mut self,
        position: &mut Position,
        depth: i32,
        mut alpha: i32,
        beta: i32,
        ply: i32,
        cut_node: bool,
        allow_null: bool,
        prev_move: u16,
        timer: &mut TimeManager,
    ) -> i32 {
        timer.tick();
        if timer.timed_out() {
            return self.static_eval(position, position.player_to_move, depth);
        }

        if ply >= (MAX_PLY - 1) as i32 {
            return self.static_eval(position, position.player_to_move, depth);
        }

        let player = position.player_to_move;
        let opp = opponent(player);

        let round_connect_outcome = round_end_connection_outcome(position);
        if round_connect_outcome == RED || round_connect_outcome == BLUE {
            return if round_connect_outcome == player {
                MATE_SCORE - ply
            } else {
                -MATE_SCORE + ply
            };
        }
        if round_connect_outcome == ROUND_CONNECT_BOTH {
            return terminal_swarm_score(position, player, ply);
        }

        if position.turn >= 60 {
            return terminal_swarm_score(position, player, ply);
        }

        let own_count = if player == RED {
            position.red_count as i32
        } else {
            position.blue_count as i32
        };
        let opp_count = if opp == RED {
            position.red_count as i32
        } else {
            position.blue_count as i32
        };

        if own_count == 0 && opp_count > 0 {
            self.stats.no_move_terminal_hits += 1;
            return -MATE_SCORE + ply;
        }
        if opp_count == 0 && own_count > 0 {
            return MATE_SCORE - ply;
        }

        if depth <= 0 {
            return self.quiescence(position, alpha, beta, ply, 0, timer);
        }

        let key = position.hash;
        let repeat_distance = if self.config.enable_anti_shuffle && ply > 0 {
            self.recent_hash_distance(key, self.config.anti_shuffle_window)
        } else {
            0
        };

        if repeat_distance > 0 {
            self.stats.anti_shuffle_hits += 1;
            let adaptive_penalty = self.config.anti_shuffle_penalty
                + max(0, self.config.anti_shuffle_window - repeat_distance) * 3
                + min(20, ply * 2);
            return self.static_eval(position, player, depth) - adaptive_penalty;
        }

        self.stats.tt_probes += 1;
        let (tt_hit, tt_score, mut tt_move) = self.tt.probe(key, depth, alpha, beta);
        if tt_hit {
            self.stats.tt_hits += 1;
            return tt_score;
        }
        if tt_move == 0 {
            tt_move = self.tt.best_move(key);
        }

        let static_eval_score = self.static_eval(position, player, depth);

        if !PV
            && depth <= self.config.reverse_futility_depth
            && static_eval_score - self.config.reverse_futility_margin * depth >= beta
        {
            if !has_legal_move(position, player) {
                self.stats.no_move_terminal_hits += 1;
                return -MATE_SCORE + ply;
            }
            return static_eval_score;
        }

        if allow_null
            && !PV
            && depth >= self.config.null_move_min_depth
            && own_count > self.config.null_move_min_pieces
            && static_eval_score >= beta - (90 + depth * 8)
        {
            let own_components = position.component_count(player);
            let opp_components = position.component_count(opp);

            if own_components > 2 && opp_components > 2 {
                let mut nu = NullUndo::default();
                position.make_null_move(&mut nu);
                let reduction = self.config.null_move_reduction + depth / 4;
                let nm_depth = max(0, depth - 1 - reduction);
                let score = -self.search_node::<false>(
                    position,
                    nm_depth,
                    -beta,
                    -beta + 1,
                    ply + 1,
                    true,
                    false,
                    0,
                    timer,
                );
                position.unmake_null_move(&nu);

                if score >= beta {
                    let can_verify = self.config.enable_verification_search
                        && depth
                            >= self.config.null_move_min_depth
                                + max(1, self.config.verification_depth_margin);

                    if !can_verify {
                        if !has_legal_move(position, player) {
                            self.stats.no_move_terminal_hits += 1;
                            return -MATE_SCORE + ply;
                        }
                        return score;
                    }

                    let verify_depth = max(0, depth - max(1, reduction - 1));
                    let before_nodes = timer.nodes();
                    let verify = self.search_node::<false>(
                        position,
                        verify_depth,
                        beta - 1,
                        beta,
                        ply,
                        false,
                        false,
                        prev_move,
                        timer,
                    );
                    let after_nodes = timer.nodes();
                    if after_nodes > before_nodes {
                        self.stats.verification_nodes += after_nodes - before_nodes;
                    }
                    if verify >= beta {
                        if !has_legal_move(position, player) {
                            self.stats.no_move_terminal_hits += 1;
                            return -MATE_SCORE + ply;
                        }
                        return verify;
                    }
                }
            }
        }

        let mut moves = Vec::new();
        generate_moves(position, player, &mut moves);
        if moves.is_empty() {
            self.stats.no_move_terminal_hits += 1;
            return -MATE_SCORE + ply;
        }

        let killer_idx = min(ply as usize, MAX_PLY - 1);
        let killer1 = self.killers[killer_idx][0];
        let killer2 = self.killers[killer_idx][1];
        let mut counter = 0_u16;
        if prev_move != 0 {
            let prev = Move::decode(prev_move);
            counter = self.counter_moves[prev.from as usize][prev.to as usize];
        }

        self.order_moves(
            position, &mut moves, player, tt_move, killer1, killer2, counter, ply,
        );

        if !PV
            && cut_node
            && depth >= self.config.multicut_depth
            && static_eval_score >= beta - 260
            && moves.len() >= 10
        {
            let mut hits = 0_i32;
            let mut dynamic_required = self.config.multicut_required;
            let mut dynamic_test_moves = self.config.multicut_moves;
            if static_eval_score >= beta - 80 {
                dynamic_required = max(1, dynamic_required - 1);
                dynamic_test_moves += 2;
            }
            if beta - alpha > 200 {
                dynamic_required += 1;
                dynamic_test_moves = max(4, dynamic_test_moves - 2);
            }

            let test_moves = min(dynamic_test_moves as usize, moves.len());
            let mc_depth = max(0, depth - 1 - self.config.multicut_reduction);

            for mv in moves.iter().copied().take(test_moves) {
                let mut undo = Undo::default();
                if !position.make_move(mv, &mut undo) {
                    continue;
                }

                let score = -self.search_node::<false>(
                    position,
                    mc_depth,
                    -beta,
                    -beta + 1,
                    ply + 1,
                    true,
                    true,
                    mv.encode(),
                    timer,
                );
                position.unmake_move(&undo);

                if score >= beta {
                    hits += 1;
                    if hits >= dynamic_required {
                        return beta;
                    }
                }

                if timer.timed_out() {
                    break;
                }
            }
        }

        let original_alpha = alpha;
        let mut best_score = -MATE_SCORE;
        let mut best_move = moves[0];

        let opp_threat = false;

        for (idx, mv) in moves.iter().copied().enumerate() {
            if timer.timed_out() {
                break;
            }

            let quiet = self.is_quiet(position, mv, player);
            let tactical_quiet = self.is_tactical_quiet(position, mv, player, depth);

            if !PV && quiet {
                if depth <= self.config.futility_depth + 1
                    && idx >= 3
                    && static_eval_score + self.config.futility_margin * depth <= alpha
                {
                    continue;
                }

                if depth <= 9 && idx as i32 >= self.config.lmp_base + self.config.lmp_scale * depth
                {
                    break;
                }
            }

            let mut extension = 0_i32;

            if self.config.enable_singular_extension
                && depth >= self.config.singular_depth_min
                && idx == 0
                && tt_move != 0
                && mv.encode() == tt_move
                && moves.len() >= 2
            {
                let singular_margin = 60 + depth * 6;
                let singular_beta = max(-MATE_SCORE + 1, alpha - singular_margin);
                let singular_depth = max(0, depth / 2 - 1);
                let mut singular = true;
                let probe_count = min(6, moves.len());

                for alt in moves.iter().copied().take(probe_count).skip(1) {
                    let mut alt_undo = Undo::default();
                    if !position.make_move(alt, &mut alt_undo) {
                        continue;
                    }

                    let alt_score = -self.search_node::<false>(
                        position,
                        singular_depth,
                        -singular_beta - 1,
                        -singular_beta,
                        ply + 1,
                        true,
                        false,
                        alt.encode(),
                        timer,
                    );
                    position.unmake_move(&alt_undo);

                    if alt_score >= singular_beta || timer.timed_out() {
                        singular = false;
                        break;
                    }
                }

                if singular {
                    extension = 1;
                    self.stats.singular_extensions += 1;
                }
            }

            let mut undo = Undo::default();
            if !position.make_move(mv, &mut undo) {
                continue;
            }

            let mut anti_shuffle_penalty = 0_i32;
            if self.config.enable_anti_shuffle && quiet {
                let anti_shuffle_distance =
                    self.recent_hash_distance(position.hash, self.config.anti_shuffle_window);
                if anti_shuffle_distance > 0 {
                    anti_shuffle_penalty = self.config.anti_shuffle_penalty
                        + max(0, self.config.anti_shuffle_window - anti_shuffle_distance) * 2;
                }
            }
            if anti_shuffle_penalty > 0 {
                self.stats.anti_shuffle_hits += 1;
            }

            if depth <= 2 && own_count <= 10 && position.is_connected(player) {
                extension = max(extension, 1);
            }
            if depth <= 3 && opp_threat && !self.has_quick_one_move_connect(position, opp, 2) {
                extension = max(extension, 1);
            }

            let full_depth = max(0, depth - 1 + extension);

            let mut reduction = 0_i32;
            if !PV
                && quiet
                && depth >= self.config.lmr_min_depth
                && idx as i32 >= self.config.lmr_min_index
            {
                reduction = 1 + (idx as i32 / 2) + depth / 2;
                if tactical_quiet {
                    reduction = reduction.saturating_sub(1);
                } else if idx > 8 {
                    reduction += 1;
                }
                if depth >= 8 && idx >= 3 {
                    reduction += 1;
                }
                if depth >= 10 && idx >= 5 {
                    reduction += 1;
                }
                reduction = min(reduction, max(0, full_depth - 1));
            }

            let mut score = if idx == 0 {
                if PV {
                    -self.search_node::<true>(
                        position,
                        full_depth,
                        -beta,
                        -alpha,
                        ply + 1,
                        false,
                        true,
                        mv.encode(),
                        timer,
                    )
                } else {
                    -self.search_node::<false>(
                        position,
                        full_depth,
                        -beta,
                        -alpha,
                        ply + 1,
                        true,
                        true,
                        mv.encode(),
                        timer,
                    )
                }
            } else {
                let reduced_depth = max(0, full_depth - reduction);
                let mut score = -self.search_node::<false>(
                    position,
                    reduced_depth,
                    -alpha - 1,
                    -alpha,
                    ply + 1,
                    true,
                    true,
                    mv.encode(),
                    timer,
                );

                if score > alpha && score < beta {
                    score = if PV {
                        -self.search_node::<true>(
                            position,
                            full_depth,
                            -beta,
                            -alpha,
                            ply + 1,
                            false,
                            true,
                            mv.encode(),
                            timer,
                        )
                    } else {
                        -self.search_node::<false>(
                            position,
                            full_depth,
                            -beta,
                            -alpha,
                            ply + 1,
                            true,
                            true,
                            mv.encode(),
                            timer,
                        )
                    };
                }

                score
            };

            position.unmake_move(&undo);

            if anti_shuffle_penalty > 0 {
                score -= anti_shuffle_penalty;
            }

            if score > best_score {
                best_score = score;
                best_move = mv;
            }

            if score > alpha {
                alpha = score;
                if prev_move != 0 {
                    let prev = Move::decode(prev_move);
                    self.counter_moves[prev.from as usize][prev.to as usize] = mv.encode();
                }
            } else if quiet {
                self.adjust_history(mv.from as usize, mv.to as usize, -max(1, depth / 2));
            }

            if alpha >= beta {
                if quiet {
                    let killer_idx = min(ply as usize, MAX_PLY - 1);
                    self.killers[killer_idx][1] = self.killers[killer_idx][0];
                    self.killers[killer_idx][0] = mv.encode();
                    let mut bonus = depth * depth;
                    if tactical_quiet {
                        bonus += depth * depth / 2;
                    }
                    self.adjust_history(mv.from as usize, mv.to as usize, bonus);
                }
                break;
            }
        }

        if best_score <= -MATE_SCORE {
            return static_eval_score;
        }

        let bound = if best_score <= original_alpha {
            Bound::Upper
        } else if best_score >= beta {
            Bound::Lower
        } else {
            Bound::Exact
        };

        self.tt
            .store(key, depth, best_score, bound, best_move.encode());
        best_score
    }

    fn quiescence(
        &mut self,
        position: &mut Position,
        mut alpha: i32,
        beta: i32,
        ply: i32,
        qdepth: i32,
        timer: &mut TimeManager,
    ) -> i32 {
        timer.tick();
        self.stats.qnodes += 1;

        if timer.timed_out() {
            return self.static_eval(position, position.player_to_move, 3);
        }

        let player = position.player_to_move;
        let opp = opponent(player);

        let round_connect_outcome = round_end_connection_outcome(position);
        if round_connect_outcome == RED || round_connect_outcome == BLUE {
            return if round_connect_outcome == player {
                MATE_SCORE - ply
            } else {
                -MATE_SCORE + ply
            };
        }
        if round_connect_outcome == ROUND_CONNECT_BOTH {
            return terminal_swarm_score(position, player, ply);
        }

        if position.turn >= 60 {
            return terminal_swarm_score(position, player, ply);
        }

        if qdepth == 0 && !has_legal_move(position, player) {
            self.stats.no_move_terminal_hits += 1;
            return -MATE_SCORE + ply;
        }

        let stand_pat = self.static_eval(position, player, 3);
        if stand_pat >= beta {
            return stand_pat;
        }
        if stand_pat > alpha {
            alpha = stand_pat;
        }

        if qdepth >= self.config.qsearch_depth_cap {
            return stand_pat;
        }

        let mut noisy: Vec<(i32, Move)> = Vec::new();

        let mut captures = Vec::new();
        generate_capture_moves(position, player, &mut captures);
        noisy.reserve(captures.len() + 24);

        let center = center_table();

        for mv in captures {
            let cap_value = piece_value(position.board[mv.to as usize]) as i32;
            let swing = if qdepth <= 1 {
                local_connectivity_swing(position, mv, player)
            } else {
                0
            };
            noisy.push((
                3_000_000 + cap_value * 120_000 + swing * 4_000 + center[mv.to as usize],
                mv,
            ));
        }

        if qdepth == 0 {
            let mut all_moves = Vec::new();
            generate_moves(position, player, &mut all_moves);

            for mv in all_moves {
                if piece_owner(position.board[mv.to as usize]) == opp {
                    continue;
                }

                let swing = local_connectivity_swing(position, mv, player);
                if swing <= 0 {
                    continue;
                }

                noisy.push((900_000 + swing * 9_000 + center[mv.to as usize], mv));
            }
        }

        if noisy.is_empty() {
            if qdepth > 0 && !has_legal_move(position, player) {
                self.stats.no_move_terminal_hits += 1;
                return -MATE_SCORE + ply;
            }
            return stand_pat;
        }

        noisy.sort_by(|a, b| {
            if a.0 != b.0 {
                return b.0.cmp(&a.0);
            }
            if a.1.to != b.1.to {
                return b.1.to.cmp(&a.1.to);
            }
            b.1.from.cmp(&a.1.from)
        });

        let mut noisy_top_k = self.config.qsearch_top_k + if qdepth == 0 { 6 } else { 0 };
        noisy_top_k -= qdepth * 6;
        let noisy_top_k = max(4, noisy_top_k) as usize;
        if noisy.len() > noisy_top_k {
            noisy.truncate(noisy_top_k);
        }

        for (_, mv) in noisy {
            let capture = piece_owner(position.board[mv.to as usize]) == opp;
            let adaptive_delta_margin = max(
                30,
                self.config.q_delta_margin - qdepth * 24 + if qdepth == 0 { 32 } else { 0 },
            );
            if !capture && stand_pat + adaptive_delta_margin < alpha {
                continue;
            }

            let mut undo = Undo::default();
            if !position.make_move(mv, &mut undo) {
                continue;
            }

            let score = -self.quiescence(position, -beta, -alpha, ply + 1, qdepth + 1, timer);

            position.unmake_move(&undo);

            if score >= beta {
                return score;
            }
            if score > alpha {
                alpha = score;
            }

            if timer.timed_out() {
                break;
            }
        }

        alpha
    }
}
