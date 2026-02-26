use std::cmp::{max, min};
use std::sync::OnceLock;

pub const BOARD_SIZE: usize = 10;
pub const NUM_SQUARES: usize = BOARD_SIZE * BOARD_SIZE;
pub const MAX_RAY: usize = 9;
pub const MAX_NEIGHBORS: usize = 8;
pub const MAX_PLY: usize = 128;

pub const EMPTY: u8 = 0;
pub const RED_1: u8 = 1;
pub const RED_2: u8 = 2;
pub const RED_3: u8 = 3;
pub const BLUE_1: u8 = 4;
pub const BLUE_2: u8 = 5;
pub const BLUE_3: u8 = 6;
pub const KRAKEN: u8 = 7;

pub const RED: u8 = 1;
pub const BLUE: u8 = 2;

pub const WIN_SCORE: i32 = 1_000_000;
pub const MATE_SCORE: i32 = 900_000;

const DIRECTIONS: [(i32, i32); 8] = [
    (-1, -1),
    (0, -1),
    (1, -1),
    (-1, 0),
    (1, 0),
    (-1, 1),
    (0, 1),
    (1, 1),
];

const DIRECTION_LINE_KIND: [u8; 8] = [
    2, // diag a
    1, // col
    3, // diag b
    0, // row
    0, 3, 1, 2,
];

#[inline]
pub const fn opponent(player: u8) -> u8 {
    if player == RED {
        BLUE
    } else {
        RED
    }
}

#[inline]
pub const fn is_red_piece(piece: u8) -> bool {
    piece >= RED_1 && piece <= RED_3
}

#[inline]
pub const fn is_blue_piece(piece: u8) -> bool {
    piece >= BLUE_1 && piece <= BLUE_3
}

#[inline]
pub const fn is_fish_piece(piece: u8) -> bool {
    is_red_piece(piece) || is_blue_piece(piece)
}

#[inline]
pub const fn piece_owner(piece: u8) -> u8 {
    if is_red_piece(piece) {
        RED
    } else if is_blue_piece(piece) {
        BLUE
    } else {
        0
    }
}

#[inline]
pub const fn piece_value(piece: u8) -> u8 {
    if is_red_piece(piece) {
        piece - RED_1 + 1
    } else if is_blue_piece(piece) {
        piece - BLUE_1 + 1
    } else {
        0
    }
}

