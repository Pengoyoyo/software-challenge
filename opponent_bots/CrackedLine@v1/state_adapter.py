from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

try:
    from socha import _socha as _socha_mod
except Exception:  # pragma: no cover - optional in tests
    _socha_mod = None

BOARD_SIZE = 10
NUM_SQUARES = BOARD_SIZE * BOARD_SIZE

RED = 1
BLUE = 2

EMPTY = 0
RED_1 = 1
RED_2 = 2
RED_3 = 3
BLUE_1 = 4
BLUE_2 = 5
BLUE_3 = 6
KRAKEN = 7


def xy_to_sq(x: int, y: int) -> int:
    return y * BOARD_SIZE + x


def sq_to_xy(square: int) -> tuple[int, int]:
    return square % BOARD_SIZE, square // BOARD_SIZE


def in_bounds(x: int, y: int) -> bool:
    return 0 <= x < BOARD_SIZE and 0 <= y < BOARD_SIZE


def opponent(player: int) -> int:
    return BLUE if player == RED else RED


def is_red_piece(cell: int) -> bool:
    return RED_1 <= int(cell) <= RED_3


def is_blue_piece(cell: int) -> bool:
    return BLUE_1 <= int(cell) <= BLUE_3


def is_fish_piece(cell: int) -> bool:
    return is_red_piece(cell) or is_blue_piece(cell)


def owner_of_cell(cell: int) -> int:
    value = int(cell)
    if is_red_piece(value):
        return RED
    if is_blue_piece(value):
        return BLUE
    return 0


def make_piece(player: int, value: int) -> int:
    clamped = max(1, min(3, int(value)))
    if player == RED:
        return RED_1 + clamped - 1
    return BLUE_1 + clamped - 1


@dataclass(frozen=True, slots=True)
class Move:
    from_sq: int
    to_sq: int

    def to_key(self) -> tuple[int, int]:
        return self.from_sq, self.to_sq


@dataclass(slots=True)
class ExternalState:
    board: list[int]
    player_to_move: int
    turn: int


def board_to_hex(board: Iterable[int]) -> str:
    return "".join(f"{int(cell) & 0xFF:02x}" for cell in board)


def _call_noarg_bool(value: Any, name: str) -> bool | None:
    attr = getattr(value, name, None)
    if attr is None:
        return None
    try:
        if callable(attr):
            return bool(attr())
        return bool(attr)
    except Exception:
        return None


def _normalize_player(value: Any) -> int | None:
    if value is None:
        return None

    if isinstance(value, int):
        if value in (RED, BLUE):
            return value

    if isinstance(value, str):
        upper = value.upper()
        if "RED" in upper or "ONE" in upper:
            return RED
        if "BLUE" in upper or "TWO" in upper:
            return BLUE

    name = getattr(value, "name", None)
    if isinstance(name, str):
        return _normalize_player(name)

    for attr in ("color", "owner", "player", "team"):
        nested = getattr(value, attr, None)
        resolved = _normalize_player(nested)
        if resolved is not None:
            return resolved

    try:
        text = str(value).upper()
    except Exception:
        return None

    if "RED" in text or "ONE" in text:
        return RED
    if "BLUE" in text or "TWO" in text:
        return BLUE

    return None


def _team_value_to_cell(team: Any, value: Any) -> int | None:
    player = _normalize_player(team)
    if player is None:
        return None

    try:
        fish_value = int(value)
    except Exception:
        return None

    if fish_value <= 0:
        return EMPTY
    if fish_value > 3:
        fish_value = 3
    return make_piece(player, fish_value)


