# GA Long-Run Analysis

- Generated: 2026-02-24T08:54:06
- Runtime: 133:54:53
- Start: 2026-02-18T18:59:12
- End: 2026-02-24T08:54:06

## Configuration

- Chunk generations: 5
- Population: 16
- Elites: 4
- Games per opponent: 2
- Timeout per game: 180s
- Mutation sigma/decay/floor: 0.08/0.985/0.015
- Seed: 42
- Opponents file: /home/server/Software-Challenge-GA/log/ga_longrun/opponents.txt
- Active opponents (last chunk): /home/server/Software-Challenge-GA/cython_v1/client_cython.py, /home/server/Software-Challenge-GA/cython_v2/client_cython_baseline.py, /home/server/Software-Challenge-GA/bots/client_optimized.py

## Training Result

- Last checkpoint generation: 49
- Chunks completed: 10
- Best fitness: 0.9166666666666666
- Best weights: [18.304268191865734, 2.3380110168554924, 4.0585724000759456, 2.505749773055636, 0.4874924205286992]
- Best W/L/D/E: 5/0/1/0
- First recorded best fitness: 1.0
- Last recorded best fitness: 0.9166666666666666

## History (Top 10 by best_fitness)

| generation | best_fitness | mean_fitness | best_weights |
|---:|---:|---:|---|
| 0 | 1.0 | 0.65625 | [21.28340793167841, 3.0, 2.0, 4.9153375970488336, 0.5913016636208931] |
| 2 | 1.0 | 0.5390625 | [25.12008290997277, 2.366551164011428, 2.918080336607282, 3.725537067891856, 0.9665111998043376] |
| 3 | 1.0 | 0.671875 | [24.7638421887523, 4.512284543873173, 2.824639501377237, 3.4695580080848805, 0.5676307317925009] |
| 4 | 1.0 | 0.6796875 | [24.8752392988824, 3.1563170907361036, 3.795517889297569, 2.8010527999470454, 0.5998607722257409] |
| 5 | 1.0 | 0.7604166666666666 | [18.976514324532396, 1.448871764034514, 1.9986797294184275, 3.1634001948275348, 0.27259949575432296] |
| 7 | 1.0 | 0.7291666666666666 | [23.24975448693701, 4.371039047671587, 2.002061555334935, 2.8872418936347444, 0.5427655106702733] |
| 8 | 1.0 | 0.6614583333333334 | [17.363085802209255, 0.22543207827731826, 3.8005633329458934, 3.388877038365367, 0.6234648106915878] |
| 9 | 1.0 | 0.6302083333333334 | [22.90392559507746, 2.620605475542668, 2.7203476839191807, 4.056377051568007, 0.33811558648776224] |
| 10 | 1.0 | 0.6822916666666666 | [25.77962122774087, 0.2718477329588411, 2.851481386436188, 3.5695308254655567, 0.8263885774934843] |
| 11 | 1.0 | 0.71875 | [13.93157152488868, 2.296892879729426, 4.528372935240018, 3.043495802630667, 0.4900094655796015] |

## Final Validation

- Games per opponent: 20
- Total W/L/D/E: 35/24/1/0
- Total fitness: 0.5916666666666667
- Total win rate: 0.5833333333333334

| opponent | games | wins | losses | draws | errors | fitness |
|---|---:|---:|---:|---:|---:|---:|
| /home/server/Software-Challenge-GA/cython_v1/client_cython.py | 20 | 12 | 7 | 1 | 0 | 0.625 |
| /home/server/Software-Challenge-GA/cython_v2/client_cython_baseline.py | 20 | 13 | 7 | 0 | 0 | 0.65 |
| /home/server/Software-Challenge-GA/bots/client_optimized.py | 20 | 10 | 10 | 0 | 0 | 0.5 |
