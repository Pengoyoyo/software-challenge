use crate::state::{
    init_tables, opponent, piece_owner, ray_lengths_table, rays_table, Move, Position, KRAKEN,
    NUM_SQUARES, RED,
};

#[inline]
fn pop_lsb_square(bits: &mut u64, block: usize) -> usize {
    let offset = bits.trailing_zeros() as usize;
    *bits &= *bits - 1;
    (block << 6) + offset
}

pub fn generate_moves(pos: &Position, player: u8, out_moves: &mut Vec<Move>) {
    init_tables();
    out_moves.clear();

    let rays = rays_table();
    let ray_lengths = ray_lengths_table();

    let opp = opponent(player);

    let pieces = if player == RED {
        &pos.red_bits
    } else {
        &pos.blue_bits
    };

    for (block, mut bits) in pieces.iter().copied().enumerate() {
        while bits != 0 {
            let from = pop_lsb_square(&mut bits, block);
            if from >= NUM_SQUARES {
                continue;
            }

            for dir in 0..8 {
                let distance = pos.line_count(from, dir);
                if distance <= 0 || distance > ray_lengths[from][dir] as i32 {
                    continue;
                }

                let to = rays[from][dir][(distance - 1) as usize] as i32;
                if to < 0 {
                    continue;
                }
                let to = to as usize;

                let target = pos.board[to];
                if piece_owner(target) == player || target == KRAKEN {
                    continue;
                }

                let mut blocked = false;
                for step in 0..(distance as usize - 1) {
                    let square = rays[from][dir][step] as i32;
                    if square < 0 {
                        continue;
                    }
                    if piece_owner(pos.board[square as usize]) == opp {
                        blocked = true;
                        break;
                    }
                }

                if !blocked {
                    out_moves.push(Move::new(from as u8, to as u8));
                }
            }
        }
    }
}

pub fn has_legal_move(pos: &Position, player: u8) -> bool {
    init_tables();

    let rays = rays_table();
    let ray_lengths = ray_lengths_table();

    let opp = opponent(player);
    let pieces = if player == RED {
        &pos.red_bits
    } else {
        &pos.blue_bits
    };

    for (block, mut bits) in pieces.iter().copied().enumerate() {
        while bits != 0 {
            let from = pop_lsb_square(&mut bits, block);
            if from >= NUM_SQUARES {
                continue;
            }

            for dir in 0..8 {
                let distance = pos.line_count(from, dir);
                if distance <= 0 || distance > ray_lengths[from][dir] as i32 {
                    continue;
                }

                let to = rays[from][dir][(distance - 1) as usize] as i32;
                if to < 0 {
                    continue;
                }
                let to = to as usize;

                let target = pos.board[to];
                if piece_owner(target) == player || target == KRAKEN {
                    continue;
                }

                let mut blocked = false;
                for step in 0..(distance as usize - 1) {
                    let square = rays[from][dir][step] as i32;
                    if square < 0 {
                        continue;
                    }
                    if piece_owner(pos.board[square as usize]) == opp {
                        blocked = true;
                        break;
                    }
                }

                if !blocked {
                    return true;
                }
            }
        }
    }

    false
}

pub fn generate_capture_moves(pos: &Position, player: u8, out_moves: &mut Vec<Move>) {
    init_tables();
    out_moves.clear();

    let rays = rays_table();
    let ray_lengths = ray_lengths_table();

    let opp = opponent(player);

    let pieces = if player == RED {
        &pos.red_bits
    } else {
        &pos.blue_bits
    };

    for (block, mut bits) in pieces.iter().copied().enumerate() {
        while bits != 0 {
            let from = pop_lsb_square(&mut bits, block);
            if from >= NUM_SQUARES {
                continue;
            }

            for dir in 0..8 {
                let distance = pos.line_count(from, dir);
                if distance <= 0 || distance > ray_lengths[from][dir] as i32 {
                    continue;
                }

                let to = rays[from][dir][(distance - 1) as usize] as i32;
                if to < 0 {
                    continue;
                }
                let to = to as usize;

                if piece_owner(pos.board[to]) != opp {
                    continue;
                }

                let mut blocked = false;
                for step in 0..(distance as usize - 1) {
                    let square = rays[from][dir][step] as i32;
                    if square < 0 {
                        continue;
                    }
                    if piece_owner(pos.board[square as usize]) == opp {
                        blocked = true;
                        break;
                    }
                }

                if !blocked {
                    out_moves.push(Move::new(from as u8, to as u8));
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::state::{Position, BLUE_1, EMPTY, RED, RED_1};

    #[test]
    fn generates_basic_moves() {
        let mut pos = Position::default();
        pos.board = [EMPTY; crate::state::NUM_SQUARES];
        pos.board[11] = RED_1;
        pos.board[12] = BLUE_1;
        pos.player_to_move = RED;
        pos.recompute_caches();

        let mut moves = Vec::new();
        generate_moves(&pos, RED, &mut moves);
        assert!(!moves.is_empty());
    }
}
