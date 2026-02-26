from __future__ import annotations

import os
import queue
from dataclasses import dataclass
from pathlib import Path
import subprocess
import threading
from typing import Iterable, TextIO

from state_adapter import ExternalState, board_to_hex


@dataclass(slots=True)
class DepthTrace:
    depth: int
    score: int
    nodes_delta: int
    tt_hits_delta: int
    elapsed_ns_delta: int
    nps_iter: int


@dataclass(slots=True)
class RustSearchResult:
    has_move: bool
    from_sq: int
    to_sq: int
    score: int
    depth: int
    elapsed_ns: int
    nodes: int
    qnodes: int
    tt_probes: int
    tt_hits: int
    eval_calls: int
    reply_cache_hits: int
    anti_shuffle_hits: int
    subtree_reuse_hits: int
    book_hits: int
    verification_nodes: int
    singular_extensions: int
    legal_root_count: int
    team: int
    iterations: list[DepthTrace]


class RustEngineProcess:
    def __init__(
        self,
        root: Path | None = None,
        binary: Path | None = None,
        env_overrides: dict[str, str] | None = None,
    ) -> None:
        self.root = (root or Path(__file__).resolve().parent).resolve()
        self.binary = binary or (self.root / "target" / "release" / "piranhas-rs-engine")
        self.env_overrides = dict(env_overrides or {})
        self._proc: subprocess.Popen[str] | None = None
        self._lock = threading.Lock()
        self._max_budget_ns = 1_850_000_000

    def _build_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env.setdefault("RUST_BACKTRACE", "0")
        for key, value in self.env_overrides.items():
            env[str(key)] = str(value)
        return env

    def start(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            return

        if not self.binary.exists():
            raise RuntimeError(f"Rust binary not found: {self.binary}")

        self._proc = subprocess.Popen(
            [str(self.binary)],
            cwd=self.root,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=self._build_env(),
        )

        pong = self._request_raw("ping")
        if pong.strip().lower() != "pong":
            self.close()
            raise RuntimeError(f"Rust engine handshake failed: {pong!r}")

    def close(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return

        try:
            if proc.poll() is None:
                try:
                    self._request_raw("quit", timeout_fallback=True, timeout_ns=120_000_000)
                except Exception:
                    pass
                proc.terminate()
                proc.wait(timeout=0.5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def _abort_process(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        if proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass

    def force_abort(self) -> None:
        self._abort_process()

    def _readline_with_timeout(self, stdout: TextIO, timeout_ns: int | None) -> str:
        if timeout_ns is None:
            return stdout.readline()

        timeout_s = max(0.0, timeout_ns / 1_000_000_000)
        if timeout_s <= 0.0:
            raise TimeoutError("Rust engine timed out before read")

        out_q: queue.Queue[str | Exception] = queue.Queue(maxsize=1)

        def _reader() -> None:
            try:
                out_q.put(stdout.readline())
            except Exception as exc:  # pragma: no cover - defensive
                out_q.put(exc)

        thread = threading.Thread(target=_reader, daemon=True)
        thread.start()
        try:
            item = out_q.get(timeout=timeout_s)
        except queue.Empty as exc:
            raise TimeoutError(f"Rust engine read timeout after {timeout_s:.3f}s") from exc

        if isinstance(item, Exception):
            raise RuntimeError("Rust engine read failed") from item
        return item

    def _request_raw(
        self,
        command: str,
        timeout_fallback: bool = False,
        timeout_ns: int | None = None,
    ) -> str:
        if self._proc is None or self._proc.poll() is not None:
            self.start()

        assert self._proc is not None
        stdin = self._proc.stdin
        stdout = self._proc.stdout
        if stdin is None or stdout is None:
            raise RuntimeError("Rust engine IO streams unavailable")

        try:
            stdin.write(command + "\n")
            stdin.flush()
        except BrokenPipeError as exc:
            self.close()
            if timeout_fallback:
                return ""
            raise RuntimeError("Rust engine pipe closed") from exc

        try:
            line = self._readline_with_timeout(stdout, timeout_ns)
        except TimeoutError as exc:
            self._abort_process()
            if timeout_fallback:
                return ""
            raise RuntimeError(str(exc)) from exc
        if line == "":
            stderr_text = ""
            if self._proc.stderr is not None:
                try:
                    stderr_text = self._proc.stderr.read().strip()
                except Exception:
                    stderr_text = ""
            self.close()
            if timeout_fallback:
                return ""
            raise RuntimeError(f"Rust engine returned EOF (stderr={stderr_text!r})")

        return line.rstrip("\n")

    def _parse_iteration_blob(self, blob: str, expected_count: int) -> list[DepthTrace]:
        if blob == "-" or expected_count <= 0:
            return []

        traces: list[DepthTrace] = []
        entries = [entry for entry in blob.split(";") if entry]
        for entry in entries[:expected_count]:
            parts = entry.split(",")
            if len(parts) != 6:
                continue
            try:
                traces.append(
                    DepthTrace(
                        depth=int(parts[0]),
                        score=int(parts[1]),
                        nodes_delta=int(parts[2]),
                        tt_hits_delta=int(parts[3]),
                        elapsed_ns_delta=int(parts[4]),
                        nps_iter=int(parts[5]),
                    )
                )
            except ValueError:
                continue
        return traces

    def _parse_result(self, line: str) -> RustSearchResult:
        tokens = line.strip().split()
        if not tokens:
            raise RuntimeError("Empty response from Rust engine")

        if tokens[0] == "error":
            raise RuntimeError("Rust engine error: " + " ".join(tokens[1:]))

        if tokens[0] != "result":
            raise RuntimeError(f"Unexpected Rust engine response: {line!r}")

        if len(tokens) < 22:
            raise RuntimeError(f"Incomplete Rust engine response: {line!r}")

        values = [int(part) for part in tokens[1:21]]
        has_move = values[0] == 1
        iterations = self._parse_iteration_blob(tokens[21], values[19])

        return RustSearchResult(
            has_move=has_move,
            from_sq=values[1],
            to_sq=values[2],
            score=values[3],
            depth=values[4],
            elapsed_ns=values[5],
            nodes=values[6],
            qnodes=values[7],
            tt_probes=values[8],
            tt_hits=values[9],
            eval_calls=values[10],
            reply_cache_hits=values[11],
            anti_shuffle_hits=values[12],
            subtree_reuse_hits=values[13],
            book_hits=values[14],
            verification_nodes=values[15],
            singular_extensions=values[16],
            legal_root_count=values[17],
            team=values[18],
            iterations=iterations,
        )

    def _parse_hash(self, line: str) -> int:
        tokens = line.strip().split()
        if not tokens:
            raise RuntimeError("Empty response from Rust engine")
        if tokens[0] == "error":
            raise RuntimeError("Rust engine error: " + " ".join(tokens[1:]))
        if len(tokens) != 2 or tokens[0] != "hash":
            raise RuntimeError(f"Unexpected hash response: {line!r}")
        return int(tokens[1])

    def _encode_root_moves(self, root_moves_encoded: Iterable[int] | None) -> str:
        if not root_moves_encoded:
            return "-"
        parts = [str(int(encoded)) for encoded in root_moves_encoded]
        return ",".join(parts) if parts else "-"

    def search(
        self,
        state: ExternalState,
        budget_ns: int,
        root_moves_encoded: Iterable[int] | None = None,
        timeout_ns: int | None = None,
    ) -> RustSearchResult:
        budget_ns = max(1_000_000, min(int(budget_ns), self._max_budget_ns))
        if timeout_ns is not None:
            timeout_ns = max(1_000_000, int(timeout_ns))
        board_hex = board_to_hex(state.board)
        root_blob = self._encode_root_moves(root_moves_encoded)

        cmd = f"search {budget_ns} {int(state.player_to_move)} {int(state.turn)} {board_hex} {root_blob}"

        with self._lock:
            line = self._request_raw(cmd, timeout_ns=timeout_ns)
            return self._parse_result(line)

    def position_hash(self, state: ExternalState) -> int:
        board_hex = board_to_hex(state.board)
        cmd = f"hash {int(state.player_to_move)} {int(state.turn)} {board_hex}"
        with self._lock:
            line = self._request_raw(cmd)
            return self._parse_hash(line)


def format_telemetry(result: RustSearchResult) -> str:
    elapsed_s = max(1e-9, result.elapsed_ns / 1_000_000_000)
    nps = int(result.nodes / elapsed_s)
    qnps = int(result.qnodes / elapsed_s) if result.qnodes > 0 else 0
    tt_rate = (result.tt_hits / result.tt_probes) if result.tt_probes > 0 else 0.0

    return (
        f"depth={result.depth} score={result.score} nodes={result.nodes} qnodes={result.qnodes} "
        f"nps={nps} qnps={qnps} tt={tt_rate:.2%} eval={result.eval_calls} "
        f"reply={result.reply_cache_hits} anti_shuffle={result.anti_shuffle_hits} "
        f"subtree={result.subtree_reuse_hits} book={result.book_hits} "
        f"verify_nodes={result.verification_nodes} singular={result.singular_extensions} "
        f"time_ms={result.elapsed_ns / 1_000_000:.2f} legal_root={result.legal_root_count} team={result.team}"
    )
