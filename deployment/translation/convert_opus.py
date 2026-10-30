"""Pinned OPUS-MT conversion; run in the isolated translation environment."""
import hashlib
import json
from pathlib import Path

import ctranslate2
from huggingface_hub import snapshot_download

ROOT = Path('/opt/max-postiz/data/translation-models')
REVISION = 'bb09c99d180016eac6819df3dae68edb1690fdee'
source = snapshot_download('Helsinki-NLP/opus-mt-en-ru', revision=REVISION,
    local_dir=ROOT / 'opus-source', allow_patterns=['*.json', '*.spm', 'pytorch_model.bin'])
weights = Path(source) / 'pytorch_model.bin'
assert hashlib.sha256(weights.read_bytes()).hexdigest() == 'd15fa58c6bc3efd3629c1b6b86d9aa6d15d2751a4620aa4cdd7eed7b5cbe583b'
target = ROOT / 'opus-mt-en-ru-int8'
if not (target / 'model.bin').exists():
    ctranslate2.converters.TransformersConverter(source, copy_files=[
        'source.spm', 'target.spm', 'vocab.json', 'tokenizer_config.json']).convert(
            str(target), quantization='int8')
translator = ctranslate2.Translator(str(target), device='cpu', compute_type='int8', intra_threads=2)
report = {'repo':'Helsinki-NLP/opus-mt-en-ru','revision':REVISION,
          'compute_type':translator.compute_type,'files':{p.name:hashlib.sha256(p.read_bytes()).hexdigest()
          for p in target.iterdir() if p.is_file()}}
(ROOT / 'opus-conversion.json').write_text(json.dumps(report,indent=2))
print(json.dumps(report), flush=True)
