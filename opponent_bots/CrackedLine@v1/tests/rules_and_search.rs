use crackedline_piranhas::eval::{
    evaluate_hce, round_end_connection_outcome, ROUND_CONNECT_BOTH, ROUND_CONNECT_NONE,
};
use crackedline_piranhas::movegen::generate_moves;
use crackedline_piranhas::search::{EngineConfig, SearchEngine};
use crackedline_piranhas::state::{
    make_piece, setup_initial_position, Move, Position, Undo, BLUE, BLUE_1, EMPTY, KRAKEN, RED,
    RED_1, RED_3, WIN_SCORE,
};
use crackedline_piranhas::time_manager::TimeManager;
use std::fs::File;
use std::io::Write;

fn empty_position() -> Position {
    let mut pos = Position::default();
    pos.board = [EMPTY; crackedline_piranhas::state::NUM_SQUARES];
    pos.player_to_move = RED;
    pos.turn = 0;
    pos.recompute_caches();
    pos
}

#[test]
fn make_unmake_restores_state_and_hash() {
    let mut pos = empty_position();
    pos.board[11] = RED_1;
    pos.board[22] = BLUE_1;
    pos.player_to_move = RED;
    pos.recompute_caches();

    let original = pos.clone();

    let mv = Move::new(11, 22);
    let mut undo = Undo::default();
    assert!(pos.make_move(mv, &mut undo));
    pos.unmake_move(&undo);

    assert_eq!(pos.board, original.board);
    assert_eq!(pos.hash, original.hash);
    assert_eq!(pos.player_to_move, original.player_to_move);
    assert_eq!(pos.turn, original.turn);
    assert_eq!(pos.red_count, original.red_count);
    assert_eq!(pos.blue_count, original.blue_count);
}

#[test]
fn blocks_opponent_on_path_but_allows_capture_on_target() {
    let mut blocked = empty_position();
    blocked.board[0] = RED_1;
    blocked.board[10] = BLUE_1;
    blocked.board[20] = BLUE_1;
    blocked.player_to_move = RED;
    blocked.recompute_caches();

    let mut moves = Vec::new();
    generate_moves(&blocked, RED, &mut moves);
    assert!(!moves.iter().any(|mv| mv.from == 0 && mv.to == 30));

    let mut capture = empty_position();
    capture.board[1] = RED_1;
    capture.board[21] = BLUE_1;
    capture.player_to_move = RED;
    capture.recompute_caches();

    moves.clear();
    generate_moves(&capture, RED, &mut moves);
    assert!(moves.iter().any(|mv| mv.from == 1 && mv.to == 21));
}

#[test]
fn largest_component_value_is_value_based() {
    let mut pos = empty_position();
    pos.board[0] = RED_1;
    pos.board[1] = RED_1;
    pos.board[99] = RED_3;
    pos.recompute_caches();

    assert_eq!(pos.largest_component_value(RED), 3);
}

#[test]
fn search_returns_legal_move_on_initial_position() {
    let mut pos = Position::default();
    setup_initial_position(&mut pos, 7);

    let mut legal = Vec::new();
    generate_moves(&pos, pos.player_to_move, &mut legal);
    assert!(!legal.is_empty());

    let mut engine = SearchEngine::new();
    let mut config = EngineConfig::default();
    config.max_depth = 3;
    config.enable_opening_book = false;
    config.tt_mb = 16;
    engine.set_config(config);

    let deadline = TimeManager::now_ns() + 150_000_000;
    let result = engine.search(pos.clone(), deadline);

    assert!(result.has_move);
    assert!(legal.contains(&result.best_move));
}

#[test]
fn search_is_deterministic_for_same_config_and_position() {
    let mut pos = empty_position();
    pos.board[11] = make_piece(RED, 2);
    pos.board[22] = make_piece(RED, 1);
    pos.board[33] = make_piece(BLUE, 3);
    pos.board[44] = make_piece(BLUE, 1);
    pos.player_to_move = RED;
    pos.turn = 12;
    pos.recompute_caches();

    let mut config = EngineConfig::default();
    config.max_depth = 2;
    config.enable_opening_book = false;
    config.tt_mb = 16;

    let mut engine_a = SearchEngine::new();
    let mut engine_b = SearchEngine::new();
    engine_a.set_config(config.clone());
    engine_b.set_config(config);

    let deadline_a = TimeManager::now_ns() + 120_000_000;
    let deadline_b = TimeManager::now_ns() + 120_000_000;

    let a = engine_a.search(pos.clone(), deadline_a);
    let b = engine_b.search(pos, deadline_b);

    assert_eq!(a.has_move, b.has_move);
    assert_eq!(a.best_move, b.best_move);
    assert_eq!(a.score, b.score);
    assert_eq!(a.depth, b.depth);
}

