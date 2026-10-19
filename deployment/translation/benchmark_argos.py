"""Isolated CPU Argos benchmark; install the EN->RU package before running.

No package installation or network downloads are performed by this runner.
PackageTranslation is used directly so repeated cases still run inference.
"""

import argparse
import hashlib
import json
import os
import platform
import statistics
import time
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def save(path, report):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    os.replace(temporary, path)


def installed_version(name):
    try:
        return version(name)
    except PackageNotFoundError:
        return None


def no_network(event, arguments):
    if event in {'socket.connect', 'socket.getaddrinfo'}:
        raise RuntimeError('ARGOS_BENCHMARK_NETWORK_DISABLED')


def workload(args):
    """Validate all inputs before loading a model or starting the timed work."""
    value = json.loads(args.cases.read_text(encoding='utf-8'))
    corpus = value.get('cases') if isinstance(value, dict) else None
    if (not isinstance(corpus, list) or len(corpus) < 2
            or any(not isinstance(item, dict) or not isinstance(item.get('id'), str)
                   or not item['id'].strip() or not isinstance(item.get('text'), str)
                   or not item['text'].strip() for item in corpus)):
        raise ValueError('ARGOS_CORPUS_CASES_INVALID')
    if len({item['id'] for item in corpus}) != len(corpus):
        raise ValueError('ARGOS_CORPUS_IDS_DUPLICATE')
    work = [(item, 1) for item in corpus] + [(item, 2) for item in corpus[-2:]]
    metadata = {'corpus_sha256': digest(args.cases), 'unique_pilot_cases': len(corpus)}
    if args.full_source:
        full = json.loads(args.full_source.read_text(encoding='utf-8'))
        if (not isinstance(full, dict) or not isinstance(full.get('original'), str)
                or not full['original'].strip()):
            raise ValueError('FULL_SOURCE_ORIGINAL_REQUIRED')
        if any(item['id'] == 'nasa-escapade-full' for item in corpus):
            raise ValueError('ARGOS_CORPUS_IDS_DUPLICATE')
        metadata['full_source_sha256'] = digest(args.full_source)
        work.append(({'id': 'nasa-escapade-full', 'text': full['original'],
                      'type': 'project_full_body'}, 1))
    return work, metadata


