from __future__ import annotations

import logging
import time

from socha import Coordinate, Direction, FieldType, Move, RulesEngine, TeamEnum
from socha.api.networking.game_client import IClientHandler

try:
    from cython_core.bridge_cy import RustEngineBridge
except Exception:
    from cython_core.bridge import RustEngineBridge


TIME_BUDGET_MS = 1700
DIR_FROM_SIGN = {
    (0, 1): Direction.Up,
    (1, 1): Direction.UpRight,
    (1, 0): Direction.Right,
    (1, -1): Direction.DownRight,
    (0, -1): Direction.Down,
    (-1, -1): Direction.DownLeft,
    (-1, 0): Direction.Left,
    (-1, 1): Direction.UpLeft,
}

def _dir_label(direction: Direction) -> str:
    if direction == Direction.Up:
        return "Up \u2191"
    if direction == Direction.UpRight:
        return "UpRight \u2197"
    if direction == Direction.Right:
        return "Right \u2192"
    if direction == Direction.DownRight:
        return "DownRight \u2198"
    if direction == Direction.Down:
        return "Down \u2193"
    if direction == Direction.DownLeft:
        return "DownLeft \u2199"
    if direction == Direction.Left:
        return "Left \u2190"
    if direction == Direction.UpLeft:
        return "UpLeft \u2196"

    text = str(direction)
    if text.startswith("(") and text.endswith(")"):
        return text[1:-1]
    return text


def _encode_board(game_state) -> list[int]:
    encoded = [0] * 100

    for y in range(10):
        for x in range(10):
            sq = y * 10 + x
            field = game_state.board.get_field(Coordinate(x, y))

            if field == FieldType.Squid:
                encoded[sq] = 7
                continue

            team = field.get_team()
            if team is None:
                encoded[sq] = 0
                continue

            value = field.get_value()
            encoded[sq] = value if team == TeamEnum.One else value + 3

    return encoded


def _to_move(from_idx: int, to_idx: int) -> Move:
    from_x = from_idx % 10
    from_y = from_idx // 10
    to_x = to_idx % 10
    to_y = to_idx // 10

    dx = (to_x > from_x) - (to_x < from_x)
    dy = (to_y > from_y) - (to_y < from_y)
    direction = DIR_FROM_SIGN.get((dx, dy))
    if direction is None:
        raise ValueError(f"invalid move direction from {from_idx} to {to_idx}")

    return Move(Coordinate(from_x, from_y), direction)


class CythonRustLogic(IClientHandler):
    def __init__(self) -> None:
        self.game_state = None
        self.engine = RustEngineBridge()

    def on_update(self, game_state) -> None:
        self.game_state = game_state

    def calculate_move(self) -> Move:
        if self.game_state is None:
            raise RuntimeError("No game state available")

        t0 = time.perf_counter()
        game_state = self.game_state
        current_team = RulesEngine.get_team_on_turn(game_state.turn)
        current_player = 1 if current_team == TeamEnum.One else 2

        encoded = _encode_board(game_state)
        best = self.engine.choose_move(
            board_codes=encoded,
            current_player=current_player,
            turn=game_state.turn,
            time_ms=TIME_BUDGET_MS,
        )

        move: Move | None = None
        if best is not None:
            try:
                move = _to_move(best[0], best[1])
            except Exception as exc:
                logging.warning("Rust move conversion failed: %s", exc)

        if move is None:
            moves = game_state.possible_moves()
            if not moves:
                raise RuntimeError("No legal moves available")
            move = moves[0]

        label = _dir_label(move.direction)
        print(f"-> ({move.start.x}, {move.start.y}) ({label})", flush=True)
        elapsed = time.perf_counter() - t0
        logging.info(
            "Sent Move von (%d, %d) in Richtung (%s) after %.3f seconds.",
            move.start.x,
            move.start.y,
            label,
            elapsed,
        )
        return move

    def on_game_over(self, data) -> None:
        self.engine.close()


if __name__ == "__main__":
    from socha.starter import Starter

    Starter(CythonRustLogic())