#[test]
fn root_filter_restricts_search_to_given_moves() {
    let mut pos = Position::default();
    setup_initial_position(&mut pos, 11);

    let mut legal = Vec::new();
    generate_moves(&pos, pos.player_to_move, &mut legal);
    assert!(legal.len() > 2);
    let forced = legal[1];

    let mut engine = SearchEngine::new();
    let mut config = EngineConfig::default();
    config.max_depth = 4;
    config.enable_opening_book = false;
    config.tt_mb = 16;
    engine.set_config(config);

    let deadline = TimeManager::now_ns() + 120_000_000;
    let result = engine.search_with_root_moves(pos, deadline, Some(&[forced]));

    assert!(result.has_move);
    assert_eq!(result.legal_root_count, 1);
    assert_eq!(result.best_move, forced);
}

#[test]
fn iteration_traces_are_recorded_consistently() {
    let mut pos = Position::default();
    setup_initial_position(&mut pos, 5);

    let mut engine = SearchEngine::new();
    let mut config = EngineConfig::default();
    config.max_depth = 5;
    config.enable_opening_book = false;
    config.tt_mb = 16;
    engine.set_config(config);

    let deadline = TimeManager::now_ns() + 140_000_000;
    let result = engine.search(pos, deadline);

    assert!(result.has_move);
    assert!(!result.iterations.is_empty());
    assert!(result.depth as usize <= result.iterations.len());
    for (idx, trace) in result.iterations.iter().enumerate() {
        assert_eq!(trace.depth as usize, idx + 1);
        assert!(trace.elapsed_ns_delta > 0);
    }
}

#[test]
fn timecap_stress_stays_within_safe_margin() {
    let mut pos = Position::default();
    setup_initial_position(&mut pos, 19);

    let mut engine = SearchEngine::new();
    let mut config = EngineConfig::default();
    config.max_depth = 6;
    config.enable_opening_book = false;
    config.tt_mb = 16;
    engine.set_config(config);

    let budget_ns = 40_000_000_u64;
    let allowed_margin_ns = 120_000_000_u64;
    for _ in 0..8 {
        let deadline = TimeManager::now_ns() + budget_ns;
        let result = engine.search(pos.clone(), deadline);
        assert!(result.elapsed_ns <= allowed_margin_ns);
    }
}

#[test]
fn connectivity_is_terminal_only_at_round_end() {
    let mut pos = empty_position();
    pos.board[11] = RED_1;
    pos.board[12] = RED_1;
    pos.board[77] = BLUE_1;
    pos.board[99] = BLUE_1;

    // Mid-round: blue may still break red's connection.
    pos.player_to_move = BLUE;
    pos.turn = 1;
    pos.recompute_caches();

    assert_eq!(round_end_connection_outcome(&pos), ROUND_CONNECT_NONE);
    let mut mid = pos.clone();
    let mid_score = evaluate_hce(&mut mid, RED, 0);
    assert!(mid_score < WIN_SCORE / 2);

    // End of round: same structure is now terminal for red.
    pos.player_to_move = RED;
    pos.turn = 2;
    pos.recompute_caches();

    let mut end = pos.clone();
    let end_score = evaluate_hce(&mut end, RED, 0);
    assert_eq!(end_score, WIN_SCORE - pos.turn as i32);
}