def run(args, report):
    work, corpus_metadata = workload(args)
    report.update(corpus_metadata)
    if not args.sbd_model.is_file() or args.sbd_model.stat().st_size == 0:
        raise RuntimeError('LOCAL_MINISBD_MODEL_REQUIRED_NO_DOWNLOAD_ALLOWED')
    # Set all settings before importing Argos, CTranslate2, Torch or Stanza.
    for key, value in {
        'XDG_DATA_HOME': str(args.data_dir / 'share'),
        'XDG_CACHE_HOME': str(args.data_dir / 'cache'),
        'XDG_CONFIG_HOME': str(args.data_dir / 'config'),
        'ARGOS_PACKAGES_DIR': str(args.packages_dir),
        'ARGOS_DEVICE_TYPE': 'cpu',
        'ARGOS_COMPUTE_TYPE': 'int8',
        'ARGOS_INTER_THREADS': '1',
        'ARGOS_INTRA_THREADS': '2',
        'ARGOS_CHUNK_TYPE': 'MINISBD',
        'ARGOS_MODEL_PROVIDER': 'OPENNMT',
        'ARGOS_BEAM_SIZE': '4',
        'OMP_NUM_THREADS': '2',
        'MKL_NUM_THREADS': '2',
        'OPENBLAS_NUM_THREADS': '2',
        'NUMEXPR_NUM_THREADS': '2',
        'CUDA_VISIBLE_DEVICES': '',
        'HF_HUB_OFFLINE': '1',
        'TRANSFORMERS_OFFLINE': '1',
    }.items():
        os.environ[key] = value
    import resource
    import sys

    # All assets must be prepared first; no dependency may lazily download data.
    sys.addaudithook(no_network)
    started = time.perf_counter()
    import ctranslate2
    import torch
    from argostranslate import package, translate
    from minisbd import SBDetect
    report['imports_seconds'] = round(time.perf_counter() - started, 6)
    torch.set_num_threads(2)
    torch.set_num_interop_threads(1)
    packages = [item for item in package.get_installed_packages()
                if item.from_code == 'en' and item.to_code == 'ru'
                and item.package_version == args.package_version]
    if len(packages) != 1:
        raise RuntimeError('EXACTLY_ONE_PINNED_EN_RU_PACKAGE_REQUIRED')
    selected = packages[0]
    metadata = selected.package_path / 'metadata.json'
    report['package'] = {
        'metadata': json.loads(metadata.read_text(encoding='utf-8')),
        'path': str(selected.package_path),
        'files': {path.relative_to(selected.package_path).as_posix(): {
            'size': path.stat().st_size, 'sha256': digest(path)}
            for path in sorted(selected.package_path.rglob('*')) if path.is_file()},
    }
    if args.model_archive:
        report['package']['archive'] = {'name': args.model_archive.name,
            'size': args.model_archive.stat().st_size, 'sha256': digest(args.model_archive)}
    report['versions'] = {name: installed_version(name) for name in (
        'argostranslate', 'ctranslate2', 'sentencepiece', 'stanza', 'torch',
        'spacy', 'minisbd', 'onnxruntime', 'sacremoses')}
    # Avoid CachedTranslation: repeated inputs must measure translation work.
    translator = translate.PackageTranslation(translate.Language('en', 'English'),
        translate.Language('ru', 'Russian'), selected)
    started = time.perf_counter()
    translator.translator = ctranslate2.Translator(str(selected.package_path / 'model'),
        device='cpu', compute_type='int8', intra_threads=2, inter_threads=1)
    report['model_load_seconds'] = round(time.perf_counter() - started, 6)
    report['compute_type'] = translator.translator.compute_type
    report['runtime'] = {'ctranslate2_device': translator.translator.device,
        'cuda_visible_devices': os.environ['CUDA_VISIBLE_DEVICES'],
        'torch_num_threads': torch.get_num_threads(),
        'torch_num_interop_threads': torch.get_num_interop_threads()}
    started = time.perf_counter()
    detector = SBDetect(str(args.sbd_model), use_gpu=False, max_threads=2)
    translator.sentencizer.detector = detector
    report['sentence_detector_load_seconds'] = round(time.perf_counter() - started, 6)
    report['sentence_detector'] = 'minisbd_english_onnx_offline'
    report['sbd_model'] = {'path': str(args.sbd_model), 'size': args.sbd_model.stat().st_size,
        'sha256': digest(args.sbd_model),
        'source_url': 'https://github.com/LibreTranslate/MiniSBD/releases/download/v0.0.1/en.onnx',
        'providers': detector.session.get_providers(),
        'intra_op_num_threads': detector.session.get_session_options().intra_op_num_threads}
    if (report['sbd_model']['providers'] != ['CPUExecutionProvider']
            or report['sbd_model']['intra_op_num_threads'] != 2):
        raise RuntimeError('MINISBD_CPU_TWO_THREADS_REQUIRED')
    boundaries = []
    original_split = translator.sentencizer.split_sentences

    def record_boundaries(text):
        sentences = original_split(text)
        boundaries.append({'paragraph': text, 'sentences': list(sentences)})
        return sentences

    translator.sentencizer.split_sentences = record_boundaries
    started = time.perf_counter()
    warmup = translator.translate('This is a translation speed test.')
    report['warmup'] = {'source': 'This is a translation speed test.', 'translated': warmup,
                        'seconds': round(time.perf_counter() - started, 6),
                        'sentence_boundaries': list(boundaries)}
    report['status'] = 'running'
    save(args.output, report)
    for case, repeat in work:
        item = {'id': case['id'], 'source': case['text'], 'repeat': repeat,
                'type': case.get('type'), 'reference_ru': case.get('reference_ru')}
        boundaries.clear()
        started = time.perf_counter()
        try:
            item['translated'] = translator.translate(case['text'])
            if not isinstance(item['translated'], str) or not item['translated'].strip():
                raise ValueError('ARGOS_TRANSLATION_EMPTY')
            item['error'] = None
        except Exception as exc:
            item['translated'] = None
            item['error'] = {'type': type(exc).__name__, 'message': str(exc)[:300]}
        item['seconds'] = round(time.perf_counter() - started, 6)
        item['sentence_boundaries'] = list(boundaries)
        report['cases'].append(item)
        report['peak_rss_mib'] = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 3)
        save(args.output, report)
        print(json.dumps({'id': item['id'], 'repeat': repeat, 'seconds': item['seconds'],
                          'error': item['error']}), flush=True)
    timings = [item['seconds'] for item in report['cases'] if item['error'] is None]
    report['summary'] = {'successful': len(timings), 'total': len(work),
        'median_seconds': round(statistics.median(timings), 6) if timings else None,
        'total_translation_seconds': round(sum(timings), 6)}
    report['status'] = 'complete'
    report['completed_at'] = datetime.now(timezone.utc).isoformat()
    save(args.output, report)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cases', type=Path, required=True)
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--packages-dir', type=Path, required=True)
    parser.add_argument('--sbd-model', type=Path, required=True,
                        help='Prepared local English MiniSBD ONNX model; never downloaded by this runner')
    parser.add_argument('--package-version', default='1.9')
    parser.add_argument('--model-archive', type=Path)
    parser.add_argument('--full-source', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    report = {'schema_version': 1, 'model': 'argos-en-ru-' + args.package_version,
        'engine': 'argos-translate', 'host': platform.node(), 'python': platform.python_version(),
        'checked_at': datetime.now(timezone.utc).isoformat(), 'device': 'cpu',
        'intra_threads': 2, 'inter_threads': 1, 'beam_size': 4,
        'response_cache': False, 'network_downloads_allowed': False,
        'status': 'initializing', 'cases': []}
    save(args.output, report)
    try:
        run(args, report)
    except Exception as exc:
        report['status'] = 'failed'
        report['error'] = {'type': type(exc).__name__, 'message': str(exc)[:500]}
        save(args.output, report)
        raise


if __name__ == '__main__':
    main()
