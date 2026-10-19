"""Argos pilot safeguards without Argos installation, model loading or sockets."""

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def runner():
    path = Path(__file__).resolve().parents[1] / 'deployment/translation/benchmark_argos.py'
    spec = importlib.util.spec_from_file_location('argos_benchmark_under_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def args(tmp_path):
    cases = tmp_path / 'cases.json'
    cases.write_text(json.dumps({'cases': [
        {'id': 'first', 'text': 'One. Two.'},
        {'id': 'last-1', 'text': 'Third sentence.'},
        {'id': 'last-2', 'text': 'Fourth sentence.'},
    ]}), encoding='utf-8')
    sentence_model = tmp_path / 'minisbd/en.onnx'
    sentence_model.parent.mkdir()
    sentence_model.write_bytes(b'offline English ONNX model')
    return SimpleNamespace(cases=cases, data_dir=tmp_path / 'runtime',
        packages_dir=tmp_path / 'packages', package_version='1.9',
        model_archive=None, full_source=None, output=tmp_path / 'report.json',
        sbd_model=sentence_model)


@pytest.fixture
def fake_runtime(runner, args, monkeypatch):
    monkeypatch.setattr(runner.os, 'environ', runner.os.environ.copy())
    path = args.packages_dir / 'translate-en_ru-1_9'
    (path / 'stanza').mkdir(parents=True)
    (path / 'model').mkdir()
    metadata = {'from_code': 'en', 'to_code': 'ru', 'package_version': '1.9',
                'extra_upstream_field': 'retained verbatim'}
    (path / 'metadata.json').write_text(json.dumps(metadata), encoding='utf-8')
    (path / 'stanza/resources.json').write_text(json.dumps({'en': {
        'lang_name': 'English', 'tokenize': {'ewt': {'md5': 'original-model-checksum'}},
        'default_processors': {'tokenize': 'ewt'}}}), encoding='utf-8')
    (path / 'stanza/en/tokenize').mkdir(parents=True)
    (path / 'stanza/en/tokenize/ewt.pt').write_bytes(b'original packaged sentence detector')
    (path / 'model/model.bin').write_bytes(b'fake native model')
    package = SimpleNamespace(from_code='en', to_code='ru', package_version='1.9',
                              package_path=path)
    state = SimpleNamespace(packages=[package], constructor_options=[], requests=[],
                            detectors=[], hooks=[], torch_threads=None, torch_interop=None,
                            metadata=metadata, path=path, providers=['CPUExecutionProvider'],
                            onnx_threads=2)

    class Native:
        def __init__(self, model_path, **options):
            state.constructor_options.append((model_path, options))
            self.compute_type, self.device = 'int8_float32', options['device']

        def infer(self, text):
            state.requests.append(text)
            return f'Перевод {len(state.requests)}: {text}'

    class Sentencizer:
        detector = None

        def split_sentences(self, text):
            return self.detector.sentences(text)

    class PackageTranslation:
        def __init__(self, *_):
            self.sentencizer = Sentencizer()

        def translate(self, text):
            self.sentencizer.split_sentences(text)
            return self.translator.infer(text)

    class Detector:
        def __init__(self, model_path, **options):
            state.detectors.append((model_path, options))
            self.session = SimpleNamespace(get_providers=lambda: state.providers,
                get_session_options=lambda: SimpleNamespace(intra_op_num_threads=state.onnx_threads))

        def sentences(self, text):
            return ['One.', 'Two.'] if text == 'One. Two.' else [text]

    def forbidden_cache(*_):
        pytest.fail('Benchmark must not use Argos text response cache')

    fake_translate = SimpleNamespace(PackageTranslation=PackageTranslation,
        Language=lambda *x: SimpleNamespace(code=x[0]),
        CachedTranslation=forbidden_cache, get_translation_from_codes=forbidden_cache,
        translate=forbidden_cache)
    monkeypatch.setitem(sys.modules, 'argostranslate', SimpleNamespace(
        package=SimpleNamespace(get_installed_packages=lambda: state.packages),
        translate=fake_translate))
    monkeypatch.setitem(sys.modules, 'ctranslate2', SimpleNamespace(Translator=Native))
    monkeypatch.setitem(sys.modules, 'minisbd', SimpleNamespace(SBDetect=Detector))
    monkeypatch.setitem(sys.modules, 'torch', SimpleNamespace(
        set_num_threads=lambda n: setattr(state, 'torch_threads', n),
        set_num_interop_threads=lambda n: setattr(state, 'torch_interop', n),
        get_num_threads=lambda: state.torch_threads,
        get_num_interop_threads=lambda: state.torch_interop))
    monkeypatch.setitem(sys.modules, 'resource', SimpleNamespace(RUSAGE_SELF=0,
        getrusage=lambda _: SimpleNamespace(ru_maxrss=2048)))
    # A real audit hook is irreversible; record registration and test its handler
    # directly below so this test does not modify the pytest process permanently.
    monkeypatch.setattr(sys, 'addaudithook', state.hooks.append)
    monkeypatch.setattr(runner, 'installed_version', lambda _: 'test-fixture')
    return state


@pytest.mark.parametrize('event', ['socket.connect', 'socket.getaddrinfo'])
def test_offline_guard_blocks_network_before_dependencies_can_download(runner, event):
    with pytest.raises(RuntimeError, match='NETWORK_DISABLED'):
        runner.no_network(event, ('translation.example', 443))
    runner.no_network('open', ('local-model.bin', 'rb', 0))


@pytest.mark.parametrize('corpus', [None, [], [{'id': 'x', 'text': 'only one'}],
    [{'id': 'x', 'text': ''}, {'id': 'y', 'text': 'ok'}],
    [{'id': 'x', 'text': 'ok'}, {'id': 'x', 'text': 'duplicate'}]])
def test_invalid_corpus_rejected_before_loading_models(runner, args, fake_runtime, corpus):
    args.cases.write_text(json.dumps({'cases': corpus}), encoding='utf-8')
    with pytest.raises(ValueError, match='ARGOS_CORPUS'):
        runner.run(args, {'cases': []})
    assert not fake_runtime.constructor_options and not fake_runtime.requests


@pytest.mark.parametrize('package_count', [0, 2])
def test_requires_exactly_one_pinned_direct_en_ru_package(runner, args, fake_runtime, package_count):
    fake_runtime.packages *= package_count
    with pytest.raises(RuntimeError, match='EXACTLY_ONE_PINNED'):
        runner.run(args, {'cases': []})
    assert not fake_runtime.constructor_options and not fake_runtime.requests


def test_other_package_version_is_not_silently_selected(runner, args, fake_runtime):
    fake_runtime.packages[0].package_version = '1.8'
    with pytest.raises(RuntimeError, match='EXACTLY_ONE_PINNED'):
        runner.run(args, {'cases': []})


def test_repeats_run_inference_and_capture_actual_offline_sentence_boundaries(
        runner, args, fake_runtime, monkeypatch):
    monkeypatch.setenv('ARGOS_DEVICE_TYPE', 'cuda')
    monkeypatch.setenv('CUDA_VISIBLE_DEVICES', '0')
    report = {'cases': []}
    runner.run(args, report)
    rows = report['cases']
    assert [(item['id'], item['repeat']) for item in rows] == [
        ('first', 1), ('last-1', 1), ('last-2', 1), ('last-1', 2), ('last-2', 2)]
    assert len(fake_runtime.requests) == 6  # Separate warmup plus five inference calls.
    assert rows[1]['translated'] != rows[3]['translated']
    assert rows[2]['translated'] != rows[4]['translated']
    assert report['summary']['total'] == report['summary']['successful'] == 5
    assert rows[0]['sentence_boundaries'] == [{'paragraph': 'One. Two.',
                                             'sentences': ['One.', 'Two.']}]
    assert report['warmup']['sentence_boundaries'][0]['sentences'] == [
        'This is a translation speed test.']
    assert all(item['source'] != report['warmup']['source'] for item in rows)
    assert report['runtime'] == {'ctranslate2_device': 'cpu', 'cuda_visible_devices': '',
                                'torch_num_threads': 2, 'torch_num_interop_threads': 1}
    assert fake_runtime.constructor_options[0][1] == {'device': 'cpu', 'compute_type': 'int8',
                                                    'intra_threads': 2, 'inter_threads': 1}
    assert fake_runtime.detectors == [(str(args.sbd_model), {'use_gpu': False, 'max_threads': 2})]
    assert runner.os.environ['ARGOS_CHUNK_TYPE'] == 'MINISBD'
    assert report['sbd_model']['providers'] == ['CPUExecutionProvider']
    assert report['sbd_model']['sha256'] == runner.digest(args.sbd_model)
    assert fake_runtime.hooks == [runner.no_network]
    assert report['package']['metadata'] == fake_runtime.metadata
    assert report['package']['files']['model/model.bin']['sha256'] == runner.digest(
        fake_runtime.path / 'model/model.bin')
    assert json.loads(args.output.read_text(encoding='utf-8'))['status'] == 'complete'


def test_minisbd_selection_preserves_original_model_package(runner, args, fake_runtime):
    original = fake_runtime.path / 'stanza/resources.json'
    checkpoint = fake_runtime.path / 'stanza/en/tokenize/ewt.pt'
    before = {path: path.read_bytes() for path in (original, checkpoint)}
    report = {'cases': []}
    runner.run(args, report)
    assert all(path.read_bytes() == value for path, value in before.items())
    assert report['sentence_detector'] == 'minisbd_english_onnx_offline'


@pytest.mark.parametrize('broken', ['missing', 'empty'])
def test_minisbd_missing_local_asset_never_triggers_download(
        runner, args, fake_runtime, broken):
    if broken == 'missing':
        args.sbd_model.unlink()
    else:
        args.sbd_model.write_bytes(b'')
    with pytest.raises(RuntimeError, match='LOCAL_MINISBD_MODEL_REQUIRED_NO_DOWNLOAD_ALLOWED'):
        runner.run(args, {'cases': []})
    assert not fake_runtime.detectors and not fake_runtime.requests


@pytest.mark.parametrize('invalid_runtime', ['gpu', 'threads'])
def test_minisbd_rejects_accelerated_or_unbounded_session(runner, args, fake_runtime, invalid_runtime):
    if invalid_runtime == 'gpu':
        fake_runtime.providers = ['CUDAExecutionProvider', 'CPUExecutionProvider']
    else:
        fake_runtime.onnx_threads = 0
    with pytest.raises(RuntimeError, match='MINISBD_CPU_TWO_THREADS_REQUIRED'):
        runner.run(args, {'cases': []})
    assert not fake_runtime.requests


@pytest.mark.parametrize('original', ['', None, 123])
def test_full_source_requires_complete_nonempty_original(runner, args, fake_runtime, original):
    args.full_source = args.cases.parent / 'full.json'
    args.full_source.write_text(json.dumps({'original': original}), encoding='utf-8')
    with pytest.raises(ValueError, match='FULL_SOURCE_ORIGINAL_REQUIRED'):
        runner.run(args, {'cases': []})
    assert not fake_runtime.constructor_options


def test_full_article_is_included_once_without_truncation(runner, args, fake_runtime):
    args.full_source = args.cases.parent / 'full.json'
    original = 'Complete original paragraph.\n\nIts final sentence must survive.'
    args.full_source.write_text(json.dumps({'original': original}), encoding='utf-8')
    report = {'cases': []}
    runner.run(args, report)
    last = report['cases'][-1]
    assert last['id'] == 'nasa-escapade-full' and last['repeat'] == 1
    assert last['source'] == original and fake_runtime.requests[-1] == original
    assert report['summary']['total'] == 6
