use std::sync::OnceLock;

use socha::internal::GameState;
use socha::neutral::{PiranhaField, Size, Team};

use crate::bitboard::{flood_fill_fast, get_neighbor_masks, pop_lsb, Bitboard};

// ─── Piece encoding ──────────────────────────────────────────────────────────
pub const EMPTY: u8 = 0;
pub const SQUID: u8 = 7;

// ─── Player identifiers ───────────────────────────────────────────────────────
pub const ONE: u8 = 1;
pub const TWO: u8 = 2;

#[inline(always)]
pub fn opponent(player: u8) -> u8 {
    if player == ONE { TWO } else { ONE }
}

#[inline(always)]
pub fn is_one(p: u8) -> bool {
    p >= 1 && p <= 3
}

#[inline(always)]
pub fn is_two(p: u8) -> bool {
    p >= 4 && p <= 6
}

#[inline(always)]
pub fn is_fish(p: u8) -> bool {
    p >= 1 && p <= 6
}

#[inline(always)]
pub fn piece_owner(p: u8) -> u8 {
    if is_one(p) {
        ONE
    } else if is_two(p) {
        TWO
    } else {
        0
    }
}

#[inline(always)]
pub fn piece_value(p: u8) -> u8 {
    if is_one(p) {
        p
    } else if is_two(p) {
        p - 3
    } else {
        0
    }
}

// ─── Precomputed tables ───────────────────────────────────────────────────────

/// 8 directions: index → (dx, dy)
pub const DIRS: [(i32, i32); 8] = [
    (-1, -1), // 0
    (0, -1),  // 1
    (1, -1),  // 2
    (-1, 0),  // 3
    (1, 0),   // 4
    (-1, 1),  // 5
    (0, 1),   // 6
    (1, 1),   // 7
];

/// Which line (0=row, 1=col, 2=diag_a, 3=diag_b) each direction walks.
pub const DIR_LINE_KIND: [u8; 8] = [2, 1, 3, 0, 0, 3, 1, 2];

pub struct Tables {
    /// Packed list of valid neighbor squares per square (as i8, 0-99).
    pub neighbors: [[i8; 8]; 100],
    /// How many valid neighbors each square has.
    pub nb_count: [u8; 100],
    /// Ray: rays[sq][dir][step] = square index along that ray.
    pub rays: [[[i8; 9]; 8]; 100],
    /// Length of each ray.
    pub ray_len: [[u8; 8]; 100],
    /// Prefix bitmasks: ray_prefix[sq][dir][n] = bitmask of first n squares on the ray.
    /// Used for O(1) blocking checks: (ray_prefix[from][dir][distance-1] & opp_bb) != 0.
    pub ray_prefix: [[[Bitboard; 10]; 8]; 100],
    /// Center-distance score per square (18 - max(|2x-9|, |2y-9|)).
    pub center: [i16; 100],
    /// Zobrist hash per (piece 0-7, square 0-99).
    pub zobrist_piece: [[u64; 100]; 8],
    pub zobrist_side: u64,
}

static TABLES: OnceLock<Tables> = OnceLock::new();

pub fn get_tables() -> &'static Tables {
    TABLES.get_or_init(Tables::init)
}

fn splitmix64(state: &mut u64) -> u64 {
    *state = state.wrapping_add(0x9e3779b97f4a7c15u64);
    let mut z = *state;
    z = (z ^ (z >> 30)).wrapping_mul(0xbf58476d1ce4e5b9u64);
    z = (z ^ (z >> 27)).wrapping_mul(0x94d049bb133111ebu64);
    z ^ (z >> 31)
}

