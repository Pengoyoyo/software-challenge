# Eval-Weight-Tuning für `rust_v2` — Schritt-für-Schritt-Anleitung

Diese Anleitung erklärt vollständig, wie das Eval-Weight-Tuning des `rust_v2`-Bots
per Docker auf einem HPC-Server mit Intel Xeon (≤ 30 Kerne) durchgeführt wird.

---

## Inhaltsverzeichnis

1. [Konzept verstehen](#1-konzept-verstehen)
2. [Voraussetzungen](#2-voraussetzungen)
3. [Repository auf dem Server bereitstellen](#3-repository-auf-dem-server-bereitstellen)
4. [Docker-Image bauen](#4-docker-image-bauen)
5. [Smoke-Test: Bot-Discovery verifizieren](#5-smoke-test-bot-discovery-verifizieren)
6. [Tuning starten](#6-tuning-starten)
7. [Laufenden Container überwachen](#7-laufenden-container-überwachen)
8. [Unterbrochenen Lauf fortsetzen](#8-unterbrochenen-lauf-fortsetzen)
9. [Ergebnisse auswerten](#9-ergebnisse-auswerten)
10. [Beste Weights übernehmen](#10-beste-weights-übernehmen)
11. [Image neu bauen nach Code-Änderungen](#11-image-neu-bauen-nach-code-änderungen)
12. [Parameter-Referenz](#12-parameter-referenz)
13. [Fehlerbehebung](#13-fehlerbehebung)

---

## 1. Konzept verstehen

### Was wird optimiert?

Der `rust_v2`-Bot bewertet Spielpositionen mit einer gewichteten Summe aus
13 Heuristiken (z. B. „Größte Schwarmgruppe", „Anzahl Komponenten", …).
Die 13 Gewichte sind über die Umgebungsvariable `PIRANHAS_RSV2_EVAL_WEIGHTS`
konfigurierbar (13 kommagetrennte Zahlen).

### Wie funktioniert der Tuner?

Der Tuner (`scripts/tune_rust_v2.py`) läuft einen **Genetischen Algorithmus (GA)**:

```
Start
  │
  ▼
Population aus N Kandidaten-Gewichtssets erzeugen
  │
  ▼
┌─────────────────────────────────────────────────────┐
│  Generation-Loop (bis --generations erreicht)        │
│                                                     │
│  1. Jeder Kandidat spielt gegen alle Gegner          │
│     (beide Seiten, parallel mit --core-budget Kernen)│
│                                                     │
│  2. Fitness = Ø Win-Rate über alle Gegner            │
│                                                     │
│  3. Ranking: Beste oben                              │
│                                                     │
│  4. Nächste Generation:                              │
│     - Elites überleben unverändert                   │
│     - Immigrants (Zufallskandidaten) für Diversität  │
│     - Rest: Crossover + Gausssche Mutation           │
│                                                     │
│  5. Checkpoint speichern → Resume möglich            │
└─────────────────────────────────────────────────────┘
  │
  ▼
Final-Validation: Bestes Set nochmals gegen alle Gegner
  │
  ▼
best_env.txt + final_analysis.md
```

### Warum Docker?

- Alle Build-Abhängigkeiten (Rust, Java, GCC, Cython) sind isoliert
- Kein Aufräumen nach dem Tuning nötig
- Portabel: selbe Umgebung auf jedem Server
- Reproduzierbare Ergebnisse (gleiche Compiler-Versionen)

---

## 2. Voraussetzungen

### Auf dem HPC-Server müssen installiert sein:

| Komponente | Mindestversion | Prüfen |
|---|---|---|
| Docker Engine | 20.10 | `docker --version` |
| Docker Compose (Plugin) | 2.0 | `docker compose version` |
| Freier Arbeitsspeicher | ≥ 4 GB | `free -h` |
| Freier Festplattenplatz | ≥ 8 GB | `df -h .` |

> **Hinweis:** Docker BuildKit ist ab Docker 23.0 standardmäßig aktiv.
> Bei älteren Versionen: `export DOCKER_BUILDKIT=1` vor jedem `docker build`.

### Docker installieren (falls nicht vorhanden)

```bash
# Debian / Ubuntu
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
# Neu einloggen, damit Gruppenmitgliedschaft aktiv wird
```

### Prüfen, ob Docker ohne sudo läuft:

```bash
docker run --rm hello-world
# Erwartete Ausgabe: "Hello from Docker!"
```

---

## 3. Repository auf dem Server bereitstellen

### Option A: Git-Klon

```bash
git clone <REPO-URL> ~/Software-Challenge
cd ~/Software-Challenge
```

### Option B: Über rsync vom lokalen Rechner kopieren

```bash
# Auf dem lokalen Rechner ausführen:
rsync -av --exclude='.git/' --exclude='.venv/' --exclude='log/' \
      --exclude='bots/*/target/' --exclude='bots/*/build/' \
      /home/rasmus/OC/Personal/linux/Documents/Software-Challenge/ \
      user@hpc-server:~/Software-Challenge/
```

### Option C: ZIP-Archiv übertragen

```bash
# Lokal packen:
git archive --format=zip HEAD -o software-challenge.zip

# Auf Server übertragen und entpacken:
scp software-challenge.zip user@hpc-server:~/
ssh user@hpc-server "mkdir -p ~/Software-Challenge && unzip ~/software-challenge.zip -d ~/Software-Challenge"
```

### Verzeichnis wechseln

```bash
cd ~/Software-Challenge
ls
# Erwartete Ausgabe enthält u. a.:
# Dockerfile  docker-compose.yml  benchmark.py  bots/  server/  scripts/
```

---

## 4. Docker-Image bauen

### Was passiert beim Build?

Der Build läuft in **zwei Phasen**:

**Phase 1 — Rust-Builder** (`rust:1-slim`-Image):
- Kompiliert `bots/rust/` → Binary `piranhas-bot`
- Kompiliert `bots/rust_v2/` → Binary `piranhas-bot-v2` (Tuning-Ziel)
- Kompiliert `bots/cython_v3/rust_core/` → `librust_core.so` (Cython-v3-Engine)
- Cargo lädt die `socha`-Crate einmalig aus GitHub und cacht sie

**Phase 2 — Runtime** (`python:3.12-slim`-Image):
- Installiert OpenJDK 17 JRE (für `server.jar`) + GCC (für Cython)
- Installiert Python-Pakete (`socha`, `Cython`, `setuptools`)
- Kopiert Projektdateien in `/app/`
- Überschreibt Rust-Binaries mit den in Phase 1 gebauten (korrekte Architektur)
- Kompiliert Cython-Extensions für v1, v2, v3
- Passt `custom_bot_paths.json` auf Container-Pfade (`/app/...`) an

### Image bauen

```bash
cd ~/Software-Challenge

# BuildKit explizit aktivieren (falls Docker < 23.0)
export DOCKER_BUILDKIT=1

docker compose build
```

**Erwartete Ausgabe (gekürzt):**
```
[+] Building 142.3s (24/24) FINISHED
 => [rust-builder 1/8] FROM docker.io/library/rust:1-slim
 => [rust-builder 4/8] RUN cargo build --release --manifest-path bots/rust/Cargo.toml
 => [runtime 5/14] RUN apt-get update && apt-get install -y ...
 => [runtime 11/14] RUN python3 setup.py build_ext --inplace -q   (cython_v1)
 => [runtime 12/14] RUN python3 setup.py build_ext --inplace -q   (cython_v2)
 => [runtime 13/14] RUN python3 setup.py build_ext --inplace -q   (cython_v3)
 => [runtime 14/14] RUN python3 - <<'PYEOF' ...  (paths fix)
 => exporting to image
```

**Erster Build:** 5–15 Minuten (Rust-Crates werden heruntergeladen und kompiliert).
**Folgende Builds** (nach Quellcode-Änderungen): 30–90 Sekunden dank Layer-Cache.

### Build-Erfolg prüfen

```bash
docker images piranhas-tune-rust-v2
# Ausgabe:
# REPOSITORY                TAG       IMAGE ID       CREATED         SIZE
# piranhas-tune-rust-v2     latest    abc123def456   2 minutes ago   1.87GB
```

```bash
# Binaries im Image prüfen:
docker run --rm piranhas-tune-rust-v2 \
  bash -c "ls -lh /app/bots/rust/target/release/piranhas-bot \
                   /app/bots/rust_v2/target/release/piranhas-bot-v2 \
                   /app/bots/cython_v3/rust_core/target/release/librust_core.so"
# Erwartete Ausgabe: drei Dateien, jeweils mehrere MB
```

---

## 5. Smoke-Test: Bot-Discovery verifizieren

Bevor ein echter Tuning-Lauf gestartet wird, prüft der Dry-Run, welche Bots
der Container als Gegner erkennt — **ohne ein einziges Spiel zu spielen**.

```bash
docker compose run --rm dry-run
```

**Erwartete Ausgabe:**
```
[setup] root: /app
[setup] target: /app/bots/rust_v2/pur_rust_client.py
[setup] weight names: w_largest, w_components, w_spread, ...
[setup] initial weights: 380,260,50,130,15,4,7,180,130,90,20,12,85000
[setup] parallel_games=15 (cpu_cores=64, cores_per_game=2.0, ..., core_budget=30)

[discovery] target /app/bots/rust_v2/pur_rust_client.py
[discovery] discovered bots: 8
[discovery] opponents (7):
  - /app/bots/cython_v1/client_cython.py
  - /app/bots/cython_v2/client_cython.py
  - /app/bots/cython_v3/client_cython.py
  - /app/bots/python/client.py
  - /app/bots/python/client_v2.py
  - /app/bots/rust/pur_rust_client.py
  - /app/opponent_bots/CrackedLine@v1/run.py
```

**Worauf achten:**
- `parallel_games=15` bei `--core-budget 30` und `--cores-per-game 2.0` ✓
- `bots/rust/pur_rust_client.py` (rust_v1) muss als Gegner erscheinen ✓
- Das Tuning-Ziel (`rust_v2`) darf **nicht** unter Gegnern stehen ✓
- Keine Fehlermeldungen bezüglich fehlender Binaries ✓

---

## 6. Tuning starten

### 6.1 Kurzform mit Docker Compose

```bash
docker compose run --rm tune
```

Dies startet den Tuner mit den in `docker-compose.yml` vordefinierten Parametern:
`--core-budget 30 --games-per-opponent 4 --population-size 20 --generations 50`

### 6.2 Eigene Parameter übergeben

```bash
docker run --rm \
  -v "$(pwd)/log:/app/log" \
  piranhas-tune-rust-v2 \
  --core-budget 30 \
  --games-per-opponent 4 \
  --population-size 20 \
  --generations 50 \
  --resample-top 3 \
  --resample-rounds 1
```

### 6.3 Im Hintergrund laufen lassen

Für eine SSH-Session die abgebrochen werden kann:

```bash
# Detached starten, Container-Name festlegen:
docker run -d \
  --name rust_v2_tuning \
  -v "$(pwd)/log:/app/log" \
  piranhas-tune-rust-v2 \
  --core-budget 30 \
  --games-per-opponent 4 \
  --population-size 20 \
  --generations 50 \
  --resample-top 3 \
  --resample-rounds 1

# Logs verfolgen (aus anderer Session):
docker logs -f rust_v2_tuning
```

### 6.4 Was passiert nach dem Start?

**Schritt 1 — Setup (wenige Sekunden):**
```
[setup] root: /app
[setup] target: /app/bots/rust_v2/pur_rust_client.py
[setup] parallel_games=15 (cpu_cores=64, core_budget=30)
```

**Schritt 2 — Preflight (∼2–5 Minuten bei 7 Gegnern, 2 Spiele je Gegner):**
Jeder potenzielle Gegner spielt 2 Testspiele. Gegner, die abstürzen oder
hängen bleiben, werden automatisch gefiltert.
```
[preflight] opponents to test: 7
[preflight]  1/ 7 OK   bots/rust/pur_rust_client.py           W/L/D/E=1/1/0/0
[preflight]  2/ 7 OK   bots/cython_v2/client_cython.py        W/L/D/E=2/0/0/0
[preflight]  3/ 7 SKIP bots/cython_v3/client_cython.py        W/L/D/E=0/0/0/2
[preflight]  4/ 7 OK   bots/cython_v1/client_cython.py        W/L/D/E=0/2/0/0
...
[setup] active opponents: 6
```

> Ein Gegner mit `W/L/D/E=0/0/0/2` (2 Errors, 0 gültige Spiele) wird
> übersprungen — in diesem Beispiel hat cython_v3 die Rust-Engine nicht gefunden.
> Das ist normal und wird automatisch behandelt.

**Schritt 3 — Generations-Loop:**
```
=== Generation 0 ===
Sigma=0.0800
[eval] pending=20 jobs=480 parallel_games=15 games_per_candidate=24
  [ 1/20] fit=+0.6042 worst=+0.3333 W/L/D/E=29/17/2/0 weights=380,260,...
  [ 2/20] fit=+0.5417 worst=+0.1667 W/L/D/E=26/20/2/0 weights=415,241,...
  ...
  [20/20] fit=+0.4792 worst=+0.0000 W/L/D/E=23/23/2/0 weights=342,289,...
Best gen 0: fit=+0.6042 worst=+0.3333 W/L/D/E=29/17/2/0 weights=380,260,...

=== Generation 1 ===
...
```

**Was bedeuten die Werte?**

| Wert | Bedeutung |
|---|---|
| `fit=+0.6042` | Fitness: Ø Win-Rate über alle Gegner (0 = 0%, 1 = 100%) |
| `worst=+0.3333` | Win-Rate gegen den schwersten Gegner (Robustheitsmerkmal) |
| `W/L/D/E=29/17/2/0` | Wins / Losses / Draws / Errors über alle Spiele |
| `Sigma=0.0800` | Aktuelle Mutationsbreite (schrumpft über Zeit) |

---

## 7. Laufenden Container überwachen

### 7.1 Live-Logs eines Hintergrund-Containers

```bash
docker logs -f rust_v2_tuning
# Abbrechen mit Ctrl+C (Container läuft weiter)
```

### 7.2 Live-Progress-Datei lesen

Der Tuner schreibt nach jeder Generation eine JSON-Datei mit dem aktuellen Besten:

```bash
# Pfad des neuesten Log-Verzeichnisses finden:
ls -t log/tune_rust_v2/ | head -1
# Beispiel: 20260301_140022

# Live-Datei beobachten:
watch -n 10 cat log/tune_rust_v2/20260301_140022/progress_live.json
```

**Beispiel-Inhalt:**
```json
{
  "time": "2026-03-01T15:42:11",
  "state": "running",
  "generation": 12,
  "sigma": 0.0534,
  "next_game_id": 5760,
  "best": {
    "fitness": 0.7083,
    "worst_fitness": 0.5000,
    "wins": 34,
    "losses": 14,
    "draws": 0,
    "errors": 0,
    "games": 48,
    "weights": [412, 289, 38, 145, 18, 5, 8, 195, 141, 102, 22, 14, 91000],
    "named": {
      "w_largest": 412,
      "w_components": 289,
      "w_spread": 38,
      "w_material": 145,
      "w_links": 18,
      "w_center": 5,
      "w_mobility": 8,
      "w_late_largest": 195,
      "w_late_components": 141,
      "w_late_spread": 102,
      "w_late_links": 22,
      "w_late_mobility": 14,
      "connect_bonus": 91000
    }
  }
}
```

### 7.3 Ressourcenverbrauch prüfen

```bash
docker stats rust_v2_tuning
# Zeigt CPU%, Speicher, Netzwerk in Echtzeit
```

**Erwartete Werte bei 15 parallelen Spielen:**
- CPU: 1400–2800 % (14–28 Kerne ausgelastet)
- RAM: 3–6 GB (Java-Server × 15 + Bots)

### 7.4 Anzahl laufender Java-Prozesse prüfen

```bash
docker exec rust_v2_tuning sh -c "ps aux | grep java | grep -v grep | wc -l"
# Sollte ≤ 15 sein (parallel_games)
```

---

## 8. Unterbrochenen Lauf fortsetzen

Der Tuner speichert nach **jeder Generation** einen Checkpoint. Bei Unterbrechung
(Server-Neustart, SSH-Timeout, manuelles Stoppen) geht nichts verloren.

### 8.1 Checkpoint-Pfad ermitteln

```bash
ls -t log/tune_rust_v2/
# Ausgabe: Verzeichnisse nach Zeitstempel sortiert
# 20260301_140022  20260228_203148  ...

LOG_DIR=log/tune_rust_v2/20260301_140022
ls $LOG_DIR
# checkpoint.json   progress_live.json   games/
```

### 8.2 Fortsetzen

```bash
# Mit Docker Compose (CHECKPOINT-Env-Variable):
CHECKPOINT=log/tune_rust_v2/20260301_140022/checkpoint.json \
  docker compose run --rm resume

# Oder direkt:
docker run --rm \
  -v "$(pwd)/log:/app/log" \
  piranhas-tune-rust-v2 \
  --resume \
  --checkpoint log/tune_rust_v2/20260301_140022/checkpoint.json \
  --core-budget 30 \
  --generations 50
```

**Erwartete Ausgabe:**
```
[resume] continuing from generation 13
[setup] active opponents: 6
  - bots/rust/pur_rust_client.py
  ...

=== Generation 14 ===
Sigma=0.0497
```

> Der Tuner liest Gegner-Liste und Population aus dem Checkpoint — die Parameter
> `--opponent`, `--population-size` etc. aus dem Checkpoint werden verwendet.
> `--core-budget` und `--parallel-games` können beim Resume geändert werden.

### 8.3 Mehr Generationen als ursprünglich geplant

```bash
docker run --rm \
  -v "$(pwd)/log:/app/log" \
  piranhas-tune-rust-v2 \
  --resume \
  --checkpoint log/tune_rust_v2/20260301_140022/checkpoint.json \
  --generations 100   # war 50, jetzt weiter bis 100
  --core-budget 30
```

---

## 9. Ergebnisse auswerten

### 9.1 Verzeichnisstruktur nach dem Lauf

```
log/tune_rust_v2/20260301_140022/
├── checkpoint.json      # Letzter GA-State (für Resume)
├── progress_live.json   # Letzter Live-Status
├── final_analysis.md    # Vollständiger Bericht
├── best_env.txt         # Env-Variable mit besten Weights
└── games/               # Spiel-Logs (nur bei --keep-game-logs)
```

### 9.2 `best_env.txt` — das wichtigste Ergebnis

```bash
cat log/tune_rust_v2/20260301_140022/best_env.txt
```

**Ausgabe:**
```
PIRANHAS_RSV2_EVAL_WEIGHTS=412,289,38,145,18,5,8,195,141,102,22,14,91000
```

Diese Zeile ist alles, was du brauchst, um den Bot mit den optimierten Weights zu starten.

### 9.3 `final_analysis.md` — vollständiger Bericht

```bash
cat log/tune_rust_v2/20260301_140022/final_analysis.md
```

**Relevante Abschnitte:**

```markdown
## Best Result

- Best fitness: 0.7292
- Best worst-opponent fitness: 0.5833
- Best weights (named):
  - w_largest: 412
  - w_components: 289
  ...
  - connect_bonus: 91000
- Env line: PIRANHAS_RSV2_EVAL_WEIGHTS=412,289,38,145,18,5,8,195,141,102,22,14,91000
- Aggregate W/L/D/E: 35/13/0/0
- Aggregate games: 48

## History

| generation | sigma  | best_fitness | best_worst_fitness | mean_fitness |
|---:|---:|---:|---:|---:|
| 0  | 0.0800 | 0.6042 | 0.3333 | 0.5187 |
| 1  | 0.0788 | 0.6250 | 0.4167 | 0.5364 |
...
| 49 | 0.0153 | 0.7292 | 0.5833 | 0.6891 |
```

**Was bedeutet eine gute Fitness?**

| Fitness | Bedeutung |
|---|---|
| < 0.50 | Schlechter als Default → Weights verschlechtert |
| 0.50 | Gleichstand (wie Default) |
| 0.55–0.65 | Leichte Verbesserung |
| 0.65–0.75 | Gute Verbesserung (∼+50–100 Elo) |
| > 0.75 | Starke Verbesserung (nur gegen schwächere Gegner realistisch) |

**`worst_fitness`** ist wichtiger als `fitness`: Ein Bot der gegen
jeden Gegner mit 0.55 gewinnt, ist robuster als einer mit 0.80 gegen schwache
aber 0.20 gegen starke Gegner.

### 9.4 Konvergenzverlauf plotten (optional)

```bash
# History aus final_analysis.md extrahieren und plotten:
python3 - <<'EOF'
import json, pathlib, re

md = pathlib.Path("log/tune_rust_v2/20260301_140022/final_analysis.md").read_text()
# Checkpoint enthält strukturierte History-Daten:
cp = json.loads(pathlib.Path("log/tune_rust_v2/20260301_140022/checkpoint.json").read_text())
history = cp["history"]

print(f"{'Gen':>4}  {'Sigma':>7}  {'Best':>7}  {'Worst':>7}  {'Mean':>7}")
for h in history:
    print(f"{h['generation']:>4}  {h['sigma']:>7.4f}  {h['best_fitness']:>7.4f}  "
          f"{h['best_worst_fitness']:>7.4f}  {h['mean_fitness']:>7.4f}")
EOF
```

---

## 10. Beste Weights übernehmen

### 10.1 Bot sofort testen (ohne Code-Änderung)

```bash
# Env-Variable aus best_env.txt laden und Bot-Benchmark starten:
source <(cat log/tune_rust_v2/20260301_140022/best_env.txt)
echo $PIRANHAS_RSV2_EVAL_WEIGHTS
# 412,289,38,145,18,5,8,195,141,102,22,14,91000

# Bot gegen sich selbst (Default-Weights vs. neue Weights) testen:
python3 benchmark.py \
  --rounds 20 \
  bots/rust_v2/pur_rust_client.py \
  bots/rust_v2/pur_rust_client.py
```

> Beim zweiten Bot wird `PIRANHAS_RSV2_EVAL_WEIGHTS` aus der Umgebung geerbt.
> Beim ersten Bot nicht (da `benchmark.py` Env-Variablen nur selektiv injiziert).
> Besser: direkt mit dem Tuner gegen rust_v1 vergleichen.

### 10.2 Verifikations-Benchmark

Vor dem Festschreiben: 50-Spiel-Match neuer Weights gegen rust_v1:

```bash
PIRANHAS_RSV2_EVAL_WEIGHTS="412,289,38,145,18,5,8,195,141,102,22,14,91000" \
python3 benchmark.py \
  --rounds 50 \
  bots/rust_v2/pur_rust_client.py \
  bots/rust/pur_rust_client.py
```

**Erwartetes Ergebnis:** ≥ 60% Win-Rate (≥ 30 von 50 Siegen) bei erfolgreicher Optimierung.

### 10.3 Weights in `evaluate.rs` festschreiben

Wenn die Verifikation überzeugt, die `DEFAULT_WEIGHTS` in der Quelldatei aktualisieren:

**Datei:** `bots/rust_v2/src/evaluate.rs`, Zeilen 29–44

```rust
// Vorher:
const DEFAULT_WEIGHTS: EvalWeights = EvalWeights {
    w_largest: 380,
    w_components: 260,
    w_spread: 50,
    w_material: 130,
    w_links: 15,
    w_center: 4,
    w_mobility: 7,
    w_late_largest: 180,
    w_late_components: 130,
    w_late_spread: 90,
    w_late_links: 20,
    w_late_mobility: 12,
    connect_bonus: 85_000,
};

// Nachher (mit Tuning-Ergebnis):
const DEFAULT_WEIGHTS: EvalWeights = EvalWeights {
    w_largest: 412,
    w_components: 289,
    w_spread: 38,
    w_material: 145,
    w_links: 18,
    w_center: 5,
    w_mobility: 8,
    w_late_largest: 195,
    w_late_components: 141,
    w_late_spread: 102,
    w_late_links: 22,
    w_late_mobility: 14,
    connect_bonus: 91_000,
};
```

### 10.4 Bot neu kompilieren und testen

```bash
cd bots/rust_v2
cargo build --release
cd ../..

# Schnell-Check: Bot antwortet auf Server
python3 bots/rust_v2/pur_rust_client.py --help
```

### 10.5 Docker-Image für nächstes Tuning neu bauen

Nach Änderung von `evaluate.rs` muss das Image neu gebaut werden:

```bash
docker compose build
```

---

## 11. Image neu bauen nach Code-Änderungen

### 11.1 Nach Änderungen an `bots/rust_v2/src/`

```bash
docker compose build
# Nur Stage 1 (Rust) wird neu gebaut — ∼2–5 Minuten
```

Docker baut dank Layer-Caching nur die geänderten Schichten neu:
- Neue `.rs`-Datei → rust_v2-Build-Layer wird invalidiert
- rust_v1 und cython_v3 bleiben gecacht (andere COPY-Layer)

### 11.2 Nach Änderungen an `scripts/tune_rust_v2.py`

```bash
docker compose build
# Nur ein kleiner Layer am Ende wird neu gebaut — ∼10 Sekunden
```

### 11.3 Erzwungenes Neu-Bauen ohne Cache

```bash
docker compose build --no-cache
# Alles wird neu gebaut — 10–20 Minuten
# Nur nötig wenn Basis-Images aktualisiert werden sollen
```

### 11.4 Altes Image entfernen

```bash
docker rmi piranhas-tune-rust-v2:latest
docker compose build
```

---

## 12. Parameter-Referenz

### Vollständige Parameterliste

```bash
docker run --rm piranhas-tune-rust-v2 --help
```

### Wichtigste Parameter im Detail

#### `--core-budget 30`
**Wichtigster Parameter für HPC-Server.**
Berechnet automatisch: `parallel_games = floor(30 / cores_per_game) = floor(30 / 2.0) = 15`.
Überschreibt `--max-parallel-games`. Wird im Log angezeigt.

#### `--games-per-opponent N` (Default: 2)
Anzahl Spiele pro Kandidat pro Gegner (beide Seiten = N/2 als Spieler 1, N/2 als Spieler 2).
- N=2: Schnell, hohes Rauschen (viel Glück im Ergebnis)
- N=4: Empfohlen für HPC (guter Kompromiss)
- N=6: Niedrigstes Rauschen, langsam

**Gesamtspiele pro Generation:** `population_size × opponents × games_per_opponent`
Beispiel: `20 × 6 × 4 = 480 Spiele` → bei 15 parallelen Spielen ∼ 480/15 × 90s ≈ 48 Minuten pro Generation.

#### `--population-size N` (Default: 16)
Anzahl Kandidaten pro Generation. Mehr = breitere Suche, aber länger pro Generation.
Empfehlung: 16–24 für 50+ Generationen.

#### `--generations N` (Default: 40)
Anzahl GA-Generationen. Der GA konvergiert typischerweise nach 30–60 Generationen.
Empfehlung: 50 für einen guten Lauf, 100 für maximale Qualität.

#### `--elite-count N` (Default: 4)
Wie viele der besten Kandidaten unverändert in die nächste Generation überleben.
Empfehlung: 3–5 (zu viele = zu wenig Diversität).

#### `--immigrants N` (Default: 2)
Zufällig erzeugte Kandidaten pro Generation (Diversität, verhindert vorzeitige Konvergenz).
Empfehlung: 1–3.

#### `--mutation-sigma F` (Default: 0.08)
Standardabweichung der Gauss-Mutation, relativ zur Wertebreite des jeweiligen Gewichts.
- 0.08 = 8% der Spannweite pro Mutation
- Schrumpft jede Generation: `sigma = max(floor, sigma × decay)`

#### `--mutation-decay F` (Default: 0.985)
Wie schnell `sigma` schrumpft (0.985 = −1.5% pro Generation).
Nach 50 Generationen: `0.08 × 0.985^50 ≈ 0.037`.

#### `--mutation-floor F` (Default: 0.015)
Mindestwert für `sigma` — verhindert dass Mutation ganz aufhört.

#### `--resample-top N --resample-rounds R`
Reduziert Rauschen für die besten N Kandidaten: Spielt R weitere Runden gegen alle Gegner
und mittelt das Ergebnis. Empfohlen: `--resample-top 3 --resample-rounds 1`.

#### `--skip-preflight`
Überspringt die Preflight-Phase (wenn bekannt ist dass alle Bots funktionieren).

#### `--opponent PATH`
Fügt explizit einen Gegner hinzu (kann mehrfach angegeben werden).
Nützlich um nur gegen bestimmte Bots zu tunen:
```bash
docker run --rm -v "$(pwd)/log:/app/log" piranhas-tune-rust-v2 \
  --core-budget 30 \
  --opponent bots/rust/pur_rust_client.py \
  --skip-preflight \
  --games-per-opponent 6
```

#### `--exclude REGEX`
Schließt Gegner aus, deren Pfad/Name dem Regex-Muster entspricht.
Default-Excludes: `/bots/cpp/`, `/bots/my_player/`, `/submissions/`

#### `--final-validation-games N` (Default: 10)
Anzahl Spiele für die abschließende Validierung des besten Kandidaten.
Empfehlung: 10–20 für zuverlässige finale Einschätzung.

#### `--seed N` (Default: 42)
Zufalls-Seed für Reproduzierbarkeit. Verschiedene Seeds für unabhängige Läufe.

#### `--cores-per-game F` (Default: 2.0)
Geschätzte CPU-Kerne pro laufendem Spiel (Java-Server + 2 Bots).
Bei reinen Python-Gegnern: 1.5; bei Rust-vs-Rust: 2.5.

---

### Empfohlene Konfigurationen

#### Schnell (Ergebnisse in ∼2h):
```bash
--core-budget 30 --games-per-opponent 2 --population-size 12 --generations 30
```

#### Standard (Ergebnisse in ∼6–8h):
```bash
--core-budget 30 --games-per-opponent 4 --population-size 20 --generations 50 \
--resample-top 3 --resample-rounds 1
```

#### Intensiv (Ergebnisse in ∼18–24h):
```bash
--core-budget 30 --games-per-opponent 6 --population-size 24 --generations 100 \
--resample-top 5 --resample-rounds 2 --final-validation-games 20
```

#### Fokussiert: Nur gegen rust_v1 tunen:
```bash
--core-budget 30 \
--opponent bots/rust/pur_rust_client.py \
--skip-preflight \
--games-per-opponent 8 \
--population-size 16 \
--generations 60
```

---

## 13. Fehlerbehebung

### Problem: `piranhas-bot-v2: not found` beim Container-Start

**Ursache:** Das Rust-Binary wurde nicht korrekt in Stage 1 kompiliert oder
der COPY-Schritt hat versagt.

**Diagnose:**
```bash
docker run --rm piranhas-tune-rust-v2 \
  bash -c "ls -la /app/bots/rust_v2/target/release/"
```

**Lösung:**
```bash
docker compose build --no-cache
```

---

### Problem: `Error: no opponents selected`

**Ursache:** Preflight hat alle Gegner gefiltert (alle waren fehlerhaft).

**Diagnose:**
```bash
docker compose run --rm dry-run
# Prüfen ob Gegner erkannt werden
```

```bash
# Preflight-Ausgabe mit mehr Detail:
docker run --rm -v "$(pwd)/log:/app/log" piranhas-tune-rust-v2 \
  --core-budget 30 --preflight-games 1 --keep-game-logs \
  --generations 0 2>&1 | head -50
# Dann Logs prüfen:
ls log/tune_rust_v2/*/games/
```

**Lösung:** Sicherstellen dass der Java-Server läuft (`server/server.jar` vorhanden):
```bash
docker run --rm piranhas-tune-rust-v2 bash -c "ls -lh /app/server/server.jar"
```

---

### Problem: Alle Spiele enden mit `UNKNOWN` oder `ERROR`

**Ursache:** Java nicht gefunden oder falscher Port.

**Diagnose:**
```bash
docker run --rm piranhas-tune-rust-v2 bash -c "java -version"
# Erwartete Ausgabe: openjdk version "17.x.x"
```

**Lösung:**
```bash
# Prüfen ob OpenJDK korrekt installiert:
docker run --rm piranhas-tune-rust-v2 \
  bash -c "which java && java -version 2>&1"
```

Falls Java fehlt: `docker compose build --no-cache`

---

### Problem: `Port already in use` / Port-Konflikte

**Ursache:** Zu viele parallele Spiele oder andere Prozesse belegen Ports.

**Lösung:**
```bash
# Anderen Base-Port wählen:
docker run --rm -v "$(pwd)/log:/app/log" piranhas-tune-rust-v2 \
  --core-budget 30 --base-port 17000

# Oder Parallelität reduzieren:
docker run --rm -v "$(pwd)/log:/app/log" piranhas-tune-rust-v2 \
  --parallel-games 8 --base-port 18000
```

---

### Problem: Container stirbt mit OOM (Out of Memory)

**Ursache:** Zu viele parallele Java-Prozesse.

**Diagnose:**
```bash
dmesg | grep -i "oom killer" | tail -5
```

**Lösung:**
```bash
# Weniger parallele Spiele oder cores-per-game erhöhen:
docker run --rm -v "$(pwd)/log:/app/log" piranhas-tune-rust-v2 \
  --core-budget 24 --cores-per-game 3.0
# → floor(24/3.0) = 8 parallele Spiele statt 15
```

---

### Problem: `cython_v3` wird nie als OK im Preflight gewertet

**Ursache:** `librust_core.so` konnte nicht geladen werden (Architektur, fehlende deps).

**Diagnose:**
```bash
docker run --rm piranhas-tune-rust-v2 \
  bash -c "python3 -c 'from cython_core.bridge import RustEngineBridge; b = RustEngineBridge()'"
```

**Workaround:** cython_v3 aus Gegnern ausschließen:
```bash
docker run --rm -v "$(pwd)/log:/app/log" piranhas-tune-rust-v2 \
  --exclude "/bots/cython_v3/" \
  --core-budget 30 --generations 50
```

---

### Problem: `custom_bot_paths.json` enthält falsche Pfade

**Diagnose:**
```bash
docker run --rm piranhas-tune-rust-v2 cat /app/custom_bot_paths.json
# Alle Pfade sollten mit /app/ beginnen
```

**Manuelle Korrektur im Container:**
```bash
docker run --rm piranhas-tune-rust-v2 \
  python3 -c "import json; d=json.load(open('/app/custom_bot_paths.json')); print(d)"
```

---

### Problem: Build schlägt mit `cargo: command not found` fehl

**Ursache:** BuildKit ist nicht aktiv, Dockerfile wird mit Legacy-Builder gebaut.

**Lösung:**
```bash
export DOCKER_BUILDKIT=1
docker compose build
# Oder:
docker buildx build -t piranhas-tune-rust-v2 .
```

---

### Problem: Build schlägt bei `socha`-Crate fehl (Git-Fetch-Fehler)

**Ursache:** Server hat keinen Internet-Zugang für GitHub.

**Lösung A:** Crate vorher lokal cachen und per `vendor/` ins Image kopieren:
```bash
# Lokal (mit Internet):
cd bots/rust_v2 && cargo vendor > .cargo/config.toml
# Dann im Dockerfile: COPY bots/rust_v2/.cargo/ und cargo build --frozen
```

**Lösung B:** Auf einem anderen Rechner bauen, Image per `docker save/load` übertragen:
```bash
# Auf Build-Rechner mit Internet:
docker compose build
docker save piranhas-tune-rust-v2:latest | gzip > piranhas-tune-rust-v2.tar.gz

# Auf HPC-Server (ohne Internet):
docker load < piranhas-tune-rust-v2.tar.gz
docker compose run --rm tune
```

---

### Nützliche Debug-Befehle

```bash
# Shell im Container öffnen:
docker run --rm -it piranhas-tune-rust-v2 bash

# Alle Bots im Container testen:
docker run --rm piranhas-tune-rust-v2 python3 benchmark.py --help

# Laufende Container anzeigen:
docker ps

# Container stoppen (sauber):
docker stop rust_v2_tuning
# Container-Logs werden durch das Volume-Mount in ./log gesichert

# Vollständige Bereinigung (Image + Container):
docker compose down --rmi all
```
