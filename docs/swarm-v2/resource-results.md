# Worker resource results

Suite `worker-resources`, generation 30. Source: Worker resources run 38140098792 attempt 1 at 9122150d620de942b10046f8e9d9d976c4d0cb76.

## Profiles

| Profile | Memory limit MiB | CPU limit millis | Ephemeral MiB | Memory request MiB | CPU request millis | Runs |
|---|---|---|---|---|---|---|
| browser | 3072 | 2000 | 3072 | 2304 | 1500 | 9 |
| compiler | 1792 | 4500 | 3072 | 1280 | 1000 | 9 |
| general | 1792 | 4750 | 3072 | 1536 | 2250 | 9 |
| memory | 2816 | 500 | 3072 | 2304 | 250 | 9 |

## Measured distributions

| Profile | Metric | n | min | p50 | p95 | max |
|---|---|---|---|---|---|---|
| browser | peak_rss_mib | 9 | 660.73 | 839.3 | 1244.89 | 1244.89 |
| browser | peak_pss_mib | 9 | 397.16 | 485.54 | 703.87 | 703.87 |
| browser | peak_cores | 9 | 0.101 | 1.356 | 1.562 | 1.562 |
| browser | cpu_seconds | 9 | 1.12 | 1.75 | 3.11 | 3.11 |
| browser | peak_storage_mib | 9 | 0.38 | 0.38 | 0.38 | 0.38 |
| browser | peak_processes | 9 | 8 | 9 | 11 | 11 |
| compiler | peak_rss_mib | 9 | 72.04 | 121.64 | 225.61 | 225.61 |
| compiler | peak_pss_mib | 9 | 63.11 | 91.73 | 154.48 | 154.48 |
| compiler | peak_cores | 9 | 0.466 | 0.932 | 3.491 | 3.491 |
| compiler | cpu_seconds | 9 | 14.488 | 15.558 | 24.728 | 24.728 |
| compiler | peak_storage_mib | 9 | 2 | 2 | 2 | 2 |
| compiler | peak_processes | 9 | 3 | 5 | 9 | 9 |
| general | peak_rss_mib | 9 | 107.38 | 157.54 | 258.67 | 258.67 |
| general | peak_pss_mib | 9 | 81.01 | 117.72 | 191.71 | 191.71 |
| general | peak_cores | 9 | 1.015 | 2.033 | 3.76 | 3.76 |
| general | cpu_seconds | 9 | 11.499 | 15.01 | 27.644 | 27.644 |
| general | peak_storage_mib | 9 | 110.4 | 110.4 | 110.4 | 110.4 |
| general | peak_processes | 9 | 3 | 4 | 6 | 6 |
| memory | peak_rss_mib | 9 | 282.73 | 552.32 | 1091.21 | 1091.21 |
| memory | peak_pss_mib | 9 | 271.89 | 534.78 | 1060.14 | 1060.14 |
| memory | peak_cores | 9 | 0.054 | 0.116 | 0.321 | 0.321 |
| memory | cpu_seconds | 9 | 0.105 | 0.198 | 0.468 | 0.468 |
| memory | peak_storage_mib | 9 | 0 | 0 | 0 | 0 |
| memory | peak_processes | 9 | 2 | 3 | 5 | 5 |

## Refused

- idle: an idle sample cannot size a worker

## Coexistence

| Node | Free memory MiB | Free CPU millis | Workers that fit | All profiles at once |
|---|---|---|---|---|
| anton-ec2-0396dfd54e94477e5 | 15645 | 3984 | browser 2, compiler 3, general 1, memory 5 | no |
| anton-ec2-0447e2769b1a3084a | 5405 | 1484 | browser 0, compiler 1, general 0, memory 1 | no |
| anton-ec2-0af3a8fe7769581ef | 4893 | 1234 | browser 0, compiler 1, general 0, memory 1 | no |
| anton-ec2-0c9b3493a0ef846f4 | 5405 | 1484 | browser 0, compiler 1, general 0, memory 1 | no |
| k8s-master | 6208 | 3783 | browser 2, compiler 3, general 1, memory 2 | no |
| k8s-worker-1 | 6276 | 1362 | browser 0, compiler 1, general 0, memory 2 | no |
| k8s-worker-2 | 10252 | 1417 | browser 0, compiler 1, general 0, memory 3 | no |
| worker-001 | 3073 | 4412 | browser 1, compiler 1, general 1, memory 1 | no |

## Measurement context

- host cpus: 4
- host kernel: 6.17.0-1022-azure
- host load_average_1m: 0.39
- host load_average_1m_after: 1.83
- host memory_available_mib: 14325
- host memory_total_mib: 15989
- tool cc: cc (Ubuntu 13.3.0-6ubuntu2~24.04.1) 13.3.0
- tool kernel: 6.17.0-1022-azure
- tool playwright: Version 1.63.0
- tool pytest: pytest 9.1.1
- tool python: Python 3.12.15
- tool workload:browser: 1
- tool workload:compiler: 1
- tool workload:general: 1
- tool workload:idle: 1
- tool workload:memory: 1
- policy margin: 0.25
- policy min_repeats: 3
- policy agent_reserve_mib: 1024
- policy base_storage_mib: 2048
- policy memory_step_mib: 256
- policy cpu_step_millis: 250
- policy storage_step_mib: 1024
- policy tolerance: 0.15

## Limits of this measurement

- Measured on a GitHub hosted runner, not inside a worker Pod: CPU peaks are bounded by the runner's cores.
- The agent CLI's own resident memory is a declared reserve, not a measurement; it needs a live agent run.
- Coexistence counts whole workers in each node's allocatable capacity minus the requests already scheduled there.
