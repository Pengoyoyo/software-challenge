/// NNUE forward pass: 800 → 256 → 32 → 1, ClippedReLU activations.
///
/// Weights are embedded at compile time from `src/weights.bin` (exported by
/// nnue/training/export.py). Only compiled when `cfg(has_nnue)` is set by build.rs.

use crate::board::Position;
use std::sync::OnceLock;

#[allow(dead_code)]
const L1: usize = 256;
#[allow(dead_code)]
const L2: usize = 32;
#[allow(dead_code)]
const INPUT: usize = 800; // 100 squares × 8 piece types

#[cfg(has_nnue)]
static WEIGHTS_BYTES: &[u8] = include_bytes!("weights.bin");

// ─── Weight storage ───────────────────────────────────────────────────────────

pub struct NNUEWeights {
    l1_w: Vec<f32>, // [INPUT, L1]  transposed from PyTorch [L1, INPUT]
    l1_b: Vec<f32>, // [L1]
    l2_w: Vec<f32>, // [L1, L2]    transposed from PyTorch [L2, L1]
    l2_b: Vec<f32>, // [L2]
    out_w: Vec<f32>, // [L2]
    out_b: f32,
}

impl NNUEWeights {
    #[allow(dead_code)]
    fn load_from_bytes(bytes: &[u8]) -> Self {
        let floats: Vec<f32> = bytes
            .chunks_exact(4)
            .map(|b| f32::from_le_bytes([b[0], b[1], b[2], b[3]]))
            .collect();

        let mut pos = 0usize;
        let mut take = |n: usize| -> Vec<f32> {
            let v = floats[pos..pos + n].to_vec();
            pos += n;
            v
        };

        // PyTorch exports weight as [out, in]; transpose to [in, out] for cache efficiency
        let l1_w_raw = take(L1 * INPUT); // [L1, INPUT]
        let l1_b = take(L1);
        let l2_w_raw = take(L2 * L1); // [L2, L1]
        let l2_b = take(L2);
        let out_w = take(L2);
        let out_b = floats[pos];

        let mut l1_w = vec![0.0f32; INPUT * L1];
        for i in 0..L1 {
            for j in 0..INPUT {
                l1_w[j * L1 + i] = l1_w_raw[i * INPUT + j];
            }
        }

        let mut l2_w = vec![0.0f32; L1 * L2];
        for i in 0..L2 {
            for j in 0..L1 {
                l2_w[j * L2 + i] = l2_w_raw[i * L1 + j];
            }
        }

        NNUEWeights { l1_w, l1_b, l2_w, l2_b, out_w, out_b }
    }
}

static WEIGHTS: OnceLock<Option<NNUEWeights>> = OnceLock::new();

fn get_weights() -> Option<&'static NNUEWeights> {
    WEIGHTS.get_or_init(|| {
        #[cfg(has_nnue)]
        {
            Some(NNUEWeights::load_from_bytes(WEIGHTS_BYTES))
        }
        #[cfg(not(has_nnue))]
        {
            None
        }
    }).as_ref()
}

// ─── Accumulator ─────────────────────────────────────────────────────────────

struct Accumulator {
    acc: [f32; L1],
}

impl Accumulator {
    fn new(weights: &NNUEWeights) -> Self {
        let mut acc = [0.0f32; L1];
        acc.copy_from_slice(&weights.l1_b);
        Self { acc }
    }

    #[inline(always)]
    fn add_feature(&mut self, feat: usize, weights: &NNUEWeights) {
        let row = &weights.l1_w[feat * L1..(feat + 1) * L1];
        for (a, &w) in self.acc.iter_mut().zip(row) {
            *a += w;
        }
    }
}

// ─── Feature encoding (player-relative) ──────────────────────────────────────

#[inline(always)]
fn feature_index(square: usize, piece: u8, player: u8) -> Option<usize> {
    let feat: usize = match player {
        1 => match piece {
            1 => 1, 2 => 2, 3 => 3, // own S/M/L
            4 => 4, 5 => 5, 6 => 6, // opp S/M/L
            7 => 7,                  // squid
            _ => return None,
        },
        2 => match piece {
            4 => 1, 5 => 2, 6 => 3, // own S/M/L
            1 => 4, 2 => 5, 3 => 6, // opp S/M/L
            7 => 7,
            _ => return None,
        },
        _ => return None,
    };
    Some(square * 8 + feat)
}

fn accumulator_from_board(board: &[u8; 100], player: u8, weights: &NNUEWeights) -> Accumulator {
    let mut acc = Accumulator::new(weights);
    for sq in 0..100 {
        let piece = board[sq];
        if piece == 0 { continue; }
        if let Some(feat) = feature_index(sq, piece, player) {
            acc.add_feature(feat, weights);
        }
    }
    acc
}

// ─── Forward pass ─────────────────────────────────────────────────────────────

fn forward(acc: &Accumulator, weights: &NNUEWeights) -> f32 {
    let mut l2_out = [0.0f32; L2];
    l2_out.copy_from_slice(&weights.l2_b);

    for (neuron, &a) in acc.acc.iter().enumerate() {
        let act = a.clamp(0.0, 1.0);
        if act == 0.0 { continue; }
        let row = &weights.l2_w[neuron * L2..(neuron + 1) * L2];
        for (o, &w) in l2_out.iter_mut().zip(row) {
            *o += act * w;
        }
    }

    let mut score = weights.out_b;
    for (neuron, &l2) in l2_out.iter().enumerate() {
        score += l2.clamp(0.0, 1.0) * weights.out_w[neuron];
    }
    score
}

// ─── Public API ───────────────────────────────────────────────────────────────

pub fn nnue_available() -> bool {
    get_weights().is_some()
}

/// Evaluate `pos` from `perspective`'s point of view using NNUE.
/// Returns centipawn-like score (positive = good for perspective).
/// Panics if called without weights — check `nnue_available()` first.
pub fn nnue_eval(pos: &Position, perspective: u8) -> i32 {
    let weights = get_weights().expect("nnue_eval called without weights");
    let acc = accumulator_from_board(&pos.board, perspective, weights);
    let raw = forward(&acc, weights);
    // Invert tanh normalization: label = tanh(score/600), so score = atanh(label)*600
    let clamped = raw.clamp(-0.9999, 0.9999);
    (clamped.atanh() * 600.0) as i32
}
