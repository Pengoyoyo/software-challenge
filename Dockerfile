# syntax=docker/dockerfile:1.4
# ── Stage 1: Compile all Rust code ──────────────────────────────────────────
FROM rust:1-slim-bookworm AS rust-builder

WORKDIR /build

# rust_v1
COPY bots/rust/Cargo.toml bots/rust/Cargo.lock bots/rust/
COPY bots/rust/src/ bots/rust/src/
RUN --mount=type=cache,target=/usr/local/cargo/registry \
    --mount=type=cache,target=/usr/local/cargo/git \
    cargo build --release --manifest-path bots/rust/Cargo.toml

# rust_v2 (tuning target)
COPY bots/rust_v2/Cargo.toml bots/rust_v2/Cargo.lock bots/rust_v2/
COPY bots/rust_v2/src/ bots/rust_v2/src/
RUN --mount=type=cache,target=/usr/local/cargo/registry \
    --mount=type=cache,target=/usr/local/cargo/git \
    cargo build --release --manifest-path bots/rust_v2/Cargo.toml

# cython_v3 Rust engine (cdylib → librust_core.so)
COPY bots/cython_v3/rust_core/ bots/cython_v3/rust_core/
RUN --mount=type=cache,target=/usr/local/cargo/registry \
    --mount=type=cache,target=/usr/local/cargo/git \
    cargo build --release --manifest-path bots/cython_v3/rust_core/Cargo.toml


# ── Stage 2: Runtime image ───────────────────────────────────────────────────
FROM python:3.12-slim-bookworm

# Java JRE for server.jar  +  GCC/python3-dev for Cython compilation
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        openjdk-17-jre-headless \
        gcc \
        python3-dev \
    && rm -rf /var/lib/apt/lists/*

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

WORKDIR /app

# Python deps (own layer — rarely changes)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Project source
COPY server/        server/
COPY bots/          bots/
COPY opponent_bots/ opponent_bots/
COPY scripts/       scripts/
COPY benchmark.py   custom_bot_paths.json ./

# ── Place freshly compiled Rust binaries (overwrite any stale host artifacts)
COPY --from=rust-builder \
    /build/bots/rust/target/release/piranhas-bot \
    bots/rust/target/release/piranhas-bot

COPY --from=rust-builder \
    /build/bots/rust_v2/target/release/piranhas-bot-v2 \
    bots/rust_v2/target/release/piranhas-bot-v2

# librust_core.so — bridge.py searches this path first
COPY --from=rust-builder \
    /build/bots/cython_v3/rust_core/target/release/librust_core.so \
    bots/cython_v3/rust_core/target/release/librust_core.so

# ── Compile Cython extensions in-place
WORKDIR /app/bots/cython_v1
RUN python3 setup.py build_ext --inplace -q
WORKDIR /app/bots/cython_v2
RUN python3 setup.py build_ext --inplace -q
# bridge_cy.pyx is a thin re-export of bridge.py; failure is non-fatal
WORKDIR /app/bots/cython_v3
RUN python3 setup.py build_ext --inplace -q \
    || echo "[warn] cython_v3 Cython extension failed — Python fallback will be used"
WORKDIR /app

# ── Rewrite custom_bot_paths.json to use container-internal paths
RUN python3 - <<'PYEOF'
import json, pathlib, re
cfg = pathlib.Path("custom_bot_paths.json")
data = json.loads(cfg.read_text())
fixed = []
for p in data.get("paths", []):
    p2 = re.sub(r"^.*?Software-Challenge", "/app", p)
    if pathlib.Path(p2).exists():
        fixed.append(p2)
    else:
        print(f"  [skip] {p2} (not present in container)")
data["paths"] = fixed
cfg.write_text(json.dumps(data, indent=2))
print("custom_bot_paths.json →", fixed)
PYEOF

# Tuning logs and checkpoints — mount a host directory here
VOLUME ["/app/log"]

ENTRYPOINT ["python3", "scripts/tune_rust_v2.py"]
CMD ["--help"]
