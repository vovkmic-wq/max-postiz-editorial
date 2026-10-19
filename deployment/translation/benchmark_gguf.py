"""Sequential, bounded GGUF benchmark; restores the existing reviewer in finally."""
import argparse
import json
import subprocess
import time
from pathlib import Path

import httpx


def run(args):
    subprocess.run(args, check=True, capture_output=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=Path('/opt/translation-lab'))
    parser.add_argument('--weights', type=Path, required=True)
    parser.add_argument('--model', help='Optional single model ID for an explicitly recorded retry')
    parser.add_argument('--gpu-layers', type=int, help='Explicit retry profile')
    parser.add_argument('--output-name', default='gguf.json')
    parser.add_argument('--chat-template', type=Path)
    args = parser.parse_args()
    root = args.root
    output = root / 'results'
    output.mkdir(exist_ok=True)
    cases = json.loads((root / 'cases.json').read_text())['cases']
    models = json.loads((root / 'models.lock.json').read_text())
    if args.model:
        models = [model for model in models if model['id'] == args.model]
        if len(models) != 1:
            raise ValueError('Unknown model ID')
    if args.gpu_layers is not None:
        for model in models:
            model['gpu_layers'] = args.gpu_layers
    prompt = (root / 'DEDICATED_TRANSLATE.txt').read_text()
    client = httpx.Client(timeout=360, trust_env=False)
    base = 'http://10.20.30.143:8087'
    restore = subprocess.run(['systemctl', 'is-active', '--quiet', 'llama-k2200']).returncode == 0
    rows = []

    def save():
        (output / args.output_name).write_text(json.dumps(rows, ensure_ascii=False, indent=2))

    try:
        if restore:
            slots = client.get('http://10.20.30.143:8086/slots').json()
            if any(slot.get('is_processing') for slot in slots):
                raise RuntimeError('REVIEWER_BUSY')
        subprocess.run(['systemctl', 'stop', 'hy-mt2-cpu'], capture_output=True)
        run(['systemctl', 'stop', 'llama-k2200'])
        for model in models:
            model_id = model['id']
            log = (output / (model_id + '.log')).open('w')
            command = ['/opt/llama.cpp/build-cuda-k2200/bin/llama-server',
                '--model', str(args.weights / model['file']), '--alias', model_id,
                '--host', '10.20.30.143', '--port', '8087', '--n-gpu-layers', str(model['gpu_layers']),
                '--ctx-size', '4096', '--threads', '6', '--threads-batch', '6',
                '--batch-size', '128', '--ubatch-size', '128', '--parallel', '1',
                '--flash-attn', 'off', '--jinja']
            if args.chat_template:
                command.extend(['--chat-template-file', str(args.chat_template)])
            started = time.monotonic()
            process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
            try:
                while time.monotonic() - started < 180:
                    if process.poll() is not None:
                        raise RuntimeError('MODEL_LOAD_FAILED')
                    try:
                        if client.get(base + '/health', timeout=2).status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(2)
                else:
                    raise RuntimeError('MODEL_LOAD_TIMEOUT')
                load_seconds = round(time.monotonic() - started, 3)
                gpu = subprocess.run(['nvidia-smi', '--query-gpu=memory.used,memory.free',
                    '--format=csv,noheader,nounits'], capture_output=True, text=True).stdout.strip()
                # Two repeated short cases supplement the seven unique inputs.
                schedule = [(case, 1) for case in cases] + [(case, 2) for case in cases[-2:]]
                for case, repeat in schedule:
                    row = {'model': model_id, 'case': case['id'], 'repeat': repeat,
                        'gpu_layers': model['gpu_layers'], 'load_seconds': load_seconds,
                        'gpu_memory_mib': gpu, 'source': case['text']}
                    tick = time.monotonic()
                    try:
                        if model_id.startswith('translategemma'):
                            content = [{'type': 'text', 'source_lang_code': 'en',
                                'target_lang_code': 'ru', 'text': case['text']}]
                        else:
                            content = prompt.replace('{source_text}', case['text'])
                        response = client.post(base + '/v1/chat/completions', json={
                            'model': model_id, 'messages': [{'role': 'user', 'content': content}],
                            'temperature': 0, 'seed': 42, 'max_tokens': 1024,
                            'cache_prompt': False, 'stream': False})
                        response.raise_for_status()
                        data = response.json()
                        choice = data['choices'][0]
                        row.update(text=choice['message']['content'], finish_reason=choice['finish_reason'],
                            timings=data.get('timings'), usage=data.get('usage'))
                    except Exception as exc:
                        row['error'] = type(exc).__name__ + ': ' + str(exc)[:250]
                        if isinstance(exc, httpx.HTTPStatusError):
                            row['error_detail'] = exc.response.text[:1200]
                    row['seconds'] = round(time.monotonic() - tick, 3)
                    rows.append(row)
                    save()
                    print(json.dumps({'model': model_id, 'case': case['id'], 'repeat': repeat,
                        'seconds': row['seconds'], 'error': row.get('error'),
                        'finish': row.get('finish_reason')}, ensure_ascii=False), flush=True)
            except Exception as exc:
                rows.append({'model': model_id, 'error': str(exc), 'phase': 'load'})
                save()
                print(model_id + ': ' + str(exc), flush=True)
            finally:
                process.terminate()
                try:
                    process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                log.close()
    finally:
        if restore:
            run(['systemctl', 'start', 'llama-k2200'])
        client.close()


if __name__ == '__main__':
    main()
