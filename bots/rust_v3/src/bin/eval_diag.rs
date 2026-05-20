/// Diagnose: zeigt NNUE-Score und HCE-Score für mehrere Test-Stellungen,
/// damit man sehen kann, ob die Werte in vergleichbaren Größenordnungen liegen.

use piranhas_bot_v2::board::{Position, ONE, MoveList, Undo, SQUID};
use piranhas_bot_v2::evaluate::debug_eval_split;

struct Rng(u64);
impl Rng {
    fn new(seed: u64) -> Self { Self(seed) }
    fn next(&mut self) -> u64 {
        self.0 = self.0.wrapping_add(0x9e3779b97f4a7c15u64);
        let mut z = self.0;
        z = (z ^ (z >> 30)).wrapping_mul(0xbf58476d1ce4e5b9u64);
        z = (z ^ (z >> 27)).wrapping_mul(0x94d049bb133111ebu64);
        z ^ (z >> 31)
    }
    fn range(&mut self, n: u64) -> u64 { self.next() % n }
}

fn realistic_start(rng: &mut Rng) -> Position {
    let mut pos = Position::default();
    for row in 1..=8usize {
        let s = (rng.range(10) as u8).min(2);
        pos.board[row * 10 + 0] = 1 + s;
        pos.board[row * 10 + 9] = 1 + s;
    }
    for col in 1..=8usize {
        let s = (rng.range(10) as u8).min(2);
        pos.board[0 * 10 + col] = 4 + s;
        pos.board[9 * 10 + col] = 4 + s;
    }
    pos.board[24] = SQUID;
    pos.board[57] = SQUID;
    pos.recompute_caches();
    pos
}

fn randomize(pos: &mut Position, n: usize, rng: &mut Rng) {
    for _ in 0..n {
        let mut ml = MoveList::new();
        pos.generate_moves_for(pos.player, &mut ml);
        if ml.len == 0 { return; }
        let idx = rng.range(ml.len as u64) as usize;
        let mut u = Undo::default();
        let _ = pos.make_move(ml.moves[idx], &mut u);
    }
}

fn main() {
    let mut rng = Rng::new(42);
    println!("{:>4} {:>10} {:>10} {:>8}", "n", "nnue", "hce", "diff");
    println!("{}", "-".repeat(40));

    let mut sum_abs_nnue = 0i64;
    let mut sum_abs_hce = 0i64;
    let mut n = 0i64;

    for i in 0..20 {
        let mut pos = realistic_start(&mut rng);
        randomize(&mut pos, 6 + (i as usize % 12), &mut rng);

        let (nnue_s, hce_s) = debug_eval_split(&pos, ONE, 5);
        println!("{:>4} {:>10} {:>10} {:>+8}", i, nnue_s, hce_s, nnue_s - hce_s);
        sum_abs_nnue += nnue_s.abs() as i64;
        sum_abs_hce += hce_s.abs() as i64;
        n += 1;
    }

    println!("{}", "-".repeat(40));
    println!("mean |nnue| = {}", sum_abs_nnue / n);
    println!("mean |hce|  = {}", sum_abs_hce / n);
    println!("ratio nnue/hce = {:.3}", sum_abs_nnue as f64 / sum_abs_hce.max(1) as f64);
}