#[inline]
pub const fn make_piece(player: u8, value: u8) -> u8 {
    let v = if value == 0 {
        1
    } else if value > 3 {
        3
    } else {
        value
    };
    if player == RED {
        RED_1 + v - 1
    } else {
        BLUE_1 + v - 1
    }
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Move {
    pub from: u8,
    pub to: u8,
}

impl Move {
    #[inline]
    pub const fn new(from: u8, to: u8) -> Self {
        Self { from, to }
    }

    #[inline]
    pub const fn encode(self) -> u16 {
        ((self.from as u16) << 7) | (self.to as u16)
    }

    #[inline]
    pub const fn decode(value: u16) -> Self {
        Self {
            from: ((value >> 7) & 0x7f) as u8,
            to: (value & 0x7f) as u8,
        }
    }
}

#[derive(Clone, Copy, Debug, Default)]
pub struct Undo {
    pub mv: Move,
    pub captured: u8,
    pub previous_player: u8,
    pub previous_turn: u16,
    pub previous_hash: u64,
}

#[derive(Clone, Copy, Debug, Default)]
pub struct NullUndo {
    pub previous_player: u8,
    pub previous_hash: u64,
}

#[derive(Clone)]
pub struct Position {
    pub board: [u8; NUM_SQUARES],
    pub fish_value: [u8; NUM_SQUARES],

    pub red_bits: [u64; 2],
    pub blue_bits: [u64; 2],
    pub kraken_bits: [u64; 2],

    pub row_counts: [u8; BOARD_SIZE],
    pub col_counts: [u8; BOARD_SIZE],
    pub diag_a_counts: [u8; 19],
    pub diag_b_counts: [u8; 19],

    pub player_to_move: u8,
    pub turn: u16,
    pub red_count: u16,
    pub blue_count: u16,
    pub red_value_total: u16,
    pub blue_value_total: u16,
    pub hash: u64,
}

impl Default for Position {
    fn default() -> Self {
        Self {
            board: [EMPTY; NUM_SQUARES],
            fish_value: [0; NUM_SQUARES],
            red_bits: [0; 2],
            blue_bits: [0; 2],
            kraken_bits: [0; 2],
            row_counts: [0; BOARD_SIZE],
            col_counts: [0; BOARD_SIZE],
            diag_a_counts: [0; 19],
            diag_b_counts: [0; 19],
            player_to_move: RED,
            turn: 0,
            red_count: 0,
            blue_count: 0,
            red_value_total: 0,
            blue_value_total: 0,
            hash: 0,
        }
    }
}

#[derive(Clone)]
struct Tables {
    neighbors: [[i8; MAX_NEIGHBORS]; NUM_SQUARES],
    neighbor_counts: [u8; NUM_SQUARES],
    rays: [[[i8; MAX_RAY]; 8]; NUM_SQUARES],
    ray_lengths: [[u8; 8]; NUM_SQUARES],
    zobrist: [[u64; NUM_SQUARES]; (KRAKEN as usize) + 1],
    zobrist_side: u64,
}

static TABLES: OnceLock<Tables> = OnceLock::new();

#[inline]
pub const fn in_bounds(x: i32, y: i32) -> bool {
    x >= 0 && x < BOARD_SIZE as i32 && y >= 0 && y < BOARD_SIZE as i32
}

#[inline]
pub const fn xy_to_sq(x: i32, y: i32) -> usize {
    (y as usize) * BOARD_SIZE + (x as usize)
}

#[inline]
pub const fn sq_x(square: usize) -> usize {
    square % BOARD_SIZE
}

#[inline]
pub const fn sq_y(square: usize) -> usize {
    square / BOARD_SIZE
}

#[inline]
const fn diag_a_idx(square: usize) -> usize {
    (sq_x(square) as i32 - sq_y(square) as i32 + 9) as usize
}

#[inline]
const fn diag_b_idx(square: usize) -> usize {
    sq_x(square) + sq_y(square)
}

#[inline]
fn splitmix64(state: &mut u64) -> u64 {
    *state = state.wrapping_add(0x9e3779b97f4a7c15);
    let mut z = *state;
    z = (z ^ (z >> 30)).wrapping_mul(0xbf58476d1ce4e5b9);
    z = (z ^ (z >> 27)).wrapping_mul(0x94d049bb133111eb);
    z ^ (z >> 31)
}

#[inline]
fn bit_block(square: usize) -> (usize, u32) {
    ((square >> 6), (square & 63) as u32)
}

#[inline]
fn clear_bit(bits: &mut [u64; 2], square: usize) {
    let (block, offset) = bit_block(square);
    bits[block] &= !(1_u64 << offset);
}

#[inline]
fn set_bit(bits: &mut [u64; 2], square: usize) {
    let (block, offset) = bit_block(square);
    bits[block] |= 1_u64 << offset;
}

#[inline]
fn get_bit(bits: &[u64; 2], square: usize) -> bool {
    let (block, offset) = bit_block(square);
    ((bits[block] >> offset) & 1_u64) != 0
}

#[inline]
fn line_increment(pos: &mut Position, square: usize) {
    pos.row_counts[sq_y(square)] = pos.row_counts[sq_y(square)].saturating_add(1);
    pos.col_counts[sq_x(square)] = pos.col_counts[sq_x(square)].saturating_add(1);
    pos.diag_a_counts[diag_a_idx(square)] = pos.diag_a_counts[diag_a_idx(square)].saturating_add(1);
    pos.diag_b_counts[diag_b_idx(square)] = pos.diag_b_counts[diag_b_idx(square)].saturating_add(1);
}

#[inline]
fn line_decrement(pos: &mut Position, square: usize) {
    pos.row_counts[sq_y(square)] = pos.row_counts[sq_y(square)].saturating_sub(1);
    pos.col_counts[sq_x(square)] = pos.col_counts[sq_x(square)].saturating_sub(1);
    pos.diag_a_counts[diag_a_idx(square)] = pos.diag_a_counts[diag_a_idx(square)].saturating_sub(1);
    pos.diag_b_counts[diag_b_idx(square)] = pos.diag_b_counts[diag_b_idx(square)].saturating_sub(1);
}

#[inline]
fn piece_hash(piece: u8, square: usize) -> u64 {
    tables().zobrist[piece as usize][square]
}

fn tables() -> &'static Tables {
    TABLES.get_or_init(|| {
        let mut neighbors = [[-1_i8; MAX_NEIGHBORS]; NUM_SQUARES];
        let mut neighbor_counts = [0_u8; NUM_SQUARES];
        let mut rays = [[[-1_i8; MAX_RAY]; 8]; NUM_SQUARES];
        let mut ray_lengths = [[0_u8; 8]; NUM_SQUARES];

        for square in 0..NUM_SQUARES {
            let x = sq_x(square) as i32;
            let y = sq_y(square) as i32;

            for (dir_idx, (dx, dy)) in DIRECTIONS.iter().enumerate() {
                let mut nx = x + dx;
                let mut ny = y + dy;
                let mut ray_len: usize = 0;

                if in_bounds(nx, ny) {
                    neighbors[square][neighbor_counts[square] as usize] = xy_to_sq(nx, ny) as i8;
                    neighbor_counts[square] += 1;
                }

                while in_bounds(nx, ny) && ray_len < MAX_RAY {
                    rays[square][dir_idx][ray_len] = xy_to_sq(nx, ny) as i8;
                    ray_len += 1;
                    nx += dx;
                    ny += dy;
                }

                ray_lengths[square][dir_idx] = ray_len as u8;
            }
        }

        let mut zobrist = [[0_u64; NUM_SQUARES]; (KRAKEN as usize) + 1];
        let mut seed: u64 = 0x20260217;
        for piece in 0..=KRAKEN as usize {
            for square in 0..NUM_SQUARES {
                zobrist[piece][square] = splitmix64(&mut seed);
            }
        }
        let zobrist_side = splitmix64(&mut seed);

        Tables {
            neighbors,
            neighbor_counts,
            rays,
            ray_lengths,
            zobrist,
            zobrist_side,
        }
    })
}

