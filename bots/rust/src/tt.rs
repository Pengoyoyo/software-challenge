// ─── Bound constants ──────────────────────────────────────────────────────────
pub const EXACT: u8 = 0;
pub const LOWER: u8 = 1; // fail-high / beta cutoff
pub const UPPER: u8 = 2; // fail-low  / alpha cutoff

// ─── TT Entry ─────────────────────────────────────────────────────────────────

#[derive(Clone, Copy)]
struct TTEntry {
    key: u64,
    score: i32,
    best_move: u16,
    depth: i16,
    bound: u8,
    generation: u8,
}

impl TTEntry {
    const fn empty() -> Self {
        TTEntry {
            key: 0,
            score: 0,
            best_move: 0,
            depth: -1,
            bound: EXACT,
            generation: 0,
        }
    }

    fn is_empty(&self) -> bool {
        self.depth < 0 && self.key == 0
    }
}

// ─── TT Cluster (4 entries) ───────────────────────────────────────────────────

#[derive(Clone, Copy)]
struct TTCluster {
    entries: [TTEntry; 4],
}

impl TTCluster {
    const fn empty() -> Self {
        TTCluster {
            entries: [TTEntry::empty(); 4],
        }
    }
}

// ─── Transposition Table ──────────────────────────────────────────────────────

pub struct TranspositionTable {
    table: Vec<TTCluster>,
    mask: usize,
    generation: u8,
}

impl TranspositionTable {
    pub fn new(mb: usize) -> Self {
        let bytes = mb.max(1) * 1024 * 1024;
        let cluster_size = std::mem::size_of::<TTCluster>();
        let clusters = (bytes / cluster_size).max(1024);

        // Round up to next power of two
        let mut p2 = 1usize;
        while p2 < clusters {
            p2 <<= 1;
        }

        TranspositionTable {
            table: vec![TTCluster::empty(); p2],
            mask: p2 - 1,
            generation: 1,
        }
    }

    #[inline(always)]
    pub fn new_search(&mut self) {
        self.generation = self.generation.wrapping_add(1).max(1);
    }

    #[inline(always)]
    fn index(&self, key: u64) -> usize {
        (key as usize) & self.mask
    }

    /// Returns (Some(score), tt_move) if a usable hit was found.
    /// Returns (None, tt_move) if the key matched but depth was insufficient.
    /// Returns (None, 0) if no match.
    pub fn probe(&self, key: u64, depth: i32, alpha: i32, beta: i32) -> (Option<i32>, u16) {
        let cluster = &self.table[self.index(key)];

        for entry in &cluster.entries {
            if entry.key != key {
                continue;
            }

            let tt_move = entry.best_move;

            if entry.depth < depth as i16 {
                return (None, tt_move);
            }

            let score = entry.score;
            let hit = match entry.bound {
                EXACT => true,
                LOWER => score >= beta,
                UPPER => score <= alpha,
                _ => false,
            };

            return if hit { (Some(score), tt_move) } else { (None, tt_move) };
        }

        (None, 0)
    }

    /// Retrieve best move only (no depth check).
    pub fn best_move(&self, key: u64) -> u16 {
        let cluster = &self.table[self.index(key)];
        for entry in &cluster.entries {
            if entry.key == key {
                return entry.best_move;
            }
        }
        0
    }

    pub fn store(&mut self, key: u64, depth: i32, score: i32, bound: u8, mv: u16) {
        let idx = self.index(key);
        let cluster = &mut self.table[idx];

        // Check for an existing entry with the same key
        for entry in cluster.entries.iter_mut() {
            if entry.key == key {
                if depth as i16 >= entry.depth || bound == EXACT {
                    entry.depth = depth as i16;
                    entry.score = score;
                    entry.bound = bound;
                    if mv != 0 {
                        entry.best_move = mv;
                    }
                    entry.generation = self.generation;
                }
                return;
            }
        }

        // Replace the entry with the lowest replacement score (inlined to avoid borrow conflict)
        let gen = self.generation;
        let mut replace_idx = 0usize;
        let mut replace_score = i32::MAX;
        for (i, entry) in cluster.entries.iter().enumerate() {
            let rs = if entry.is_empty() {
                -1_000_000
            } else {
                let age = (gen.wrapping_sub(entry.generation) & 0xFF) as i32;
                entry.depth as i32 - age * 2
            };
            if rs < replace_score {
                replace_score = rs;
                replace_idx = i;
            }
        }

        let entry = &mut cluster.entries[replace_idx];
        entry.key = key;
        entry.depth = depth as i16;
        entry.score = score;
        entry.bound = bound;
        entry.best_move = mv;
        entry.generation = self.generation;
    }
}