#[test]
fn both_connected_at_round_end_uses_tiebreak() {
    let mut pos = empty_position();
    pos.board[11] = RED_3;
    pos.board[12] = RED_1;
    pos.board[77] = BLUE_1;
    pos.board[78] = BLUE_1;
    pos.player_to_move = RED;
    pos.turn = 2;
    pos.recompute_caches();

    assert_eq!(round_end_connection_outcome(&pos), ROUND_CONNECT_BOTH);

    let mut as_red = pos.clone();
    let mut as_blue = pos.clone();
    let red_score = evaluate_hce(&mut as_red, RED, 0);
    let blue_score = evaluate_hce(&mut as_blue, BLUE, 0);

    assert_eq!(red_score, WIN_SCORE - pos.turn as i32);
    assert_eq!(blue_score, -WIN_SCORE + pos.turn as i32);
}

#[test]
fn no_move_is_immediate_loss() {
    let mut pos = empty_position();
    pos.board[0] = RED_1;
    pos.board[57] = BLUE_1;
    for sq in [1_usize, 10, 11] {
        pos.board[sq] = KRAKEN;
    }
    pos.player_to_move = RED;
    pos.turn = 1;
    pos.recompute_caches();

    let mut legal = Vec::new();
    generate_moves(&pos, RED, &mut legal);
    assert!(legal.is_empty());

    let mut engine = SearchEngine::new();
    let mut config = EngineConfig::default();
    config.max_depth = 4;
    config.enable_opening_book = false;
    config.tt_mb = 16;
    engine.set_config(config);

    let deadline = TimeManager::now_ns() + 120_000_000;
    let result = engine.search(pos, deadline);
    assert!(!result.has_move);
    assert_eq!(result.score, -WIN_SCORE);
}

#[test]
fn bridge_fragility_monotonicity() {
    let mut fragile = empty_position();
    fragile.board[44] = RED_3;
    fragile.board[45] = RED_1;
    fragile.board[54] = RED_1;
    fragile.board[22] = BLUE_1;
    fragile.board[77] = BLUE_1;
    fragile.player_to_move = BLUE;
    fragile.turn = 7;
    fragile.recompute_caches();

    let mut robust = fragile.clone();
    robust.board[55] = RED_1;
    robust.recompute_caches();

    let mut a = fragile.clone();
    let mut b = robust.clone();
    let fragile_score = evaluate_hce(&mut a, RED, 0);
    let robust_score = evaluate_hce(&mut b, RED, 0);
    assert!(robust_score > fragile_score);
}

#[test]
fn connectivity_threat_reacts_to_side_to_move() {
    let mut blue_to_move = empty_position();
    blue_to_move.board[11] = RED_1;
    blue_to_move.board[12] = RED_1;
    blue_to_move.board[77] = BLUE_1;
    blue_to_move.board[99] = BLUE_1;
    blue_to_move.player_to_move = BLUE;
    blue_to_move.turn = 0;
    blue_to_move.recompute_caches();

    let mut red_to_move = blue_to_move.clone();
    red_to_move.player_to_move = RED;
    red_to_move.recompute_caches();

    let mut x = blue_to_move.clone();
    let mut y = red_to_move.clone();
    let blue_move_score = evaluate_hce(&mut x, RED, 0);
    let red_move_score = evaluate_hce(&mut y, RED, 0);
    assert_ne!(red_move_score, blue_move_score);
}

#[test]
fn late_race_reacts_to_side_to_move() {
    let mut blue_to_move = empty_position();
    blue_to_move.board[11] = RED_1;
    blue_to_move.board[12] = RED_1;
    blue_to_move.board[77] = BLUE_1;
    blue_to_move.board[99] = BLUE_1;
    blue_to_move.player_to_move = BLUE;
    blue_to_move.turn = 58;
    blue_to_move.recompute_caches();

    let mut red_to_move = blue_to_move.clone();
    red_to_move.player_to_move = RED;
    red_to_move.recompute_caches();

    let mut a = blue_to_move.clone();
    let mut b = red_to_move.clone();
    let blue_score = evaluate_hce(&mut a, RED, 0);
    let red_score = evaluate_hce(&mut b, RED, 0);
    assert!(red_score > blue_score);
}

