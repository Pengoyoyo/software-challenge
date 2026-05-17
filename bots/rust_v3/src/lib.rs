pub mod bitboard;
pub mod board;
pub mod evaluate;
pub mod nnue;
pub mod search;
pub mod tt;

// Re-export nnue_eval so evaluate.rs can reach it as a crate-level function
// regardless of compilation context.
#[cfg(has_nnue)]
pub use nnue::nnue_eval;
