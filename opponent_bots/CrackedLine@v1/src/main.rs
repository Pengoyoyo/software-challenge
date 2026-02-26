use std::io::{self, BufRead, Write};

use crackedline_piranhas::eval::EvalWeights;
use crackedline_piranhas::search::{EngineConfig, SearchEngine};
use crackedline_piranhas::state::{init_tables, Move, Position, BLUE, RED};
use crackedline_piranhas::time_manager::TimeManager;

fn parse_u64_token(token: Option<&str>) -> Option<u64> {
    token?.trim().parse::<u64>().ok()
}

fn parse_u16_token(token: Option<&str>) -> Option<u16> {
    token?.trim().parse::<u16>().ok()
}

fn parse_u8_token(token: Option<&str>) -> Option<u8> {
    token?.trim().parse::<u8>().ok()
}

fn parse_hex_board(hex: &str) -> Option<[u8; 100]> {
    let trimmed = hex.trim();
    if trimmed.len() != 200 {
        return None;
    }

    let mut out = [0_u8; 100];
    let bytes = trimmed.as_bytes();
    for i in 0..100 {
        let hi = bytes[i * 2] as char;
        let lo = bytes[i * 2 + 1] as char;
        let pair = [hi, lo].iter().collect::<String>();
        out[i] = u8::from_str_radix(&pair, 16).ok()?;
    }

    Some(out)
}

fn parse_root_moves(token: &str) -> Vec<Move> {
    let trimmed = token.trim();
    if trimmed.is_empty() || trimmed == "-" {
        return Vec::new();
    }

    let mut out = Vec::new();
    for part in trimmed.split(',') {
        let part = part.trim();
        if part.is_empty() {
            continue;
        }
        if let Ok(encoded) = part.parse::<u16>() {
            out.push(Move::decode(encoded));
        }
    }
    out
}

fn bool_env(name: &str, default: bool) -> bool {
    match std::env::var(name) {
        Ok(value) => matches!(
            value.to_ascii_lowercase().as_str(),
            "1" | "true" | "yes" | "on"
        ),
        Err(_) => default,
    }
}

fn int_env(name: &str, default: i32) -> i32 {
    std::env::var(name)
        .ok()
        .and_then(|v| v.parse::<i32>().ok())
        .unwrap_or(default)
}

fn usize_env(name: &str, default: usize) -> usize {
    std::env::var(name)
        .ok()
        .and_then(|v| v.parse::<usize>().ok())
        .unwrap_or(default)
}

fn apply_eval_profile(weights: &mut EvalWeights, profile: &str) {
    match profile.trim().to_ascii_lowercase().as_str() {
        "" | "default" | "balanced" => {}
        "swarm" | "aggressive_swarm" => {
            weights.w_late_swarm_cohesion += 45;
            weights.w_late_fragment_pressure += 18;
            weights.w_late_disconnect_pressure += 2500;
            weights.w_threat_in1 += 2800;
            weights.w_threat_in2 += 1700;
            weights.w_race_connect1 += 1600;
            weights.w_race_connect2 += 1200;
            weights.w_race_side_to_move += 700;
            weights.w_bridge_risk += 8;
            weights.w_bridge_redundancy += 7;
            weights.w_no_move_pressure += 450;
            weights.w_cut_pressure += 5;
            weights.w_articulation_pressure += 6;
            weights.w_round_end_tempo += 550;
        }
        "disconnect" | "aggressive_disconnect" => {
            weights.w_late_disconnect_pressure += 5000;
            weights.w_late_fragment_pressure += 22;
            weights.w_threat_in1 += 1800;
            weights.w_threat_in2 += 1400;
            weights.w_race_disconnect1 += 2000;
            weights.w_race_disconnect2 += 1300;
            weights.w_safe_capture += 9;
            weights.w_cut_pressure += 8;
            weights.w_articulation_pressure += 10;
            weights.w_round_end_tempo += 350;
        }
        "mobility" => {
            weights.w_mobility += 4;
            weights.w_late_mobility += 5;
            weights.w_mobility_targets += 3;
            weights.w_no_move_pressure += 700;
            weights.w_collapse_risk += 50;
            weights.w_round_end_tempo += 250;
        }
        _ => {}
    }
}