#[test]
fn late_cohesion_prefers_connected_swarm() {
    let mut connected = empty_position();
    connected.board[44] = RED_1;
    connected.board[45] = RED_1;
    connected.board[54] = RED_1;
    connected.board[10] = BLUE_1;
    connected.board[11] = BLUE_1;
    connected.board[12] = BLUE_1;
    connected.player_to_move = BLUE;
    connected.turn = 50;
    connected.recompute_caches();

    let mut fragmented = connected.clone();
    fragmented.board[45] = EMPTY;
    fragmented.board[54] = EMPTY;
    fragmented.board[20] = RED_1;
    fragmented.board[88] = RED_1;
    fragmented.recompute_caches();

    let mut a = connected.clone();
    let mut b = fragmented.clone();
    let connected_score = evaluate_hce(&mut a, RED, 0);
    let fragmented_score = evaluate_hce(&mut b, RED, 0);
    assert!(connected_score > fragmented_score);
}

#[test]
fn late_fragment_pressure_rewards_opponent_fragmentation() {
    let mut compact = empty_position();
    compact.board[0] = RED_1;
    compact.board[9] = RED_1;
    compact.board[44] = BLUE_1;
    compact.board[45] = BLUE_1;
    compact.board[54] = BLUE_1;
    compact.player_to_move = BLUE;
    compact.turn = 50;
    compact.recompute_caches();

    let mut fragmented = compact.clone();
    fragmented.board[45] = EMPTY;
    fragmented.board[54] = EMPTY;
    fragmented.board[20] = BLUE_1;
    fragmented.board[88] = BLUE_1;
    fragmented.recompute_caches();

    let mut a = compact.clone();
    let mut b = fragmented.clone();
    let compact_score = evaluate_hce(&mut a, RED, 0);
    let fragmented_score = evaluate_hce(&mut b, RED, 0);
    assert!(fragmented_score > compact_score);
}

#[test]
fn terminal_equal_largest_swarm_is_draw() {
    let mut pos = empty_position();
    pos.board[11] = make_piece(RED, 2);
    pos.board[88] = BLUE_1;
    pos.board[89] = BLUE_1;
    pos.player_to_move = RED;
    pos.turn = 2;
    pos.recompute_caches();

    assert_eq!(round_end_connection_outcome(&pos), ROUND_CONNECT_BOTH);

    let mut as_red = pos.clone();
    let mut as_blue = pos.clone();
    assert_eq!(evaluate_hce(&mut as_red, RED, 0), 0);
    assert_eq!(evaluate_hce(&mut as_blue, BLUE, 0), 0);
}

#[test]
fn opening_policy_cache_forces_move_when_confident() {
    let mut pos = Position::default();
    setup_initial_position(&mut pos, 9);

    let mut legal = Vec::new();
    generate_moves(&pos, pos.player_to_move, &mut legal);
    assert!(legal.len() > 3);
    let forced = legal[3];
    let hash = pos.hash;

    let path = std::env::temp_dir().join("crackedline_policy_cache_test.bin");
    let mut file = File::create(&path).expect("create policy cache");
    file.write_all(b"OPC1").expect("write magic");
    file.write_all(&hash.to_le_bytes()).expect("write hash");
    file.write_all(&forced.encode().to_le_bytes())
        .expect("write best");
    file.write_all(&0_u16.to_le_bytes()).expect("write alt1");
    file.write_all(&0_u16.to_le_bytes()).expect("write alt2");
    file.write_all(&123_i16.to_le_bytes()).expect("write score");
    file.write_all(&11_u8.to_le_bytes()).expect("write depth");
    file.write_all(&9_u16.to_le_bytes()).expect("write samples");
    file.write_all(&92_u8.to_le_bytes())
        .expect("write confidence");
    file.flush().expect("flush policy cache");

    let mut engine = SearchEngine::new();
    let mut config = EngineConfig::default();
    config.max_depth = 6;
    config.tt_mb = 16;
    config.enable_opening_book = true;
    config.book_path = String::new();
    config.policy_cache_path = path.to_string_lossy().to_string();
    config.policy_cache_turn_max = 14;
    config.book_force_confidence = 85;
    config.book_hint_confidence = 65;
    config.book_min_samples = 6;
    engine.set_config(config);

    let deadline = TimeManager::now_ns() + 120_000_000;
    let result = engine.search(pos, deadline);

    assert!(result.has_move);
    assert_eq!(result.best_move, forced);
    assert_eq!(result.stats.book_forced_hits, 1);
    assert_eq!(result.depth, 11);

    let _ = std::fs::remove_file(path);
}