impl Tables {
    fn init() -> Self {
        let mut t = Tables {
            neighbors: [[-1i8; 8]; 100],
            nb_count: [0u8; 100],
            rays: [[[-1i8; 9]; 8]; 100],
            ray_len: [[0u8; 8]; 100],
            ray_prefix: [[[0u128; 10]; 8]; 100],
            center: [0i16; 100],
            zobrist_piece: [[0u64; 100]; 8],
            zobrist_side: 0,
        };

        for sq in 0..100usize {
            let x = (sq % 10) as i32;
            let y = (sq / 10) as i32;

            // Center score
            let dx2 = (2 * x - 9).abs();
            let dy2 = (2 * y - 9).abs();
            t.center[sq] = (18 - dx2.max(dy2)) as i16;

            for dir in 0..8usize {
                let (dx, dy) = DIRS[dir];

                // First neighbor
                let nx = x + dx;
                let ny = y + dy;
                if nx >= 0 && nx < 10 && ny >= 0 && ny < 10 {
                    let nb_sq = ny * 10 + nx;
                    t.neighbors[sq][t.nb_count[sq] as usize] = nb_sq as i8;
                    t.nb_count[sq] += 1;
                }

                // Full ray
                let mut ray_len = 0usize;
                let mut cx = x + dx;
                let mut cy = y + dy;
                while cx >= 0 && cx < 10 && cy >= 0 && cy < 10 && ray_len < 9 {
                    t.rays[sq][dir][ray_len] = (cy * 10 + cx) as i8;
                    ray_len += 1;
                    cx += dx;
                    cy += dy;
                }
                t.ray_len[sq][dir] = ray_len as u8;

                // Prefix masks: ray_prefix[sq][dir][n] = bitmask of first n ray squares.
                // ray_prefix[..][..][0] = 0 (no intermediate squares for distance-1 moves).
                let mut mask = 0u128;
                t.ray_prefix[sq][dir][0] = 0;
                for step in 0..ray_len {
                    // After this step is taken, the next prefix includes it
                    if step + 1 < 10 {
                        mask |= 1u128 << (t.rays[sq][dir][step] as usize);
                        t.ray_prefix[sq][dir][step + 1] = mask;
                    }
                }
            }
        }

        // Zobrist hashes
        let mut seed = 0x20260217u64;
        for piece in 0..8usize {
            for sq in 0..100usize {
                t.zobrist_piece[piece][sq] = splitmix64(&mut seed);
            }
        }
        t.zobrist_side = splitmix64(&mut seed);

        t
    }
}

// ─── Move ─────────────────────────────────────────────────────────────────────

#[derive(Copy, Clone, PartialEq, Eq, Default, Debug)]
pub struct Move {
    pub from: u8,
    pub to: u8,
}

impl Move {
    #[inline(always)]
    pub fn encode(self) -> u16 {
        ((self.from as u16) << 7) | (self.to as u16)
    }

    #[inline(always)]
    pub fn decode(v: u16) -> Self {
        Self {
            from: ((v >> 7) & 0x7F) as u8,
            to: (v & 0x7F) as u8,
        }
    }
}

// ─── MoveList ─────────────────────────────────────────────────────────────────

pub struct MoveList {
    pub moves: [Move; 256],
    pub len: usize,
}

impl MoveList {
    #[inline(always)]
    pub fn new() -> Self {
        Self {
            moves: [Move::default(); 256],
            len: 0,
        }
    }

    #[inline(always)]
    pub fn push(&mut self, mv: Move) {
        self.moves[self.len] = mv;
        self.len += 1;
    }

    #[inline(always)]
    pub fn as_slice(&self) -> &[Move] {
        &self.moves[..self.len]
    }
}

impl Default for MoveList {
    fn default() -> Self {
        Self::new()
    }
}

// ─── Undo ─────────────────────────────────────────────────────────────────────

#[derive(Copy, Clone, Default)]
pub struct Undo {
    pub mv: Move,
    pub captured: u8,
    pub prev_player: u8,
    pub prev_turn: u16,
    pub prev_hash: u64,
}

// ─── Position ─────────────────────────────────────────────────────────────────

pub struct Position {
    pub board: [u8; 100],
    pub fish_value: [u8; 100],

    /// Bitboards for fast piece iteration and connectivity checks.
    pub bb_one: Bitboard,    // all Team One fish
    pub bb_two: Bitboard,    // all Team Two fish
    pub bb_squids: Bitboard, // all squid squares
    pub bb_all: Bitboard,    // bb_one | bb_two | bb_squids

    pub row_counts: [u8; 10],
    pub col_counts: [u8; 10],
    pub diag_a_counts: [u8; 19], // index = x - y + 9
    pub diag_b_counts: [u8; 19], // index = x + y

    pub player: u8,
    pub turn: u16,
    pub one_count: u16,
    pub two_count: u16,
    pub one_value: u16,
    pub two_value: u16,
    pub hash: u64,
}