fn apply_eval_weight(weights: &mut EvalWeights, key: &str, value: i32) -> bool {
    match key {
        "w_largest" => weights.w_largest = value,
        "w_components" => weights.w_components = value,
        "w_spread" => weights.w_spread = value,
        "w_count" => weights.w_count = value,
        "w_links" => weights.w_links = value,
        "w_center" => weights.w_center = value,
        "w_mobility" => weights.w_mobility = value,
        "w_mobility_targets" => weights.w_mobility_targets = value,
        "w_late_largest" => weights.w_late_largest = value,
        "w_late_components" => weights.w_late_components = value,
        "w_late_spread" => weights.w_late_spread = value,
        "w_late_links" => weights.w_late_links = value,
        "w_late_mobility" => weights.w_late_mobility = value,
        "w_bridge_risk" => weights.w_bridge_risk = value,
        "w_bridge_redundancy" => weights.w_bridge_redundancy = value,
        "w_threat_in1" => weights.w_threat_in1 = value,
        "w_threat_in2" => weights.w_threat_in2 = value,
        "w_safe_capture" => weights.w_safe_capture = value,
        "w_no_move_pressure" => weights.w_no_move_pressure = value,
        "w_late_swarm_cohesion" => weights.w_late_swarm_cohesion = value,
        "w_late_fragment_pressure" => weights.w_late_fragment_pressure = value,
        "w_late_disconnect_pressure" => weights.w_late_disconnect_pressure = value,
        "w_race_connect1" => weights.w_race_connect1 = value,
        "w_race_connect2" => weights.w_race_connect2 = value,
        "w_race_disconnect1" => weights.w_race_disconnect1 = value,
        "w_race_disconnect2" => weights.w_race_disconnect2 = value,
        "w_race_side_to_move" => weights.w_race_side_to_move = value,
        "w_cut_pressure" => weights.w_cut_pressure = value,
        "w_collapse_risk" => weights.w_collapse_risk = value,
        "w_articulation_pressure" => weights.w_articulation_pressure = value,
        "w_round_end_tempo" => weights.w_round_end_tempo = value,
        "connect_bonus" => weights.connect_bonus = value,
        _ => return false,
    }
    true
}

fn load_eval_weights_file(path: &str, base: &mut EvalWeights) {
    if path.trim().is_empty() {
        return;
    }

    let content = match std::fs::read_to_string(path) {
        Ok(content) => content,
        Err(_) => return,
    };

    for line in content.lines() {
        let trimmed = line.trim();
        if trimmed.is_empty() || trimmed.starts_with('#') {
            continue;
        }

        let Some((k, v)) = trimmed.split_once('=') else {
            continue;
        };
        let key = k.trim();
        let value = match v.trim().parse::<i32>() {
            Ok(value) => value,
            Err(_) => continue,
        };
        let _ = apply_eval_weight(base, key, value);
    }
}

fn load_config_from_env() -> EngineConfig {
    let mut config = EngineConfig::default();

    config.max_depth = int_env("PIRANHAS_MAX_DEPTH", config.max_depth);
    config.tt_mb = usize_env("PIRANHAS_TT_MB", 6144);
    config.enable_opening_book = bool_env("PIRANHAS_ENABLE_BOOK", config.enable_opening_book);
    config.book_path = std::env::var("PIRANHAS_BOOK_PATH").unwrap_or(config.book_path);
    config.policy_cache_path =
        std::env::var("PIRANHAS_POLICY_CACHE_PATH").unwrap_or(config.policy_cache_path);
    config.book_max_mb = usize_env("PIRANHAS_BOOK_MAX_MB", config.book_max_mb);
    config.book_force_confidence = int_env(
        "PIRANHAS_BOOK_FORCE_CONFIDENCE",
        config.book_force_confidence,
    );
    config.book_hint_confidence =
        int_env("PIRANHAS_BOOK_HINT_CONFIDENCE", config.book_hint_confidence);
    config.enable_subtree_reuse = bool_env("PIRANHAS_ENABLE_SUBTREE", config.enable_subtree_reuse);
    config.enable_reply_cache = bool_env("PIRANHAS_ENABLE_REPLY_CACHE", config.enable_reply_cache);
    config.enable_anti_shuffle =
        bool_env("PIRANHAS_ENABLE_ANTI_SHUFFLE", config.enable_anti_shuffle);

    if let Ok(profile) = std::env::var("PIRANHAS_EVAL_PROFILE") {
        apply_eval_profile(&mut config.eval_weights, &profile);
    }
    if let Ok(path) = std::env::var("PIRANHAS_EVAL_WEIGHTS_FILE") {
        load_eval_weights_file(&path, &mut config.eval_weights);
    }

    config
}

