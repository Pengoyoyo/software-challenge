# Piranhas NNUE

Alpha-Beta search engine with an NNUE (Efficiently Updatable Neural Network) evaluation function for Piranhas 2026.

## Architecture

```
Input: 800 features (100 squares × 8 piece types, player-relative)
L1:    800 → 256, ClippedReLU [0, 1]
L2:    256 → 32,  ClippedReLU [0, 1]
Out:   32  → 1,   linear (centipawn-like score from mover's POV)
```

## Pipeline

```
1. datagen  →  data/*.bin       (Rust: cargo run --release --bin datagen)
2. train    →  model.pt         (Python: python training/train.py)
3. export   →  weights.bin      (Python: python training/export.py)
4. integrate → src/evaluate.rs  (Rust: replace hand-crafted eval with NNUE forward pass)
```

## Data Format (107 bytes / sample)

| Field  | Size    | Description                          |
|--------|---------|--------------------------------------|
| board  | 100 × u8| piece at each square (0=Empty, 1-3=ONE sizes, 4-6=TWO sizes, 7=SQUID) |
| player | 1 × u8  | side to move (1=ONE, 2=TWO)          |
| turn   | 2 × u8  | turn number, little-endian u16        |
| score  | 4 × u8  | AB score (mover's POV), LE i32        |

## Feature Encoding (player-relative)

For player=ONE: own pieces → indices 1-3, opponent → 4-6, SQUID → 7, Empty → 0  
For player=TWO: own pieces (4-6) → indices 1-3, opponent (1-3) → 4-6, SQUID → 7, Empty → 0

Each square contributes 8 one-hot features → 100 × 8 = 800 input neurons.

## Weights Binary Format

```
l1_w: [f32; 800 * 256]   little-endian
l1_b: [f32; 256]
l2_w: [f32; 256 * 32]
l2_b: [f32; 32]
out_w: [f32; 32]
out_b: [f32; 1]
```

## Usage

```bash
# 1. Generate training data (rust_v3 directory)
cargo run --release --bin datagen -- 5000 200 ../nnue/data/train.bin

# 2. Train
cd nnue
pip install -r training/requirements.txt
python training/train.py --data data/train.bin --epochs 20 --out model.pt

# 3. Export weights
python training/export.py --model model.pt --out data/weights.bin

# 4. Copy weights into rust_v3, integrate nnue.rs into evaluate.rs
```
