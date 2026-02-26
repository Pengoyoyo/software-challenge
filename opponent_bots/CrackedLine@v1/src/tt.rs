use std::cmp::max;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Bound {
    Exact,
    Lower,
    Upper,
}

#[derive(Clone, Copy, Debug)]
pub struct TTEntry {
    pub key: u64,
    pub score: i32,
    pub mv: u16,
    pub depth: i16,
    pub bound: Bound,
    pub generation: u8,
}

impl Default for TTEntry {
    fn default() -> Self {
        Self {
            key: 0,
            score: 0,
            mv: 0,
            depth: 0,
            bound: Bound::Exact,
            generation: 0,
        }
    }
}

impl TTEntry {
    #[inline]
    pub fn empty(self) -> bool {
        self.key == 0
    }
}

#[derive(Clone, Copy, Debug, Default)]
pub struct TTCluster {
    pub entries: [TTEntry; 4],
}

#[derive(Clone)]
pub struct TranspositionTable {
    table: Vec<TTCluster>,
    mask: usize,
    generation: u8,
}

impl TranspositionTable {
    pub fn new(megabytes: usize) -> Self {
        let mut tt = Self {
            table: Vec::new(),
            mask: 0,
            generation: 1,
        };
        tt.resize(megabytes);
        tt
    }

    pub fn resize(&mut self, megabytes: usize) {
        let bytes = max(1, megabytes) * 1024 * 1024;
        let mut clusters = bytes / std::mem::size_of::<TTCluster>();
        if clusters < 1024 {
            clusters = 1024;
        }

        let mut p2 = 1_usize;
        while p2 < clusters {
            p2 <<= 1;
        }

        self.table = vec![TTCluster::default(); p2];
        self.mask = p2 - 1;
    }

    pub fn clear(&mut self) {
        for cluster in &mut self.table {
            *cluster = TTCluster::default();
        }
    }

    pub fn new_search(&mut self) {
        self.generation = self.generation.wrapping_add(1);
        if self.generation == 0 {
            self.generation = 1;
        }
    }

    #[inline]
    fn index(&self, key: u64) -> usize {
        (key as usize) & self.mask
    }

    #[inline]
    fn replacement_score(generation: u8, entry: TTEntry) -> i32 {
        if entry.empty() {
            return -1_000_000;
        }
        let age_penalty = (((generation.wrapping_sub(entry.generation)) as i32) & 0xff) * 12;
        let bound_bonus = match entry.bound {
            Bound::Exact => 32,
            Bound::Lower => 16,
            Bound::Upper => 8,
        };
        (entry.depth as i32) * 10 + bound_bonus - age_penalty
    }

    pub fn probe(&self, key: u64, depth: i32, alpha: i32, beta: i32) -> (bool, i32, u16) {
        let cluster = &self.table[self.index(key)];

        let mut out_move = 0_u16;
        let mut best_match: Option<TTEntry> = None;
        for entry in cluster.entries {
            if entry.key != key {
                continue;
            }

            out_move = entry.mv;
            let replace = match best_match {
                Some(current) => {
                    (entry.depth as i32) > (current.depth as i32)
                        || (entry.depth == current.depth && entry.generation > current.generation)
                }
                None => true,
            };
            if replace {
                best_match = Some(entry);
            }
        }

        let Some(entry) = best_match else {
            return (false, 0, out_move);
        };

        out_move = entry.mv;
        if (entry.depth as i32) < depth {
            return (false, 0, out_move);
        }

        match entry.bound {
            Bound::Exact => (true, entry.score, out_move),
            Bound::Lower if entry.score >= beta => (true, entry.score, out_move),
            Bound::Upper if entry.score <= alpha => (true, entry.score, out_move),
            _ => (false, 0, out_move),
        }
    }

    pub fn best_move(&self, key: u64) -> u16 {
        let cluster = &self.table[self.index(key)];
        let mut best: Option<TTEntry> = None;
        for entry in cluster.entries {
            if entry.key == key {
                let replace = match best {
                    Some(cur) => {
                        (entry.depth as i32) > (cur.depth as i32)
                            || (entry.depth == cur.depth && entry.generation > cur.generation)
                    }
                    None => true,
                };
                if replace {
                    best = Some(entry);
                }
            }
        }
        best.map(|e| e.mv).unwrap_or(0)
    }

    pub fn store(&mut self, key: u64, depth: i32, score: i32, bound: Bound, mv: u16) {
        let idx = self.index(key);
        let generation = self.generation;
        let cluster = &mut self.table[idx];

        let mut replace_idx = 0_usize;
        let mut replace_score = 1_000_000_i32;

        for (idx, entry) in cluster.entries.iter_mut().enumerate() {
            if entry.key == key {
                if depth >= entry.depth as i32
                    || bound == Bound::Exact
                    || entry.generation != self.generation
                {
                    entry.key = key;
                    entry.depth = depth as i16;
                    entry.score = score;
                    entry.bound = bound;
                    if mv != 0 {
                        entry.mv = mv;
                    }
                    entry.generation = self.generation;
                }
                return;
            }

            let rs = Self::replacement_score(generation, *entry);
            if rs < replace_score {
                replace_score = rs;
                replace_idx = idx;
            }
        }

        let entry = &mut cluster.entries[replace_idx];
        entry.key = key;
        entry.depth = depth as i16;
        entry.score = score;
        entry.bound = bound;
        entry.mv = mv;
        entry.generation = self.generation;
    }
}