fn main() {
    init_tables();

    let mut engine = SearchEngine::new();
    engine.set_config(load_config_from_env());

    let stdin = io::stdin();
    let mut stdout = io::BufWriter::new(io::stdout());

    for line in stdin.lock().lines() {
        let Ok(line) = line else {
            break;
        };

        let trimmed = line.trim();
        if trimmed.is_empty() {
            continue;
        }

        if trimmed.eq_ignore_ascii_case("quit") {
            let _ = writeln!(stdout, "bye");
            let _ = stdout.flush();
            break;
        }

        if trimmed.eq_ignore_ascii_case("ping") {
            let _ = writeln!(stdout, "pong");
            let _ = stdout.flush();
            continue;
        }

        let mut parts = trimmed.split_whitespace();
        let Some(cmd) = parts.next() else {
            continue;
        };

        if cmd.eq_ignore_ascii_case("hash") {
            let player = parse_u8_token(parts.next()).unwrap_or(RED);
            let turn = parse_u16_token(parts.next()).unwrap_or(0);
            let board_hex = parts.next().unwrap_or("");

            if player != RED && player != BLUE {
                let _ = writeln!(stdout, "error invalid_player");
                let _ = stdout.flush();
                continue;
            }

            let Some(board) = parse_hex_board(board_hex) else {
                let _ = writeln!(stdout, "error invalid_board");
                let _ = stdout.flush();
                continue;
            };

            let mut position = Position::default();
            position.board = board;
            position.player_to_move = player;
            position.turn = turn;
            position.recompute_caches();
            let _ = writeln!(stdout, "hash {}", position.hash);
            let _ = stdout.flush();
            continue;
        }

        if !cmd.eq_ignore_ascii_case("search") {
            let _ = writeln!(stdout, "error unknown_command");
            let _ = stdout.flush();
            continue;
        }

        let budget_ns = parse_u64_token(parts.next())
            .unwrap_or(1_700_000_000)
            .min(1_850_000_000);
        let player = parse_u8_token(parts.next()).unwrap_or(RED);
        let turn = parse_u16_token(parts.next()).unwrap_or(0);
        let board_hex = parts.next().unwrap_or("");
        let root_moves_token = parts.next().unwrap_or("-");

        if player != RED && player != BLUE {
            let _ = writeln!(stdout, "error invalid_player");
            let _ = stdout.flush();
            continue;
        }

        let Some(board) = parse_hex_board(board_hex) else {
            let _ = writeln!(stdout, "error invalid_board");
            let _ = stdout.flush();
            continue;
        };

        let mut position = Position::default();
        position.board = board;
        position.player_to_move = player;
        position.turn = turn;
        position.recompute_caches();

        let deadline = TimeManager::now_ns().saturating_add(budget_ns);
        let root_moves = parse_root_moves(root_moves_token);
        let result = if root_moves.is_empty() {
            engine.search(position, deadline)
        } else {
            engine.search_with_root_moves(position, deadline, Some(root_moves.as_slice()))
        };
        let stats = result.stats;
        let iterations_blob = if result.iterations.is_empty() {
            String::from("-")
        } else {
            let mut parts = Vec::with_capacity(result.iterations.len());
            for it in &result.iterations {
                parts.push(format!(
                    "{},{},{},{},{},{}",
                    it.depth,
                    it.score,
                    it.nodes_delta,
                    it.tt_hits_delta,
                    it.elapsed_ns_delta,
                    it.nps_iter
                ));
            }
            parts.join(";")
        };
        let iter_count = result.iterations.len();

        if result.has_move {
            let _ = writeln!(
                stdout,
                "result 1 {} {} {} {} {} {} {} {} {} {} {} {} {} {} {} {} {} {} {} {}",
                result.best_move.from,
                result.best_move.to,
                result.score,
                result.depth,
                result.elapsed_ns,
                stats.nodes,
                stats.qnodes,
                stats.tt_probes,
                stats.tt_hits,
                stats.eval_calls,
                stats.reply_cache_hits,
                stats.anti_shuffle_hits,
                stats.subtree_reuse_hits,
                stats.book_hits,
                stats.verification_nodes,
                stats.singular_extensions,
                result.legal_root_count,
                result.team,
                iter_count,
                iterations_blob,
            );
        } else {
            let _ = writeln!(
                stdout,
                "result 0 -1 -1 {} {} {} {} {} {} {} {} {} {} {} {} {} {} {} {} {} {}",
                result.score,
                result.depth,
                result.elapsed_ns,
                stats.nodes,
                stats.qnodes,
                stats.tt_probes,
                stats.tt_hits,
                stats.eval_calls,
                stats.reply_cache_hits,
                stats.anti_shuffle_hits,
                stats.subtree_reuse_hits,
                stats.book_hits,
                stats.verification_nodes,
                stats.singular_extensions,
                result.legal_root_count,
                result.team,
                iter_count,
                iterations_blob,
            );
        }
        let _ = stdout.flush();
    }
}
