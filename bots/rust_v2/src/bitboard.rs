// ─── Bitboard for 10x10 Piranhas board ────────────────────────────────────────
// Bit i corresponds to square i where i = y * 10 + x  (x=col 0-9, y=row 0-9)
// Only bits 0..99 are used; bits 100..127 are always zero.

use std::sync::OnceLock;

pub type Bitboard = u128;

pub const BOARD_MASK: Bitboard = (1u128 << 100) - 1;

// ─── Column masks ─────────────────────────────────────────────────────────────

const fn col_mask(c: usize) -> Bitboard {
    let mut mask = 0u128;
    let mut row = 0;
    while row < 10 {
        mask |= 1u128 << (row * 10 + c);
        row += 1;
    }
    mask
}

const NOT_COL_0: Bitboard = !col_mask(0) & BOARD_MASK;
const NOT_COL_9: Bitboard = !col_mask(9) & BOARD_MASK;

// ─── Neighbor expansion (one step in all 8 directions) ────────────────────────

/// Expand a bitboard by exactly one step in all 8 directions, preventing
/// wrap-around at column edges.
#[inline(always)]
pub fn expand_neighbors(bb: Bitboard) -> Bitboard {
    let up    = (bb >> 10) & BOARD_MASK;           // y - 1
    let down  = (bb << 10) & BOARD_MASK;           // y + 1
    let left  = (bb >> 1)  & NOT_COL_9;            // x - 1 (mask wraps from col 0 → col 9)
    let right = (bb << 1)  & NOT_COL_0;            // x + 1 (mask wraps from col 9 → col 0)
    let ul    = (bb >> 11) & NOT_COL_9 & BOARD_MASK;
    let ur    = (bb >> 9)  & NOT_COL_0 & BOARD_MASK;
    let dl    = (bb << 9)  & NOT_COL_9 & BOARD_MASK;
    let dr    = (bb << 11) & NOT_COL_0 & BOARD_MASK;
    up | down | left | right | ul | ur | dl | dr
}

// ─── Flood fill ────────────────────────────────────────────────────────────────

/// Iterative flood-fill starting from `seed`, expanding only within `pieces`.
/// Returns the full connected component containing the seed square(s).
#[inline]
pub fn flood_fill_fast(seed: Bitboard, pieces: Bitboard) -> Bitboard {
    let mut filled = seed & pieces;
    loop {
        let expanded = expand_neighbors(filled) & pieces;
        let new_filled = filled | expanded;
        if new_filled == filled {
            break;
        }
        filled = new_filled;
    }
    filled
}

// ─── Bit manipulation helpers ─────────────────────────────────────────────────

/// Extract and clear the lowest set bit. Returns its index.
#[inline(always)]
pub fn pop_lsb(bb: &mut Bitboard) -> usize {
    let sq = bb.trailing_zeros() as usize;
    *bb &= *bb - 1;
    sq
}

/// Count set bits.
#[inline(always)]
pub fn popcount(bb: Bitboard) -> u32 {
    bb.count_ones()
}

// ─── Precomputed per-square neighbor masks ─────────────────────────────────────

static NEIGHBOR_MASKS: OnceLock<[Bitboard; 100]> = OnceLock::new();

pub fn get_neighbor_masks() -> &'static [Bitboard; 100] {
    NEIGHBOR_MASKS.get_or_init(|| {
        let mut masks = [0u128; 100];
        for sq in 0..100usize {
            masks[sq] = expand_neighbors(1u128 << sq);
        }
        masks
    })
}

// ─── Tests ────────────────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn flood_fill_single() {
        // Single piece: connected with itself
        let piece: Bitboard = 1u128 << 55;
        let result = flood_fill_fast(piece, piece);
        assert_eq!(result, piece);
    }

    #[test]
    fn flood_fill_two_adjacent() {
        let a: Bitboard = 1u128 << 44; // sq 44 = (col 4, row 4)
        let b: Bitboard = 1u128 << 45; // sq 45 = (col 5, row 4), adjacent
        let pieces = a | b;
        let result = flood_fill_fast(a, pieces);
        assert_eq!(result, pieces, "Two adjacent pieces should be one component");
    }

    #[test]
    fn flood_fill_two_disconnected() {
        let a: Bitboard = 1u128 << 0;  // sq 0
        let b: Bitboard = 1u128 << 99; // sq 99, far away
        let pieces = a | b;
        let result = flood_fill_fast(a, pieces);
        assert_eq!(result, a, "Seed component should not reach sq 99");
    }

    #[test]
    fn no_column_wrap() {
        // A piece at col 9 should NOT connect to col 0 via right-shift
        let col9: Bitboard = col_mask(9);
        let col0: Bitboard = col_mask(0);
        let expanded = expand_neighbors(col9);
        assert_eq!(expanded & col0, 0, "col 9 neighbors must not include col 0");
    }

    #[test]
    fn no_row_wrap() {
        // Bits above row 9 (>= bit 100) must be zero after expansion
        let top_row: Bitboard = {
            let mut m = 0u128;
            for c in 0..10 { m |= 1u128 << (9 * 10 + c); }
            m
        };
        let expanded = expand_neighbors(top_row);
        assert_eq!(expanded >> 100, 0, "No bits should spill above bit 99");
    }
}
