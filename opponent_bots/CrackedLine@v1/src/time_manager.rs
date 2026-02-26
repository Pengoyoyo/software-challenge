use std::cmp::max;
use std::sync::OnceLock;
use std::time::Instant;

pub struct TimeManager {
    start_ns: u64,
    deadline_ns: u64,
    last_check_ns: u64,
    timed_out: bool,
    nodes: u64,
    check_interval_mask: u32,
}

static BASE_INSTANT: OnceLock<Instant> = OnceLock::new();

impl TimeManager {
    pub fn new(deadline_ns: u64) -> Self {
        let start = Self::now_ns();
        Self {
            start_ns: start,
            deadline_ns,
            last_check_ns: start,
            timed_out: false,
            nodes: 0,
            check_interval_mask: 255,
        }
    }

    pub fn now_ns() -> u64 {
        let base = BASE_INSTANT.get_or_init(Instant::now);
        base.elapsed().as_nanos() as u64
    }

    pub fn timed_out(&mut self) -> bool {
        if self.timed_out {
            return true;
        }

        let now = Self::now_ns();
        self.last_check_ns = now;
        if now >= self.deadline_ns {
            self.timed_out = true;
        }
        self.timed_out
    }

    #[inline]
    pub fn hard_timeout(&mut self) -> bool {
        self.timed_out()
    }

    pub fn tick(&mut self) {
        self.nodes = self.nodes.wrapping_add(1);

        if (self.nodes as u32 & self.check_interval_mask) != 0 {
            return;
        }

        let now = Self::now_ns();
        self.last_check_ns = now;

        if now >= self.deadline_ns {
            self.timed_out = true;
            return;
        }

        let rem = self.deadline_ns - now;
        self.check_interval_mask = if rem < 80_000_000 {
            15
        } else if rem < 160_000_000 {
            31
        } else if rem < 320_000_000 {
            63
        } else if rem < 600_000_000 {
            127
        } else {
            255
        };
    }

    pub fn can_start_next_iteration(
        &self,
        recent_iterations_ns: &[u64],
        fail_events: i32,
        best_move_changes: i32,
    ) -> bool {
        let rem = self.remaining_ns();
        if rem <= 2_500_000 {
            return false;
        }

        let mut safety = 1_500_000_u64;
        safety = safety.saturating_add(max(0, fail_events) as u64 * 500_000);
        safety = safety.saturating_add(max(0, best_move_changes) as u64 * 350_000);

        let mut predicted = if recent_iterations_ns.is_empty() {
            14_000_000
        } else {
            let n = recent_iterations_ns.len().min(6);
            let start = recent_iterations_ns.len() - n;
            let mut weighted_sum = 0_u128;
            let mut weights = 0_u128;
            for (idx, value) in recent_iterations_ns[start..].iter().copied().enumerate() {
                let w = (idx + 1) as u128;
                weighted_sum = weighted_sum.saturating_add((value as u128).saturating_mul(w));
                weights = weights.saturating_add(w);
            }
            if weights == 0 {
                18_000_000
            } else {
                (weighted_sum / weights) as u64
            }
        };

        if recent_iterations_ns.len() >= 2 {
            let last = recent_iterations_ns[recent_iterations_ns.len() - 1];
            let prev = recent_iterations_ns[recent_iterations_ns.len() - 2];
            if prev > 0 {
                let ratio_permille = ((last.saturating_mul(1000)) / prev).clamp(850, 1750);
                let trend_permille = ratio_permille.saturating_add(20);
                predicted = predicted.saturating_mul(trend_permille) / 1000;
            }
        }

        let mut growth_permille = 1060_i32;
        growth_permille += std::cmp::min(
            140,
            max(0, fail_events) * 24 + max(0, best_move_changes) * 20,
        );
        predicted = predicted.saturating_mul(growth_permille as u64) / 1000;
        predicted = max(predicted as i32, 3_000_000) as u64;

        rem > predicted.saturating_add(safety)
    }

    pub fn elapsed_ns(&self) -> u64 {
        Self::now_ns().saturating_sub(self.start_ns)
    }

    pub fn remaining_ns(&self) -> u64 {
        let now = Self::now_ns();
        self.deadline_ns.saturating_sub(now)
    }

    pub fn nodes(&self) -> u64 {
        self.nodes
    }
}