def _value_to_cell(value: Any, depth: int = 0) -> int | None:
    if value is None:
        return EMPTY
    if depth > 6:
        return None

    get_team = getattr(value, "get_team", None)
    get_value = getattr(value, "get_value", None)
    if callable(get_team) and callable(get_value):
        try:
            team = get_team()
            fish_value = get_value()
            mapped = _team_value_to_cell(team, fish_value)
            if mapped is not None:
                return mapped
            if int(fish_value) == 0:
                text = str(value).upper()
                if "SQ" in text or "SQUID" in text or "KRAKEN" in text:
                    return KRAKEN
                return EMPTY
        except Exception:
            pass

    if (kraken_flag := _call_noarg_bool(value, "is_kraken")) is True:
        return KRAKEN
    if (empty_flag := _call_noarg_bool(value, "is_empty")) is True:
        return EMPTY

    if isinstance(value, int):
        if value in (EMPTY, RED_1, RED_2, RED_3, BLUE_1, BLUE_2, BLUE_3, KRAKEN):
            return value

    if isinstance(value, str):
        upper = value.upper()
        if upper in {"SQ", "SQUID", "KRAKEN"} or "SQUID" in upper or "KRAKEN" in upper:
            return KRAKEN
        if upper in {"--", "EMPTY", "NONE", "NULL"}:
            return EMPTY

        direct = {
            "O1": RED_1,
            "O2": RED_2,
            "O3": RED_3,
            "T1": BLUE_1,
            "T2": BLUE_2,
            "T3": BLUE_3,
            "ONES": RED_1,
            "ONEM": RED_2,
            "ONEL": RED_3,
            "TWOS": BLUE_1,
            "TWOM": BLUE_2,
            "TWOL": BLUE_3,
        }
        if upper in direct:
            return direct[upper]

        if "ONE" in upper or "RED" in upper or upper.startswith("O"):
            if "3" in upper or "L" in upper:
                return RED_3
            if "2" in upper or "M" in upper:
                return RED_2
            return RED_1

        if "TWO" in upper or "BLUE" in upper or upper.startswith("T"):
            if "3" in upper or "L" in upper:
                return BLUE_3
            if "2" in upper or "M" in upper:
                return BLUE_2
            return BLUE_1

    for attr in ("fish", "occupant", "piece"):
        nested = getattr(value, attr, None)
        if nested is not None:
            resolved = _value_to_cell(nested, depth + 1)
            if resolved is not None:
                return resolved

    for attr in ("owner", "player", "color", "team"):
        owner = getattr(value, attr, None)
        player = _normalize_player(owner)
        if player is None:
            continue

        for vattr in ("fish_value", "value", "size"):
            raw_value = getattr(value, vattr, None)
            if raw_value is None:
                continue
            try:
                return make_piece(player, int(raw_value))
            except Exception:
                continue
        return make_piece(player, 1)

    for attr in ("state", "type", "kind"):
        nested = getattr(value, attr, None)
        if nested is not None:
            resolved = _value_to_cell(nested, depth + 1)
            if resolved is not None:
                return resolved

    try:
        return _value_to_cell(str(value), depth + 1)
    except Exception:
        return None


def _read_board_field(board: Any, x: int, y: int) -> Any:
    getter = getattr(board, "get_field", None)
    if callable(getter):
        try:
            return getter(x, y)
        except Exception:
            if _socha_mod is not None:
                coord_cls = getattr(_socha_mod, "Coordinate", None)
                if coord_cls is not None:
                    try:
                        return getter(coord_cls(x=x, y=y))
                    except Exception:
                        pass

    fields = getattr(board, "fields", None)
    if fields is not None:
        try:
            return fields[y][x]
        except Exception:
            pass

    board_map = getattr(board, "map", None)
    if board_map is not None:
        try:
            return board_map[y][x]
        except Exception:
            pass

    try:
        return board[y][x]
    except Exception:
        return None


def _board_is_empty(board: Any, x: int, y: int) -> bool | None:
    checker = getattr(board, "is_empty", None)
    if callable(checker):
        try:
            return bool(checker(x, y))
        except Exception:
            if _socha_mod is not None:
                coord_cls = getattr(_socha_mod, "Coordinate", None)
                if coord_cls is not None:
                    try:
                        return bool(checker(coord_cls(x=x, y=y)))
                    except Exception:
                        return None
            return None
    return None


def external_position_to_square(position: Any) -> int | None:
    for x_attr in ("x", "col", "column"):
        x = getattr(position, x_attr, None)
        if x is None:
            continue
        for y_attr in ("y", "row"):
            y = getattr(position, y_attr, None)
            if y is None:
                continue
            try:
                xi = int(x)
                yi = int(y)
            except Exception:
                continue
            if in_bounds(xi, yi):
                return xy_to_sq(xi, yi)

    try:
        if isinstance(position, (tuple, list)) and len(position) == 2:
            x, y = int(position[0]), int(position[1])
            if in_bounds(x, y):
                return xy_to_sq(x, y)
    except Exception:
        return None

    return None


