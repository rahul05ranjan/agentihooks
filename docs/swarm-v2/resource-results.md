# Worker resource results

Suite `worker-resources`, generation 70. Source: Worker resources run 38147758574 attempt 1 at 50eca72cb031bc0669440c8f48fd8dff63194e68.

## Profiles

| Profile | Memory limit MiB | CPU limit millis | Ephemeral MiB | Memory request MiB | CPU request millis | Runs |
|---|---|---|---|---|---|---|
| browser | 4864 | 4250 | 3072 | 3840 | 2250 | 9 |
| compiler | 1792 | 4750 | 3072 | 1280 | 2000 | 9 |
| general | 1792 | 4750 | 3072 | 1536 | 2250 | 9 |
| memory | 2816 | 500 | 3072 | 2304 | 250 | 9 |

## Measured distributions

| Profile | Metric | n | min | p50 | p95 | max |
|---|---|---|---|---|---|---|
| browser | peak_rss_mib | 9 | 694.51 | 1363.26 | 2675.87 | 2675.87 |
| browser | peak_pss_mib | 9 | 419.26 | 666.82 | 1519.05 | 1519.05 |
| browser | peak_parent_rss_mib | 9 | 28.99 | 29.1 | 29.75 | 29.75 |
| browser | peak_children_rss_mib | 9 | 664.77 | 1334.16 | 2646.77 | 2646.77 |
| browser | peak_cores | 9 | 1.106 | 2.044 | 3.317 | 3.317 |
| browser | cpu_seconds | 9 | 1.28 | 2.79 | 6.74 | 6.74 |
| browser | peak_storage_mib | 9 | 0.38 | 0.45 | 0.6 | 0.6 |
| browser | peak_processes | 9 | 9 | 17 | 33 | 33 |
| compiler | peak_rss_mib | 9 | 72.42 | 121.51 | 236.69 | 236.69 |
| compiler | peak_pss_mib | 9 | 63.45 | 92.48 | 166.96 | 166.96 |
| compiler | peak_parent_rss_mib | 9 | 14.7 | 14.79 | 15.32 | 15.32 |
| compiler | peak_children_rss_mib | 9 | 57.75 | 106.8 | 221.83 | 221.83 |
| compiler | peak_cores | 9 | 0.987 | 1.915 | 3.682 | 3.682 |
| compiler | cpu_seconds | 9 | 14.286 | 15.076 | 24.522 | 24.522 |
| compiler | peak_storage_mib | 9 | 2 | 2 | 2 | 2 |
| compiler | peak_processes | 9 | 3 | 5 | 9 | 9 |
| general | peak_rss_mib | 9 | 107.57 | 157.58 | 258.57 | 258.57 |
| general | peak_pss_mib | 9 | 80.99 | 117.78 | 204.25 | 204.25 |
| general | peak_parent_rss_mib | 9 | 13.33 | 13.34 | 13.44 | 13.44 |
| general | peak_children_rss_mib | 9 | 94.12 | 144.25 | 245.13 | 245.13 |
| general | peak_cores | 9 | 1.015 | 2.025 | 3.794 | 3.794 |
| general | cpu_seconds | 9 | 11.207 | 14.638 | 27.682 | 27.682 |
| general | peak_storage_mib | 9 | 110.4 | 110.4 | 110.45 | 110.45 |
| general | peak_processes | 9 | 3 | 4 | 6 | 6 |
| memory | peak_rss_mib | 9 | 282.79 | 552.19 | 1091.95 | 1091.95 |
| memory | peak_pss_mib | 9 | 271.9 | 534.78 | 1061.08 | 1061.08 |
| memory | peak_parent_rss_mib | 9 | 13.28 | 13.39 | 14.27 | 14.27 |
| memory | peak_children_rss_mib | 9 | 269.39 | 538.91 | 1077.83 | 1077.83 |
| memory | peak_cores | 9 | 0.049 | 0.116 | 0.342 | 0.342 |
| memory | cpu_seconds | 9 | 0.101 | 0.181 | 0.456 | 0.456 |
| memory | peak_storage_mib | 9 | 0 | 0 | 0 | 0 |
| memory | peak_processes | 9 | 2 | 3 | 5 | 5 |

## Refused

- idle: an idle sample cannot size a worker

## Coexistence

| Node | Free memory MiB | Free CPU millis | Workers that fit | All profiles at once |
|---|---|---|---|---|
| anton-ec2-0396dfd54e94477e5 | 15645 | 3984 | browser 1, compiler 1, general 1, memory 5 | no |
| anton-ec2-0447e2769b1a3084a | 5405 | 1484 | browser 0, compiler 0, general 0, memory 1 | no |
| anton-ec2-0af3a8fe7769581ef | 4893 | 1234 | browser 0, compiler 0, general 0, memory 1 | no |
| anton-ec2-0c9b3493a0ef846f4 | 5405 | 1484 | browser 0, compiler 0, general 0, memory 1 | no |
| k8s-master | 6208 | 3783 | browser 1, compiler 1, general 1, memory 2 | no |
| k8s-worker-1 | 6276 | 1362 | browser 0, compiler 0, general 0, memory 2 | no |
| k8s-worker-2 | 10252 | 1417 | browser 0, compiler 0, general 0, memory 3 | no |
| worker-001 | 3073 | 4412 | browser 0, compiler 1, general 1, memory 1 | no |

## Measurement context

- host cpus: 4
- host kernel: 6.17.0-1022-azure
- host load_average_1m: 0.77
- host load_average_1m_after: 2.42
- host memory_available_mib: 14263
- host memory_total_mib: 15989
- tool cc: cc (Ubuntu 13.3.0-6ubuntu2~24.04.1) 13.3.0
- tool kernel: 6.17.0-1022-azure
- tool playwright: Version 1.63.0
- tool pytest: pytest 9.1.1
- tool python: Python 3.12.15
- tool workload:browser: 2
- tool workload:compiler: 2
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
- The idle class is a sleeping Python process standing in for an idle agent CLI.
- With nine runs per class the p95 column equals the maximum.
- Descendants that leave the workload's session and process tree are neither counted nor killed.
- A child that starts and exits between two samples never enters a peak window; CPU seconds still count it.
- Memory is split between the parent and its children; CPU and process count are reported for the whole tree.
- A workload change is detected through its declared version, not a fingerprint of its command.
- The sampler walks the workload folder on every sample; that cost runs outside the measured tree.
- Coexistence counts whole workers in each node's allocatable capacity minus the requests already scheduled there.