pub fn init_tables() {
    let _ = tables();
}

pub fn neighbors_table() -> &'static [[i8; MAX_NEIGHBORS]; NUM_SQUARES] {
    &tables().neighbors
}

pub fn neighbors_count_table() -> &'static [u8; NUM_SQUARES] {
    &tables().neighbor_counts
}

pub fn rays_table() -> &'static [[[i8; MAX_RAY]; 8]; NUM_SQUARES] {
    &tables().rays
}

pub fn ray_lengths_table() -> &'static [[u8; 8]; NUM_SQUARES] {
    &tables().ray_lengths
}

impl Position {
    pub fn load_from_blob(&mut self, blob: &[u8]) -> bool {
        init_tables();

        if blob.len() < 105 {
            return false;
        }

        let v2 = blob.len() >= 106 && blob[105] >= 2;
        if v2 {
            self.board.copy_from_slice(&blob[..NUM_SQUARES]);
        } else {
            for (square, legacy) in blob.iter().copied().take(NUM_SQUARES).enumerate() {
                self.board[square] = match legacy {
                    1 => RED_1,
                    2 => BLUE_1,
                    3 => KRAKEN,
                    _ => EMPTY,
                };
            }
        }

        self.player_to_move = blob[100];
        if self.player_to_move != RED && self.player_to_move != BLUE {
            self.player_to_move = RED;
        }

        let turn32 = (blob[101] as u32)
            | ((blob[102] as u32) << 8)
            | ((blob[103] as u32) << 16)
            | ((blob[104] as u32) << 24);
        self.turn = min(turn32, 0xffff) as u16;

        self.recompute_caches();
        true
    }