def _target_square_from_external_move(move: Any, board: Any | None = None) -> int | None:
    to_pos = (
        getattr(move, "to", None)
        or getattr(move, "to_value", None)
        or getattr(move, "target", None)
        or getattr(move, "end", None)
    )
    if to_pos is not None:
        sq = external_position_to_square(to_pos)
        if sq is not None:
            return sq

    if board is not None and _socha_mod is not None:
        rules = getattr(_socha_mod, "RulesEngine", None)
        if rules is not None:
            target_fn = getattr(rules, "target_position", None)
            if callable(target_fn):
                try:
                    target = target_fn(board, move)
                    sq = external_position_to_square(target)
                    if sq is not None:
                        return sq
                except Exception:
                    pass

    return None


def extract_external_move(move: Any, board: Any | None = None) -> Move | None:
    from_pos = (
        getattr(move, "start", None)
        or getattr(move, "from_", None)
        or getattr(move, "from_value", None)
        or getattr(move, "source", None)
    )
    if from_pos is None:
        return None

    from_sq = external_position_to_square(from_pos)
    to_sq = _target_square_from_external_move(move, board=board)
    if from_sq is None or to_sq is None:
        return None

    return Move(from_sq=from_sq, to_sq=to_sq)


def moves_to_lookup(external_moves: Iterable[Any], board: Any | None = None) -> dict[tuple[int, int], Any]:
    lookup: dict[tuple[int, int], Any] = {}
    for raw_move in external_moves:
        parsed = extract_external_move(raw_move, board=board)
        if parsed is None:
            continue
        lookup[parsed.to_key()] = raw_move
    return lookup


def from_game_state(game_state: Any) -> ExternalState:
    board_obj = getattr(game_state, "board", None)
    if board_obj is None:
        raise ValueError("game_state.board missing")

    board = [EMPTY] * NUM_SQUARES

    for y in range(BOARD_SIZE):
        for x in range(BOARD_SIZE):
            square = xy_to_sq(x, y)

            empty = _board_is_empty(board_obj, x, y)
            if empty is True:
                board[square] = EMPTY
                continue

            raw = _read_board_field(board_obj, x, y)
            value = _value_to_cell(raw)

            if value == KRAKEN:
                board[square] = KRAKEN
            elif value is not None and is_fish_piece(value):
                board[square] = int(value)
            else:
                board[square] = EMPTY

    player = _normalize_player(
        getattr(game_state, "current_player", None)
        or getattr(game_state, "currentPlayer", None)
        or getattr(game_state, "player", None)
    )

    if player is None:
        possible_moves_attr = getattr(game_state, "possible_moves", None)
        if callable(possible_moves_attr):
            try:
                possible_moves = list(possible_moves_attr())
            except Exception:
                possible_moves = []
            if possible_moves:
                first_start = (
                    getattr(possible_moves[0], "start", None)
                    or getattr(possible_moves[0], "from_", None)
                    or getattr(possible_moves[0], "source", None)
                )
                first_sq = external_position_to_square(first_start)
                if first_sq is not None:
                    inferred = owner_of_cell(board[first_sq])
                    if inferred in (RED, BLUE):
                        player = inferred

    if player is None:
        last_move = getattr(game_state, "last_move", None)
        if last_move is not None:
            moved_to = _target_square_from_external_move(last_move, board_obj)
            if moved_to is not None:
                inferred = owner_of_cell(board[moved_to])
                if inferred in (RED, BLUE):
                    player = opponent(inferred)

    if player is None:
        player = RED

    turn_value = (
        getattr(game_state, "turn", None)
        or getattr(game_state, "round", None)
        or getattr(game_state, "ply", None)
        or 0
    )

    try:
        turn = int(turn_value)
    except Exception:
        turn = 0

    return ExternalState(board=board, player_to_move=player, turn=turn)
