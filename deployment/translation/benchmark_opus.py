"""Reproduce the isolated EN->RU OPUS-MT pilot without touching the Hub."""

import argparse
import hashlib
import importlib.util
from importlib.metadata import version
import json
import os
import platform
import resource
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path


def write_report(path, report):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    os.replace(temporary, path)


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cases', type=Path, required=True)
    parser.add_argument('--service', type=Path, required=True)
    parser.add_argument('--prompt', type=Path, required=True)
    parser.add_argument('--model', type=Path, required=True)
    parser.add_argument('--tokenizer', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    cases = json.loads(args.cases.read_text(encoding='utf-8'))['cases']
    spec = importlib.util.spec_from_file_location('translation_service_under_test', args.service)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    started = time.perf_counter()
    import ctranslate2
    from transformers import MarianTokenizer
    imports_seconds = time.perf_counter() - started

    translator = module.Translator({'prompt_path': str(args.prompt), 'gguf': {},
                                    'opus_path': str(args.model), 'opus_source': str(args.tokenizer)})
    started = time.perf_counter()
    translator.opus = ctranslate2.Translator(str(args.model), device='cpu', compute_type='int8',
                                           intra_threads=2, inter_threads=1)
    model_load_seconds = time.perf_counter() - started
    started = time.perf_counter()
    translator.tokenizer = MarianTokenizer.from_pretrained(str(args.tokenizer), local_files_only=True)
    tokenizer_load_seconds = time.perf_counter() - started
    report = {
        'schema_version': 1,
        'model': 'opus-mt-en-ru-int8',
        'checked_at': datetime.now(timezone.utc).isoformat(),
        'host': platform.node(),
        'python': platform.python_version(),
        'ctranslate2': ctranslate2.__version__,
        'transformers': version('transformers'),
        'sacremoses': version('sacremoses'),
        'cases_sha256': hashlib.sha256(args.cases.read_bytes()).hexdigest(),
        'service_sha256': hashlib.sha256(args.service.read_bytes()).hexdigest(),
        'model_sha256': digest(args.model / 'model.bin'),
        'compute_type': translator.opus.compute_type,
        'device': 'cpu',
        'intra_threads': 2,
        'inter_threads': 1,
        'beam_size': 4,
        'imports_seconds': round(imports_seconds, 6),
        'model_load_seconds': round(model_load_seconds, 6),
        'tokenizer_load_seconds': round(tokenizer_load_seconds, 6),
        'unique_cases': len(cases),
        'repeat_case_ids': [case['id'] for case in cases[-2:]],
        'results': [],
    }
    warmup = translator.translate(report['model'], 'This is a translation speed test.')
    report['warmup'] = {'text': warmup['text'], 'metrics': warmup['metrics']}
    write_report(args.output, report)
    work = [(case, 1) for case in cases] + [(case, 2) for case in cases[-2:]]
    try:
        for case, repeat in work:
            started = time.perf_counter()
            result = {'id': case['id'], 'repeat': repeat, 'text': case['text'],
                      'reference_ru': case['reference_ru'], 'type': case['type']}
            try:
                translated = translator.translate(report['model'], case['text'])
                result.update(output=translated['text'], complete=translated['complete'],
                              metrics=translated['metrics'], error=None)
            except Exception as exc:
                result.update(output=None, complete=False,
                              error={'type': type(exc).__name__, 'message': str(exc)[:300]})
            result['wall_seconds'] = round(time.perf_counter() - started, 6)
            report['results'].append(result)
            report['peak_rss_mib'] = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 3)
            write_report(args.output, report)
            print(json.dumps({'id': case['id'], 'repeat': repeat,
                              'seconds': result['wall_seconds'], 'error': result['error']}), flush=True)
        successful = [item['wall_seconds'] for item in report['results'] if item['error'] is None]
        report['summary'] = {
            'successful': len(successful), 'total': len(work),
            'median_seconds': round(statistics.median(successful), 6) if successful else None,
            'total_translation_seconds': round(sum(successful), 6),
        }
        report['completed_at'] = datetime.now(timezone.utc).isoformat()
        write_report(args.output, report)
    finally:
        translator.http.close()


if __name__ == '__main__':
    main()
