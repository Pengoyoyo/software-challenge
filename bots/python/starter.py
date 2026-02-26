from typing import Callable

from socha import GameState, Move, RulesEngine, TeamEnum
from socha.api.networking.game_client import IClientHandler
from socha.starter import Starter


def start(evaluate: Callable[[GameState, TeamEnum, TeamEnum], float]) -> None:
    """Start the bot with the given evaluation function."""

    class Logic(IClientHandler):
        def __init__(self) -> None:
            self.game_state: GameState | None = None

        def on_update(self, game_state: GameState) -> None:
            self.game_state = game_state

        def evaluate(self, move: Move) -> float:
            assert self.game_state is not None

            new_game_state = self.game_state.perform_move(move)

            assert evaluate(new_game_state, TeamEnum.One, TeamEnum.Two) == -evaluate(new_game_state, TeamEnum.Two, TeamEnum.One)
            assert evaluate(new_game_state, TeamEnum.One, TeamEnum.One) == 0
            assert evaluate(new_game_state, TeamEnum.Two, TeamEnum.Two) == 0

            return evaluate(
                new_game_state,
                RulesEngine.get_team_on_turn(self.game_state.turn),
                RulesEngine.get_team_on_turn(self.game_state.turn).opponent(),
            )

        def calculate_move(self) -> Move:
            assert self.game_state is not None
            return max(self.game_state.possible_moves(), key=self.evaluate)

    Starter(Logic())
