# Unattended GA auf Server laufen lassen

Diese Anleitung beschreibt den kompletten Ablauf für deinen Server:
- Host: `10.0.30.137`
- User: `server`
- Ziel: `cython_v2`-Eval-Werte über Tage automatisch optimieren

## 1. Projekt auf den Server kopieren

Auf deinem lokalen Rechner:

```bash
cd /home/rasmus/Documents/Software-Challenge

rsync -avz --progress \
  -e "ssh -i ~/.ssh/<DEIN_KEY>" \
  --exclude '.git/' \
  --exclude '.venv/' \
  --exclude '__pycache__/' \
  --exclude '*.zip' \
  ./ server@10.0.30.137:/home/server/Software-Challenge/
```

Falls du schon einmal kopiert hast, reicht der gleiche `rsync`-Befehl erneut (inkrementell).

## 2. Auf dem Server vorbereiten

Einloggen:

```bash
ssh -i ~/.ssh/<DEIN_KEY> server@10.0.30.137
cd /home/server/Software-Challenge
```

Python-Umgebung + Dependencies:

```bash
python3 -m venv .venv
.venv/bin/pip install -U pip setuptools wheel
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install -r cython_v2/requirements.txt
```

Cython-Module bauen:

```bash
cd cython_v2
../.venv/bin/python setup.py build_ext --inplace
cd ..
```

## 3. Wichtige Dateien

- Runner: `run_unattended_ga.py`
- GA-Engine: `ga_optimize_v2.py`
- v2 Baseline Gegner: `cython_v2/client_cython_baseline.py`
- Live-Status (Text): `log/ga_longrun/progress_live.txt`
- Live-Status (JSON): `log/ga_longrun/progress_live.json`
- Endanalyse: `log/ga_longrun/final_analysis.md`
- Gegnerliste (dynamisch): `log/ga_longrun/opponents.txt`

## 4. Mehrtägigen Lauf starten

Beispiel für 5 Tage:

```bash
nohup .venv/bin/python run_unattended_ga.py \
  --hours 120 \
  --chunk-generations 5 \
  --population-size 16 \
  --elite-count 4 \
  --games-per-opponent 2 \
  --timeout-s 180 \
  --final-validation-games 20 \
  --checkpoint ga_v2_longrun_checkpoint.json \
  --log-dir log/ga_longrun \
  > log/ga_longrun_runner.out 2>&1 &
```

## 5. Progress live ansehen

```bash
tail -f log/ga_longrun/progress_live.txt
```

Optional:

```bash
tail -f log/ga_longrun_runner.out
ls -lt log/ga_longrun/chunk_*.log | head
```

## 6. Gegner verwalten (inkl. später dritter Bot)

Datei:

```bash
nano log/ga_longrun/opponents.txt
```

Standardinhalt:

```text
cython_v1/client_cython.py
cython_v2/client_cython_baseline.py
# /absolute/path/to/third_bot.py
```

Für den dritten Bot einfach neue Zeile hinzufügen:

```text
/home/server/Software-Challenge/bots/mein_dritter_bot.py
```

Wichtig:
- Pfad kann absolut oder relativ zum Repo sein.
- Änderungen werden automatisch pro neuem Chunk übernommen (kein Neustart nötig).

## 7. Stoppen / Fortsetzen

Sauber stoppen:

```bash
touch log/ga_longrun/STOP
```

Neu starten mit gleichem Checkpoint:

```bash
nohup .venv/bin/python run_unattended_ga.py \
  --hours 120 \
  --chunk-generations 5 \
  --population-size 16 \
  --elite-count 4 \
  --games-per-opponent 2 \
  --timeout-s 180 \
  --final-validation-games 20 \
  --checkpoint ga_v2_longrun_checkpoint.json \
  --log-dir log/ga_longrun \
  > log/ga_longrun_runner.out 2>&1 &
```

Vor Restart ggf. Stop-Datei entfernen:

```bash
rm -f log/ga_longrun/STOP
```

## 8. Was am Ende rauskommt

Am Laufende:
- `log/ga_longrun/final_analysis.md` mit:
  - Konfiguration
  - Laufzeit
  - beste gefundene Gewichte
  - Historie (Top-Generationen)
  - finale Validierung pro Gegner (W/L/D/E, Fitness, Winrate)

## 9. Realistische Erwartung

Der Runner optimiert auf maximale **empirische** Winrate gegen die Gegnerliste.  
Das ist die praktisch richtige Zielsetzung; eine mathematische Garantie auf globales Optimum gibt es bei GA nicht.

## 10. Troubleshooting

Lock-Fehler:

```text
Lock active: ... (another runner may be active)
```

Dann prüfen:

```bash
ps -ef | grep run_unattended_ga.py
```

Falls wirklich kein Lauf aktiv ist, Lockfile löschen:

```bash
rm -f ga_v2_longrun_checkpoint.json.lock
```

Wenn Builds fehlen:

```bash
cd cython_v2
../.venv/bin/python setup.py build_ext --inplace
cd ..
```
