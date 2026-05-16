/// NNUE forward pass: 800 → 256 → 32 → 1, ClippedReLU activations.
///
/// Weights are loaded once at startup from `weights.bin` (see export.py for layout).
/// The `Accumulator` holds the L1 output and is incrementally updated as pieces move.

const L1: usize = 256;
const L2: usize = 32;
const INPUT: usize = 800;

#[derive(Clone)]
pub struct NNUEWeights {
    pub l1_w: Vec<f32>,   // [INPUT * L1]  row-major: l1_w[feat * L1 + neuron]
    pub l1_b: Vec<f32>,   // [L1]
    pub l2_w: Vec<f32>,   // [L1 * L2]
    pub l2_b: Vec<f32>,   // [L2]
    pub out_w: Vec<f32>,  // [L2]
    pub out_b: f32,
}

impl NNUEWeights {
    /// Load from the binary produced by export.py.
    pub fn load(path: &str) -> Self {
        use std::io::Read;
        let mut buf = Vec::new();
        std::fs::File::open(path).expect("weights.bin not found").read_to_end(&mut buf).unwrap();

        let floats: Vec<f32> = buf.chunks_exact(4)
            .map(|b| f32::from_le_bytes([b[0], b[1], b[2], b[3]]))
            .collect();

        let mut pos = 0;
        let take = |pos: &mut usize, n: usize| -> Vec<f32> {
            let v = floats[*pos..*pos + n].to_vec();
            *pos += n;
            v
        };

        // PyTorch Linear stores weight as [out, in]; we want [in, out] for fast accumulation
        let l1_w_raw = take(&mut pos, L1 * INPUT);  // [L1, INPUT]
        let l1_b = take(&mut pos, L1);
        let l2_w_raw = take(&mut pos, L2 * L1);     // [L2, L1]
        let l2_b = take(&mut pos, L2);
        let out_w = take(&mut pos, L2);
        let out_b = floats[pos];

        // Transpose l1_w to [INPUT, L1] for cache-friendly column access
        let mut l1_w = vec![0.0f32; INPUT * L1];
        for i in 0..L1 {
            for j in 0..INPUT {
                l1_w[j * L1 + i] = l1_w_raw[i * INPUT + j];
            }
        }

        // Transpose l2_w to [L1, L2]
        let mut l2_w = vec![0.0f32; L1 * L2];
        for i in 0..L2 {
            for j in 0..L1 {
                l2_w[j * L2 + i] = l2_w_raw[i * L1 + j];
            }
        }

        NNUEWeights { l1_w, l1_b, l2_w, l2_b, out_w, out_b }
    }
}

/// L1 accumulator — maintained incrementally during search.
#[derive(Clone)]
pub struct Accumulator {
    pub acc: [f32; L1],
}

impl Accumulator {
    pub fn new(weights: &NNUEWeights) -> Self {
        Self { acc: weights.l1_b.as_slice().try_into().unwrap() }
    }

    /// Add a feature (set a previously zero input to 1).
    #[inline(always)]
    pub fn add_feature(&mut self, feat: usize, weights: &NNUEWeights) {
        let row = &weights.l1_w[feat * L1..(feat + 1) * L1];
        for (a, &w) in self.acc.iter_mut().zip(row) {
            *a += w;
        }
    }

    /// Remove a feature (set a previously one input to 0).
    #[inline(always)]
    pub fn remove_feature(&mut self, feat: usize, weights: &NNUEWeights) {
        let row = &weights.l1_w[feat * L1..(feat + 1) * L1];
        for (a, &w) in self.acc.iter_mut().zip(row) {
            *a -= w;
        }
    }
}

/// Full forward pass from a pre-computed accumulator. Returns score in centipawn-like units.
pub fn forward(acc: &Accumulator, weights: &NNUEWeights) -> f32 {
    // L1 → L2 (ClippedReLU on L1 output first)
    let mut l2_out = weights.l2_b.clone();
    for (neuron, &a) in acc.acc.iter().enumerate() {
        let act = a.clamp(0.0, 1.0);
        if act == 0.0 { continue; }
        let row = &weights.l2_w[neuron * L2..(neuron + 1) * L2];
        for (o, &w) in l2_out.iter_mut().zip(row) {
            *o += act * w;
        }
    }

    // L2 → output
    let mut score = weights.out_b;
    for (neuron, &l2) in l2_out.iter().enumerate() {
        score += l2.clamp(0.0, 1.0) * weights.out_w[neuron];
    }
    score
}

/// Convert piece at a square to a feature index (player-relative).
/// Returns None for empty squares (piece == 0).
pub fn feature_index(square: usize, piece: u8, player: u8) -> Option<usize> {
    let feat = match player {
        1 => match piece {
            0 => 0,
            1 => 1, 2 => 2, 3 => 3,   // own
            4 => 4, 5 => 5, 6 => 6,   // opp
            7 => 7,                    // squid
            _ => return None,
        },
        2 => match piece {
            0 => 0,
            4 => 1, 5 => 2, 6 => 3,   // own (TWO's pieces)
            1 => 4, 2 => 5, 3 => 6,   // opp (ONE's pieces)
            7 => 7,
            _ => return None,
        },
        _ => return None,
    };
    if feat == 0 { return None; }  // empty — no feature
    Some(square * 8 + feat)
}

/// Build a fresh accumulator from a full board (100 squares).
pub fn accumulator_from_board(board: &[u8; 100], player: u8, weights: &NNUEWeights) -> Accumulator {
    let mut acc = Accumulator::new(weights);
    for sq in 0..100 {
        if let Some(feat) = feature_index(sq, board[sq], player) {
            acc.add_feature(feat, weights);
        }
    }
    acc
}
