# cython_v3

Neuer Client in `cython_v3`, abgeleitet aus dem `rust_bot`-Suchkern.

## Aufbau

- `rust_core/`: Rust-Suchengine (board/search/eval/tt) als `cdylib`
- `cython_core/bridge.py`: Python/Cython-Bridge zur Rust-Library
- `client_cython.py`: Socha-Client (`IClientHandler`), der Board-States encodiert und Rust-Züge sendet

## Build

```bash
cd cython_v3
./build.sh
```

Optional getrennt:

```bash
cd cython_v3
./build_rust.sh
python setup.py build_ext --inplace
```

## Start

```bash
python client_cython.py -h localhost -p 13050
```

Das Zeitbudget ist fest aktiv und auf `1700 ms` pro Zug gesetzt.

## Wettkampfsystem / ZIP

Für den Upload gibt es ein `start.sh` (Interpreter: `/bin/sh`), das alle übergebenen Parameter unverändert weiterreicht.

Beispielaufrufe (funktionieren beide):

```bash
./start.sh -h gameserver -p 13050 -r 590e5e6f-cf93-488e-a12d-5c194ecf95c2
./start.sh --reservation 590e5e6f-cf93-488e-a12d-5c194ecf95c2 --host gameserver --port 13050
```

Submission-ZIP erstellen:

```bash
cd cython_v3
./prepare_submission.sh
```

Das erzeugte ZIP enthält **nur kompilierte Laufzeitdateien**:
- `client_cython.pyc`
- `cython_core/*.pyc`
- `librust_core.so`
- `start.sh`, `run_client.sh`

Es werden keine `.py`- oder Rust-Quelltexte ins ZIP aufgenommen.

Danach im Wettkampfsystem als Hauptdatei `start.sh` auswählen.