impl Clone for Position {
    fn clone(&self) -> Self {
        Position {
            board: self.board,
            fish_value: self.fish_value,
            bb_one: self.bb_one,
            bb_two: self.bb_two,
            bb_squids: self.bb_squids,
            bb_all: self.bb_all,
            row_counts: self.row_counts,
            col_counts: self.col_counts,
            diag_a_counts: self.diag_a_counts,
            diag_b_counts: self.diag_b_counts,
            player: self.player,
            turn: self.turn,
            one_count: self.one_count,
            two_count: self.two_count,
            one_value: self.one_value,
            two_value: self.two_value,
            hash: self.hash,
        }
    }
}

impl Default for Position {
    fn default() -> Self {
        Position {
            board: [EMPTY; 100],
            fish_value: [0; 100],
            bb_one: 0,
            bb_two: 0,
            bb_squids: 0,
            bb_all: 0,
            row_counts: [0; 10],
            col_counts: [0; 10],
            diag_a_counts: [0; 19],
            diag_b_counts: [0; 19],
            player: ONE,
            turn: 0,
            one_count: 0,
            two_count: 0,
            one_value: 0,
            two_value: 0,
            hash: 0,
        }
    }
}

impl Position {
    // ── Build from socha GameState ────────────────────────────────────────────

    pub fn from_game_state(state: &GameState) -> Self {
        let mut pos = Position::default();

        for y in 0..10usize {
            for x in 0..10usize {
                let sq = y * 10 + x;
                let field = state.board.get(x, y);
                pos.board[sq] = match field {
                    PiranhaField::Empty => EMPTY,
                    PiranhaField::Squid => SQUID,
                    PiranhaField::Fish { team, size } => {
                        let v = match size {
                            Size::S => 1u8,
                            Size::M => 2u8,
                            Size::L => 3u8,
                        };
                        match team {
                            Team::One => v,
                            Team::Two => v + 3,
                        }
                    }
                };
            }
        }

        pos.player = match state.current_team() {
            Team::One => ONE,
            Team::Two => TWO,
        };
        pos.turn = state.turn as u16;

        pos.recompute_caches();
        pos
    }

    // ── Convert internal Move → socha Move ────────────────────────────────────

    pub fn to_socha_move(&self, mv: Move) -> socha::neutral::Move {
        let from = mv.from as usize;
        let to = mv.to as usize;
        let from_x = (from % 10) as u8;
        let from_y = (from / 10) as u8;
        let to_x = (to % 10) as i32;
        let to_y = (to / 10) as i32;
        let dx = to_x - from_x as i32;
        let dy = to_y - from_y as i32;

        use socha::neutral::Direction;
        let dir = match (dx.signum(), dy.signum()) {
            (0, 1)   => Direction::UP,
            (1, 1)   => Direction::UpRight,
            (1, 0)   => Direction::Right,
            (1, -1)  => Direction::DownRight,
            (0, -1)  => Direction::Down,
            (-1, -1) => Direction::DownLeft,
            (-1, 0)  => Direction::Left,
            (-1, 1)  => Direction::UpLeft,
            _ => {
                log::error!("invalid move direction dx={} dy={}", dx, dy);
                Direction::Right // fallback
            }
        };

        socha::neutral::Move { from: (from_x, from_y), dir }
    }

    // ── Cache helpers ─────────────────────────────────────────────────────────

    #[inline(always)]
    pub fn recompute_caches(&mut self) {
        let t = get_tables();
        self.row_counts = [0; 10];
        self.col_counts = [0; 10];
        self.diag_a_counts = [0; 19];
        self.diag_b_counts = [0; 19];
        self.one_count = 0;
        self.two_count = 0;
        self.one_value = 0;
        self.two_value = 0;
        self.hash = 0;
        self.fish_value = [0; 100];
        self.bb_one = 0;
        self.bb_two = 0;
        self.bb_squids = 0;

        for sq in 0..100usize {
            let piece = self.board[sq];
            let owner = piece_owner(piece);
            let value = piece_value(piece);
            let bit = 1u128 << sq;
            self.fish_value[sq] = value;

            if owner == ONE {
                self.one_count += 1;
                self.one_value += value as u16;
                self.line_increment(sq);
                self.hash ^= t.zobrist_piece[piece as usize][sq];
                self.bb_one |= bit;
            } else if owner == TWO {
                self.two_count += 1;
                self.two_value += value as u16;
                self.line_increment(sq);
                self.hash ^= t.zobrist_piece[piece as usize][sq];
                self.bb_two |= bit;
            } else if piece == SQUID {
                self.hash ^= t.zobrist_piece[SQUID as usize][sq];
                self.bb_squids |= bit;
            }
        }
        self.bb_all = self.bb_one | self.bb_two | self.bb_squids;

        if self.player == TWO {
            self.hash ^= t.zobrist_side;
        }
    }

