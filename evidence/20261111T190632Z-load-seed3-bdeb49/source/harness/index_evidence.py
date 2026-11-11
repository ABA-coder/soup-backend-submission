"""Build a navigable ledger without changing any raw result."""
from collections import Counter
import json
from pathlib import Path
import re


def main():
    root = Path(__file__).resolve().parents[1]
    directories = sorted((root / 'evidence').glob('*/'))
    tests = sorted((root / 'evidence').glob('tests-*.log'))
    lines = ['# Evidence index', '',
             'Every experiment invoked during this implementation is listed below. '
             'A FAIL/aborted run remains part of the record. Windows results are not Linux SIGKILL evidence.', '']
    statuses = Counter()
    linux_runs = 0
    details = []
    for directory in directories:
        events = [json.loads(line) for line in (directory / 'run.jsonl').read_text(encoding='utf-8').splitlines()]
        start = events[0]
        summary_path = directory / 'summary.json'
        summary = json.loads(summary_path.read_text()) if summary_path.exists() else {'invariants': 'INCOMPLETE'}
        status = summary['invariants']
        statuses[status] += 1
        arguments = start['arguments']
        linux_runs += start['kill_method'] == 'SIGKILL' and arguments['mode'] == 'chaos'
        relative = directory.relative_to(root).as_posix()
        details.extend([
            f"## {directory.name}", '',
            f"**{status}** | {arguments['mode']} | {arguments['jobs']:,} intended jobs | "
            f"{arguments['workers']} workers | seed {arguments['seed']} | {start['kill_method']}", '',
            f"[Raw events]({relative}/run.jsonl) | [Every HTTP response]({relative}/requests.jsonl) | "
            f"[Summary]({relative}/summary.json) | [Input manifest]({relative}/inputs.jsonl)", '',
            '```json', json.dumps(summary, indent=2), '```', '',
        ])
        if (directory / 'intervention.json').exists():
            details += [f"[Manual intervention record]({relative}/intervention.json). "
                        'This run is invalid as worker-kill coverage; see REPORT.md.', '']
        rows = [json.loads(line) for line in (directory / 'requests.jsonl').read_text(encoding='utf-8').splitlines()]
        initial = [row for row in rows if row['attempt'] == 0 and row['path'] == '/jobs' and row['method'] == 'POST']
        counts = Counter(row['status'] for row in initial)
        latencies = sorted(row['latency_ms'] for row in initial)
        if latencies:
            percentiles = {name: latencies[min(len(latencies)-1, int(len(latencies)*fraction))]
                           for name, fraction in [('p50', .5), ('p95', .95), ('p99', .99), ('max', 1)]}
            details += [f'Initial POST attempts (before retries): **{len(initial)}**. '
                        f'HTTP statuses: `{dict(counts)}`. Latency in ms: `{percentiles}`.', '']
        schedules = [event for event in events if event['event'] == 'arrival_schedule']
        if schedules:
            details += ['Open-loop arrival schedule (includes any client scheduling lag):', '',
                        '```json', json.dumps(schedules[-1], indent=2), '```', '']
        kills = [event for event in events if event['event'] == 'worker_killed']
        if len(kills) > 1:
            gaps = [b['elapsed_s'] - a['elapsed_s'] for a, b in zip(kills, kills[1:])]
            details += [f'Observed kill intervals: mean {sum(gaps)/len(gaps):.3f}s; '
                        f'min {min(gaps):.3f}s; max {max(gaps):.3f}s.', '']
    lines += [f'Experiment invocations: **{len(directories)}**; outcomes: **{dict(statuses)}**. '
              f'Pytest invocations: **{len(tests)}**. Linux SIGKILL invocations: **{linux_runs}**. '
              'Container startup validation is tracked separately in REPORT.md.', '']
    lines += ['## Targeted test transcripts', '']
    for path in tests:
        text = path.read_text(encoding='utf-8')
        summaries = re.findall(r'=+ (.*(?:passed|failed|error).*?) =+', text)
        lines += [f"- [{path.name}]({path.relative_to(root).as_posix()}): "
                  f"{summaries[-1] if summaries else 'see raw log'}; "
                  f"{text.strip().splitlines()[-1]}"]
    lines += ['', *details]
    (root / 'EVIDENCE_INDEX.md').write_text('\n'.join(lines), encoding='utf-8')
    print(f'Indexed {len(directories)} experiments and {len(tests)} test invocations')


if __name__ == '__main__':
    main()