    pub fn to_blob(&self) -> [u8; 106] {
        let mut blob = [0_u8; 106];
        blob[..NUM_SQUARES].copy_from_slice(&self.board);
        blob[100] = self.player_to_move;

        let turn32 = self.turn as u32;
        blob[101] = (turn32 & 0xff) as u8;
        blob[102] = ((turn32 >> 8) & 0xff) as u8;
        blob[103] = ((turn32 >> 16) & 0xff) as u8;
        blob[104] = ((turn32 >> 24) & 0xff) as u8;
        blob[105] = 2;
        blob
    }

    pub fn recompute_caches(&mut self) {
        init_tables();

        self.red_bits = [0; 2];
        self.blue_bits = [0; 2];
        self.kraken_bits = [0; 2];
        self.fish_value = [0; NUM_SQUARES];
        self.row_counts = [0; BOARD_SIZE];
        self.col_counts = [0; BOARD_SIZE];
        self.diag_a_counts = [0; 19];
        self.diag_b_counts = [0; 19];
        self.red_count = 0;
        self.blue_count = 0;
        self.red_value_total = 0;
        self.blue_value_total = 0;
        self.hash = 0;

        for square in 0..NUM_SQUARES {
            let piece = self.board[square];
            let owner = piece_owner(piece);
            let value = piece_value(piece);
            self.fish_value[square] = value;

            if owner == RED {
                set_bit(&mut self.red_bits, square);
                self.red_count += 1;
                self.red_value_total += value as u16;
                line_increment(self, square);
                self.hash ^= piece_hash(piece, square);
            } else if owner == BLUE {
                set_bit(&mut self.blue_bits, square);
                self.blue_count += 1;
                self.blue_value_total += value as u16;
                line_increment(self, square);
                self.hash ^= piece_hash(piece, square);
            } else if piece == KRAKEN {
                set_bit(&mut self.kraken_bits, square);
                self.hash ^= piece_hash(KRAKEN, square);
            }
        }

        if self.player_to_move == BLUE {
            self.hash ^= tables().zobrist_side;
        }
    }

    #[inline]
    pub fn line_count(&self, square: usize, direction_index: usize) -> i32 {
        match DIRECTION_LINE_KIND[direction_index] {
            0 => self.row_counts[sq_y(square)] as i32,
            1 => self.col_counts[sq_x(square)] as i32,
            2 => self.diag_a_counts[diag_a_idx(square)] as i32,
            _ => self.diag_b_counts[diag_b_idx(square)] as i32,
        }
    }

    pub fn make_move(&mut self, mv: Move, undo: &mut Undo) -> bool {
        let from = mv.from as usize;
        let to = mv.to as usize;

        if from >= NUM_SQUARES || to >= NUM_SQUARES {
            return false;
        }

        let moved = self.board[from];
        let captured = self.board[to];
        let moved_owner = piece_owner(moved);
        let captured_owner = piece_owner(captured);
        let moved_value = piece_value(moved);
        let captured_value = piece_value(captured);

        if moved_owner != self.player_to_move || moved == EMPTY || moved == KRAKEN {
            return false;
        }
        if captured_owner == moved_owner || captured == KRAKEN {
            return false;
        }

        undo.mv = mv;
        undo.captured = captured;
        undo.previous_player = self.player_to_move;
        undo.previous_turn = self.turn;
        undo.previous_hash = self.hash;

        line_decrement(self, from);
        if captured_owner == RED || captured_owner == BLUE {
            line_decrement(self, to);
        }
        line_increment(self, to);

        self.board[from] = EMPTY;
        self.fish_value[from] = 0;
        self.board[to] = moved;
        self.fish_value[to] = moved_value;

        if moved_owner == RED {
            clear_bit(&mut self.red_bits, from);
            set_bit(&mut self.red_bits, to);
        } else {
            clear_bit(&mut self.blue_bits, from);
            set_bit(&mut self.blue_bits, to);
        }

        if captured_owner == RED {
            clear_bit(&mut self.red_bits, to);
            self.red_count = self.red_count.saturating_sub(1);
            self.red_value_total = self.red_value_total.saturating_sub(captured_value as u16);
        } else if captured_owner == BLUE {
            clear_bit(&mut self.blue_bits, to);
            self.blue_count = self.blue_count.saturating_sub(1);
            self.blue_value_total = self.blue_value_total.saturating_sub(captured_value as u16);
        }

        self.hash ^= piece_hash(moved, from);
        if captured_owner == RED || captured_owner == BLUE {
            self.hash ^= piece_hash(captured, to);
        }
        self.hash ^= piece_hash(moved, to);
        self.hash ^= tables().zobrist_side;

        self.player_to_move = opponent(self.player_to_move);
        self.turn = self.turn.wrapping_add(1);

        true
    }