    #[inline(always)]
    fn line_increment(&mut self, sq: usize) {
        let x = sq % 10;
        let y = sq / 10;
        self.row_counts[y] += 1;
        self.col_counts[x] += 1;
        self.diag_a_counts[x + 9 - y] += 1;
        self.diag_b_counts[x + y] += 1;
    }

    #[inline(always)]
    fn line_decrement(&mut self, sq: usize) {
        let x = sq % 10;
        let y = sq / 10;
        self.row_counts[y] -= 1;
        self.col_counts[x] -= 1;
        self.diag_a_counts[x + 9 - y] -= 1;
        self.diag_b_counts[x + y] -= 1;
    }

    // O(1) line count for a square+direction pair.
    #[inline(always)]
    pub fn line_count(&self, sq: usize, dir: usize) -> usize {
        let x = sq % 10;
        let y = sq / 10;
        match DIR_LINE_KIND[dir] {
            0 => self.row_counts[y] as usize,
            1 => self.col_counts[x] as usize,
            2 => self.diag_a_counts[x + 9 - y] as usize,
            3 => self.diag_b_counts[x + y] as usize,
            _ => unreachable!(),
        }
    }

    // ── Make / Unmake ─────────────────────────────────────────────────────────

    pub fn make_move(&mut self, mv: Move, undo: &mut Undo) -> bool {
        let from = mv.from as usize;
        let to = mv.to as usize;

        if from >= 100 || to >= 100 {
            return false;
        }

        let moved = self.board[from];
        let captured = self.board[to];
        let moved_owner = piece_owner(moved);
        let captured_owner = piece_owner(captured);
        let moved_value = self.fish_value[from];
        let captured_value = self.fish_value[to];

        if moved_owner != self.player || !is_fish(moved) {
            return false;
        }
        if captured_owner == moved_owner || captured == SQUID {
            return false;
        }

        let t = get_tables();

        undo.mv = mv;
        undo.captured = captured;
        undo.prev_player = self.player;
        undo.prev_turn = self.turn;
        undo.prev_hash = self.hash;

        self.line_decrement(from);
        if captured_owner != 0 {
            self.line_decrement(to);
        }
        self.line_increment(to);

        self.board[from] = EMPTY;
        self.fish_value[from] = 0;
        self.board[to] = moved;
        self.fish_value[to] = moved_value;

        // Update bitboards
        let from_bit: Bitboard = 1u128 << from;
        let to_bit: Bitboard = 1u128 << to;

        if moved_owner == ONE {
            self.bb_one = (self.bb_one & !from_bit) | to_bit;
        } else {
            self.bb_two = (self.bb_two & !from_bit) | to_bit;
        }
        if captured_owner == ONE {
            self.bb_one &= !to_bit;
        } else if captured_owner == TWO {
            self.bb_two &= !to_bit;
        }
        self.bb_all = self.bb_one | self.bb_two | self.bb_squids;

        if captured_owner == ONE {
            self.one_count -= 1;
            self.one_value -= captured_value as u16;
        } else if captured_owner == TWO {
            self.two_count -= 1;
            self.two_value -= captured_value as u16;
        }

        self.hash ^= t.zobrist_piece[moved as usize][from];
        if captured_owner != 0 {
            self.hash ^= t.zobrist_piece[captured as usize][to];
        }
        self.hash ^= t.zobrist_piece[moved as usize][to];
        self.hash ^= t.zobrist_side;

        self.player = opponent(self.player);
        self.turn += 1;

        true
    }

