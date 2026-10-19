"""Measure the actual loopback translation gateway without publishing anything."""
import argparse
import hashlib
import json
import statistics
import time
from pathlib import Path

import httpx


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--cases', type=Path, required=True)
    parser.add_argument('--model', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--base-url', default='http://127.0.0.1:8094')
    args = parser.parse_args()
    raw = args.cases.read_bytes()
    cases = json.loads(raw)['cases']
    rows = []
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=600, trust_env=False) as client:
        for case, repeat in [(c, 1) for c in cases] + [(c, 2) for c in cases[-2:]]:
            row = {'model': args.model, 'case': case['id'], 'repeat': repeat}
            started = time.monotonic()
            try:
                response = client.post(args.base_url + '/translate', json={
                    'model': args.model, 'text': case['text'], 'source': 'en', 'target': 'ru'})
                response.raise_for_status()
                row.update(response.json())
            except Exception as exc:
                row['error'] = type(exc).__name__ + ': ' + str(exc)[:150]
            row['seconds'] = round(time.monotonic() - started, 3)
            rows.append(row)
            report = {'model': args.model, 'cases_sha256': hashlib.sha256(raw).hexdigest(),
                'results': rows, 'median_seconds': statistics.median(x['seconds'] for x in rows)}
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            print(json.dumps({k: row.get(k) for k in ['model', 'case', 'repeat', 'seconds', 'error']}), flush=True)


if __name__ == '__main__':
    main()