    pub fn unmake_move(&mut self, undo: &Undo) {
        let from = undo.mv.from as usize;
        let to = undo.mv.to as usize;

        let moved = self.board[to];
        let moved_owner = piece_owner(moved);
        let captured_owner = piece_owner(undo.captured);
        let captured_value = piece_value(undo.captured);

        self.board[from] = moved;
        self.board[to] = undo.captured;
        self.fish_value[from] = piece_value(moved);
        self.fish_value[to] = captured_value;

        line_decrement(self, to);
        if captured_owner == RED || captured_owner == BLUE {
            line_increment(self, to);
        }
        line_increment(self, from);

        if moved_owner == RED {
            clear_bit(&mut self.red_bits, to);
            set_bit(&mut self.red_bits, from);
        } else if moved_owner == BLUE {
            clear_bit(&mut self.blue_bits, to);
            set_bit(&mut self.blue_bits, from);
        }

        if captured_owner == RED {
            set_bit(&mut self.red_bits, to);
            self.red_count = self.red_count.saturating_add(1);
            self.red_value_total = self.red_value_total.saturating_add(captured_value as u16);
        } else if captured_owner == BLUE {
            set_bit(&mut self.blue_bits, to);
            self.blue_count = self.blue_count.saturating_add(1);
            self.blue_value_total = self.blue_value_total.saturating_add(captured_value as u16);
        }

        self.player_to_move = undo.previous_player;
        self.turn = undo.previous_turn;
        self.hash = undo.previous_hash;
    }

    pub fn make_null_move(&mut self, undo: &mut NullUndo) {
        undo.previous_player = self.player_to_move;
        undo.previous_hash = self.hash;

        self.player_to_move = opponent(self.player_to_move);
        self.turn = self.turn.wrapping_add(1);
        self.hash ^= tables().zobrist_side;
    }

    pub fn unmake_null_move(&mut self, undo: &NullUndo) {
        self.player_to_move = undo.previous_player;
        self.turn = self.turn.wrapping_sub(1);
        self.hash = undo.previous_hash;
    }

    #[inline]
    pub fn has_piece(&self, player: u8, square: usize) -> bool {
        if player == RED {
            get_bit(&self.red_bits, square)
        } else {
            get_bit(&self.blue_bits, square)
        }
    }

    pub fn is_connected(&self, player: u8) -> bool {
        let piece_count = if player == RED {
            self.red_count as i32
        } else {
            self.blue_count as i32
        };
        if piece_count <= 1 {
            return true;
        }

        let mut seen = [false; NUM_SQUARES];
        let mut stack = [0_usize; NUM_SQUARES];

        let mut start: Option<usize> = None;
        for square in 0..NUM_SQUARES {
            if piece_owner(self.board[square]) == player {
                start = Some(square);
                break;
            }
        }

        let Some(start_square) = start else {
            return true;
        };

        let neighbors = neighbors_table();
        let counts = neighbors_count_table();

        let mut top = 0_usize;
        stack[top] = start_square;
        top += 1;
        seen[start_square] = true;
        let mut visited = 1_i32;

        while top > 0 {
            top -= 1;
            let square = stack[top];
            for i in 0..counts[square] as usize {
                let nb = neighbors[square][i];
                if nb < 0 {
                    continue;
                }
                let nbs = nb as usize;
                if !seen[nbs] && piece_owner(self.board[nbs]) == player {
                    seen[nbs] = true;
                    stack[top] = nbs;
                    top += 1;
                    visited += 1;
                }
            }
        }

        visited == piece_count
    }