    pub fn unmake_move(&mut self, undo: &Undo) {
        let from = undo.mv.from as usize;
        let to = undo.mv.to as usize;

        let moved = self.board[to];
        let moved_value = self.fish_value[to];
        let captured = undo.captured;
        let captured_owner = piece_owner(captured);
        let captured_value = piece_value(captured);

        self.board[from] = moved;
        self.board[to] = captured;
        self.fish_value[from] = moved_value;
        self.fish_value[to] = captured_value;

        // Update bitboards
        let from_bit: Bitboard = 1u128 << from;
        let to_bit: Bitboard = 1u128 << to;
        let moved_owner = piece_owner(moved);

        if moved_owner == ONE {
            self.bb_one = (self.bb_one & !to_bit) | from_bit;
        } else {
            self.bb_two = (self.bb_two & !to_bit) | from_bit;
        }
        if captured_owner == ONE {
            self.bb_one |= to_bit;
        } else if captured_owner == TWO {
            self.bb_two |= to_bit;
        }
        self.bb_all = self.bb_one | self.bb_two | self.bb_squids;

        self.line_decrement(to);
        if captured_owner != 0 {
            self.line_increment(to);
        }
        self.line_increment(from);

        if captured_owner == ONE {
            self.one_count += 1;
            self.one_value += captured_value as u16;
        } else if captured_owner == TWO {
            self.two_count += 1;
            self.two_value += captured_value as u16;
        }

        self.player = undo.prev_player;
        self.turn = undo.prev_turn;
        self.hash = undo.prev_hash;
    }

    pub fn make_null_move(&mut self) -> (u8, u16, u64) {
        let saved = (self.player, self.turn, self.hash);
        self.player = opponent(self.player);
        self.turn += 1;
        self.hash ^= get_tables().zobrist_side;
        saved
    }

    pub fn unmake_null_move(&mut self, saved: (u8, u16, u64)) {
        self.player = saved.0;
        self.turn = saved.1;
        self.hash = saved.2;
    }

    // ── Move generation ───────────────────────────────────────────────────────

    pub fn generate_moves(&self, out: &mut MoveList) {
        let t = get_tables();
        let (own_bb, opp_bb) = if self.player == ONE {
            (self.bb_one, self.bb_two)
        } else {
            (self.bb_two, self.bb_one)
        };
        let blocked_landing = own_bb | self.bb_squids;
        out.len = 0;

        let mut pieces = own_bb;
        while pieces != 0 {
            let from = pop_lsb(&mut pieces);

            for dir in 0..8usize {
                let distance = self.line_count(from, dir);
                if distance == 0 || distance > t.ray_len[from][dir] as usize {
                    continue;
                }

                let to = t.rays[from][dir][distance - 1] as usize;
                let to_bit: Bitboard = 1u128 << to;

                // Cannot land on own piece or squid
                if (to_bit & blocked_landing) != 0 {
                    continue;
                }

                // O(1) blocking check: any opponent on the intermediate squares?
                // ray_prefix[from][dir][distance-1] = bitmask of first (distance-1) ray squares
                if (t.ray_prefix[from][dir][distance - 1] & opp_bb) != 0 {
                    continue;
                }

                out.push(Move { from: from as u8, to: to as u8 });
            }
        }
    }

    pub fn generate_captures(&self, out: &mut MoveList) {
        let t = get_tables();
        let (own_bb, opp_bb) = if self.player == ONE {
            (self.bb_one, self.bb_two)
        } else {
            (self.bb_two, self.bb_one)
        };
        out.len = 0;

        let mut pieces = own_bb;
        while pieces != 0 {
            let from = pop_lsb(&mut pieces);

            for dir in 0..8usize {
                let distance = self.line_count(from, dir);
                if distance == 0 || distance > t.ray_len[from][dir] as usize {
                    continue;
                }

                let to = t.rays[from][dir][distance - 1] as usize;
                let to_bit: Bitboard = 1u128 << to;

                // Must land on opponent
                if (to_bit & opp_bb) == 0 {
                    continue;
                }

                // O(1) blocking check
                if (t.ray_prefix[from][dir][distance - 1] & opp_bb) != 0 {
                    continue;
                }

                out.push(Move { from: from as u8, to: to as u8 });
            }
        }
    }

    // ── Connectivity (bitboard flood fill) ────────────────────────────────────

    #[inline]
    pub fn is_connected(&self, player: u8) -> bool {
        let pieces_bb = if player == ONE { self.bb_one } else { self.bb_two };
        let count = pieces_bb.count_ones();
        if count <= 1 {
            return true;
        }
        let start = 1u128 << pieces_bb.trailing_zeros();
        flood_fill_fast(start, pieces_bb) == pieces_bb
    }

    pub fn component_count(&self, player: u8) -> i32 {
        let mut remaining = if player == ONE { self.bb_one } else { self.bb_two };
        if remaining == 0 {
            return 0;
        }
        let mut components = 0i32;
        while remaining != 0 {
            let start = 1u128 << remaining.trailing_zeros();
            let comp = flood_fill_fast(start, remaining);
            remaining &= !comp;
            components += 1;
        }
        components
    }

    pub fn largest_component_value(&self, player: u8) -> i32 {
        let mut remaining = if player == ONE { self.bb_one } else { self.bb_two };
        if remaining == 0 {
            return 0;
        }
        let mut best = 0i32;
        while remaining != 0 {
            let start = 1u128 << remaining.trailing_zeros();
            let comp = flood_fill_fast(start, remaining);
            remaining &= !comp;

            let mut value_sum = 0i32;
            let mut bits = comp;
            while bits != 0 {
                let sq = pop_lsb(&mut bits);
                value_sum += self.fish_value[sq] as i32;
            }
            if value_sum > best {
                best = value_sum;
            }
        }
        best
    }

    pub fn component_spread(&self, player: u8) -> i32 {
        let pieces_bb = if player == ONE { self.bb_one } else { self.bb_two };
        if pieces_bb.count_ones() <= 1 {
            return 0;
        }

        let mut remaining = pieces_bb;
        let mut centroids = [(0i32, 0i32); 16]; // max 8 fish per side, so 16 is safe
        let mut n_comps = 0usize;

        while remaining != 0 {
            let start = 1u128 << remaining.trailing_zeros();
            let comp = flood_fill_fast(start, remaining);
            remaining &= !comp;

            let mut sum_x = 0i32;
            let mut sum_y = 0i32;
            let mut size = 0i32;
            let mut bits = comp;
            while bits != 0 {
                let sq = pop_lsb(&mut bits);
                sum_x += (sq % 10) as i32;
                sum_y += (sq / 10) as i32;
                size += 1;
            }
            centroids[n_comps] = ((sum_x + size / 2) / size, (sum_y + size / 2) / size);
            n_comps += 1;
            if n_comps >= 16 {
                break;
            }
        }

        if n_comps <= 1 {
            return 0;
        }

        let mut gx = 0i32;
        let mut gy = 0i32;
        for i in 0..n_comps {
            gx += centroids[i].0;
            gy += centroids[i].1;
        }
        gx /= n_comps as i32;
        gy /= n_comps as i32;

        let mut spread = 0i32;
        for i in 0..n_comps {
            spread += (centroids[i].0 - gx)
                .abs()
                .max((centroids[i].1 - gy).abs());
        }
        spread
    }

    pub fn total_piece_value(&self, player: u8) -> i32 {
        if player == ONE {
            self.one_value as i32
        } else {
            self.two_value as i32
        }
    }

    // ── Local connectivity swing (for move ordering) ──────────────────────────

    pub fn local_connectivity_swing(&self, mv: Move, player: u8) -> i32 {
        let nb = get_neighbor_masks();
        let own_bb = if player == ONE { self.bb_one } else { self.bb_two };
        let from = mv.from as usize;
        let to = mv.to as usize;

        let from_nb = (nb[from] & own_bb).count_ones() as i32;

        // At the to-square: count own neighbors excluding the from-square (which will vacate)
        let own_without_from = own_bb & !(1u128 << from);
        // Also count from-square itself as a neighbor if it was adjacent to to (it won't be there anymore)
        // Actually: after moving, own piece is at `to`, own_bb no longer has `from`.
        // Neighbors of `to` in the NEW position = (nb[to] & own_bb) excluding from, plus nothing extra.
        let to_nb = (nb[to] & own_without_from).count_ones() as i32;

        to_nb - from_nb
    }
}