    pub fn component_count(&self, player: u8) -> i32 {
        if if player == RED {
            self.red_count
        } else {
            self.blue_count
        } == 0
        {
            return 0;
        }

        let neighbors = neighbors_table();
        let counts = neighbors_count_table();

        let mut seen = [false; NUM_SQUARES];
        let mut stack = [0_usize; NUM_SQUARES];

        let mut components = 0_i32;
        for square in 0..NUM_SQUARES {
            if piece_owner(self.board[square]) != player || seen[square] {
                continue;
            }

            components += 1;
            let mut top = 0_usize;
            stack[top] = square;
            top += 1;
            seen[square] = true;

            while top > 0 {
                top -= 1;
                let cur = stack[top];

                for i in 0..counts[cur] as usize {
                    let nb = neighbors[cur][i];
                    if nb < 0 {
                        continue;
                    }
                    let nbs = nb as usize;
                    if !seen[nbs] && piece_owner(self.board[nbs]) == player {
                        seen[nbs] = true;
                        stack[top] = nbs;
                        top += 1;
                    }
                }
            }
        }

        components
    }

    pub fn largest_component(&self, player: u8) -> i32 {
        if if player == RED {
            self.red_count
        } else {
            self.blue_count
        } == 0
        {
            return 0;
        }

        let neighbors = neighbors_table();
        let counts = neighbors_count_table();

        let mut seen = [false; NUM_SQUARES];
        let mut stack = [0_usize; NUM_SQUARES];

        let mut best = 0_i32;
        for square in 0..NUM_SQUARES {
            if piece_owner(self.board[square]) != player || seen[square] {
                continue;
            }

            let mut size = 0_i32;
            let mut top = 0_usize;
            stack[top] = square;
            top += 1;
            seen[square] = true;

            while top > 0 {
                top -= 1;
                let cur = stack[top];
                size += 1;

                for i in 0..counts[cur] as usize {
                    let nb = neighbors[cur][i];
                    if nb < 0 {
                        continue;
                    }
                    let nbs = nb as usize;
                    if !seen[nbs] && piece_owner(self.board[nbs]) == player {
                        seen[nbs] = true;
                        stack[top] = nbs;
                        top += 1;
                    }
                }
            }

            best = max(best, size);
        }

        best
    }

    pub fn largest_component_value(&self, player: u8) -> i32 {
        if if player == RED {
            self.red_count
        } else {
            self.blue_count
        } == 0
        {
            return 0;
        }

        let neighbors = neighbors_table();
        let counts = neighbors_count_table();

        let mut seen = [false; NUM_SQUARES];
        let mut stack = [0_usize; NUM_SQUARES];

        let mut best = 0_i32;
        for square in 0..NUM_SQUARES {
            if piece_owner(self.board[square]) != player || seen[square] {
                continue;
            }

            let mut value_sum = 0_i32;
            let mut top = 0_usize;
            stack[top] = square;
            top += 1;
            seen[square] = true;

            while top > 0 {
                top -= 1;
                let cur = stack[top];
                value_sum += self.fish_value[cur] as i32;

                for i in 0..counts[cur] as usize {
                    let nb = neighbors[cur][i];
                    if nb < 0 {
                        continue;
                    }
                    let nbs = nb as usize;
                    if !seen[nbs] && piece_owner(self.board[nbs]) == player {
                        seen[nbs] = true;
                        stack[top] = nbs;
                        top += 1;
                    }
                }
            }

            best = max(best, value_sum);
        }

        best
    }

    pub fn component_spread(&self, player: u8) -> i32 {
        let count = if player == RED {
            self.red_count as i32
        } else {
            self.blue_count as i32
        };

        if count <= 1 {
            return 0;
        }

        let neighbors = neighbors_table();
        let counts = neighbors_count_table();

        let mut seen = [false; NUM_SQUARES];
        let mut stack = [0_usize; NUM_SQUARES];
        let mut centroids = [(0_i32, 0_i32); NUM_SQUARES];
        let mut component_n = 0_usize;

        for square in 0..NUM_SQUARES {
            if piece_owner(self.board[square]) != player || seen[square] {
                continue;
            }

            let mut top = 0_usize;
            let mut size = 0_i32;
            let mut sum_x = 0_i32;
            let mut sum_y = 0_i32;

            stack[top] = square;
            top += 1;
            seen[square] = true;

            while top > 0 {
                top -= 1;
                let cur = stack[top];
                size += 1;
                sum_x += sq_x(cur) as i32;
                sum_y += sq_y(cur) as i32;

                for i in 0..counts[cur] as usize {
                    let nb = neighbors[cur][i];
                    if nb < 0 {
                        continue;
                    }
                    let nbs = nb as usize;
                    if !seen[nbs] && piece_owner(self.board[nbs]) == player {
                        seen[nbs] = true;
                        stack[top] = nbs;
                        top += 1;
                    }
                }
            }

            centroids[component_n] = ((sum_x + size / 2) / size, (sum_y + size / 2) / size);
            component_n += 1;
        }

        if component_n <= 1 {
            return 0;
        }

        let mut gx = 0_i32;
        let mut gy = 0_i32;
        for (cx, cy) in centroids.iter().copied().take(component_n) {
            gx += cx;
            gy += cy;
        }
        gx /= component_n as i32;
        gy /= component_n as i32;

        let mut spread = 0_i32;
        for (cx, cy) in centroids.iter().copied().take(component_n) {
            spread += max((cx - gx).abs(), (cy - gy).abs());
        }

        spread
    }

    #[inline]
    pub fn total_piece_value(&self, player: u8) -> i32 {
        if player == RED {
            self.red_value_total as i32
        } else {
            self.blue_value_total as i32
        }
    }
}

pub fn setup_initial_position(pos: &mut Position, seed: u32) {
    init_tables();

    pos.board = [EMPTY; NUM_SQUARES];
    pos.fish_value = [0; NUM_SQUARES];
    pos.player_to_move = RED;
    pos.turn = 0;

    let edge_values: [u8; 8] = [1, 2, 1, 3, 1, 2, 1, 3];
    for y in 1..=8 {
        let v = edge_values[y - 1];
        pos.board[xy_to_sq(0, y as i32)] = make_piece(RED, v);
        pos.board[xy_to_sq(9, y as i32)] = make_piece(RED, v);
    }

    for x in 1..=8 {
        let v = edge_values[x - 1];
        pos.board[xy_to_sq(x as i32, 0)] = make_piece(BLUE, v);
        pos.board[xy_to_sq(x as i32, 9)] = make_piece(BLUE, v);
    }

    let mut rng_state = 0x9e3779b97f4a7c15_u64 ^ (seed as u64);

    let mut next_inner_square = |board: &[u8; NUM_SQUARES]| -> usize {
        loop {
            let r = splitmix64(&mut rng_state);
            let x = 2 + (r % 6) as i32;
            let y = 2 + ((r >> 8) % 6) as i32;
            let square = xy_to_sq(x, y);
            if board[square] == EMPTY {
                return square;
            }
        }
    };

    let k1 = next_inner_square(&pos.board);
    pos.board[k1] = KRAKEN;
    let k2 = next_inner_square(&pos.board);
    pos.board[k2] = KRAKEN;

    pos.recompute_caches();
}
