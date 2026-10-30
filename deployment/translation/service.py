"""Loopback-only translation worker. No publishing or arbitrary model downloads."""
import argparse
import json
import math
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import httpx


def _http_origin(value, paths):
    if (not isinstance(value, str) or len(value) > 2048
            or any(char.isspace() or ord(char) < 32 for char in value)
            or any(char in value for char in ('\\', '?', '#'))):
        raise ValueError('TRANSLATION_MEMORY_GUARD_CONFIG_INVALID')
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in {'http', 'https'} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.path not in paths or parsed.query or parsed.fragment):
            raise ValueError
        port = parsed.port
        if port is None:
            port = 443 if parsed.scheme == 'https' else 80
        if not 1 <= port <= 65535:
            raise ValueError
        return parsed.scheme, parsed.hostname.lower(), port
    except ValueError as exc:
        raise ValueError('TRANSLATION_MEMORY_GUARD_CONFIG_INVALID') from exc


def _memory_guard_config(config):
    guard = config.get('memory_guard')
    if guard is None:
        return None
    required = {'model', 'peer_props_url', 'own_props_url', 'timeout_seconds', 'poll_seconds'}
    if (not isinstance(guard, dict) or set(guard) != required
            or guard['model'] != 'hy-mt2-7b-q8'):
        raise ValueError('TRANSLATION_MEMORY_GUARD_CONFIG_INVALID')
    for key in ('timeout_seconds', 'poll_seconds'):
        value = guard[key]
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 180:
            raise ValueError('TRANSLATION_MEMORY_GUARD_CONFIG_INVALID')
    if not 0.05 <= guard['poll_seconds'] <= min(30, guard['timeout_seconds']):
        raise ValueError('TRANSLATION_MEMORY_GUARD_CONFIG_INVALID')
    own = _http_origin(guard['own_props_url'], {'/props'})
    peer = _http_origin(guard['peer_props_url'], {'/props'})
    routes = config.get('gguf')
    if not isinstance(routes, dict):
        raise ValueError('TRANSLATION_MEMORY_GUARD_CONFIG_INVALID')
    route = _http_origin(routes.get(guard['model']), {'', '/'})
    if own != route or own[:2] != peer[:2] or own == peer:
        raise ValueError('TRANSLATION_MEMORY_GUARD_CONFIG_INVALID')
    return dict(guard)


class Translator:
    def __init__(self, config, *, clock=None, sleep=None):
        self.config = config
        self.memory_guard = _memory_guard_config(config)
        self.clock, self.sleep = clock or time.monotonic, sleep or time.sleep
        self.lock = threading.Lock()
        self.prompt = Path(config['prompt_path']).read_text(encoding='utf-8')
        self.http = httpx.Client(timeout=600, trust_env=False, follow_redirects=False)
        self.opus = self.tokenizer = None

    def _wait_sleeping(self, url):
        # Passive observation only: this is not an exclusive lease over the host.
        started = self.clock()
        deadline = started + self.memory_guard['timeout_seconds']
        while True:
            remaining = deadline - self.clock()
            if remaining <= 0:
                raise ValueError('TRANSLATION_MEMORY_GUARD_TIMEOUT')
            try:
                response = self.http.get(url, timeout=min(5, remaining))
                if response.status_code not in {200, 503}:
                    raise ValueError('TRANSLATION_MEMORY_GUARD_UNAVAILABLE')
                if len(response.content) > 65536:
                    raise ValueError('TRANSLATION_MEMORY_GUARD_PROPS_INVALID')
                props = response.json()
            except (httpx.HTTPError, json.JSONDecodeError) as exc:
                raise ValueError('TRANSLATION_MEMORY_GUARD_UNAVAILABLE') from exc
            if response.status_code == 503:
                error = props.get('error') if isinstance(props, dict) else None
                loading = isinstance(props, dict) and (props.get('status') == 'loading' or (
                    isinstance(error, dict) and error.get('code') == 503
                    and error.get('message') == 'Loading model'))
                if not loading:
                    raise ValueError('TRANSLATION_MEMORY_GUARD_UNAVAILABLE')
                sleeping = False
            else:
                if not isinstance(props, dict) or type(props.get('is_sleeping')) is not bool:
                    raise ValueError('TRANSLATION_MEMORY_GUARD_PROPS_INVALID')
                sleeping = props['is_sleeping']
            now = self.clock()
            if now >= deadline:
                raise ValueError('TRANSLATION_MEMORY_GUARD_TIMEOUT')
            if sleeping:
                return round(now - started, 3)
            self.sleep(min(self.memory_guard['poll_seconds'], deadline - now))

    def translate(self, model, text):
        started = self.clock()
        if model == 'opus-mt-en-ru-int8':
            import ctranslate2
            from transformers import MarianTokenizer
            if self.opus is None:
                self.opus = ctranslate2.Translator(self.config['opus_path'], device='cpu',
                    compute_type='int8', intra_threads=2, inter_threads=1)
                self.tokenizer = MarianTokenizer.from_pretrained(self.config['opus_source'], local_files_only=True)
            tokens = self.tokenizer.convert_ids_to_tokens(self.tokenizer.encode(text))
            # Never allow CT2's default truncation to silently discard source text.
            if len(tokens) > 500:
                raise ValueError('OPUS_SOURCE_TOO_LONG')
            result = self.opus.translate_batch([tokens], beam_size=4, max_input_length=0,
                max_decoding_length=768, return_end_token=True)[0]
            hypothesis = result.hypotheses[0]
            if not hypothesis or hypothesis[-1] != self.tokenizer.eos_token:
                raise ValueError('TRANSLATION_TRUNCATED')
            output = self.tokenizer.decode(self.tokenizer.convert_tokens_to_ids(hypothesis), skip_special_tokens=True)
            metrics = {'compute_type': self.opus.compute_type, 'source_tokens': len(tokens),
                       'output_tokens': len(hypothesis)}
        else:
            route = self.config['gguf'].get(model)
            if route is None:
                raise ValueError('TRANSLATION_MODEL_UNAVAILABLE')
            if model.startswith('translategemma'):
                content = [{'type':'text', 'source_lang_code':'en', 'target_lang_code':'ru', 'text':text}]
            else:
                content = self.prompt.replace('{source_text}', text)
            body = {'model':model, 'messages':[{'role':'user','content':content}],
                    'max_tokens':1024, 'temperature':0, 'stream':False, 'cache_prompt':True}
            guarded = self.memory_guard is not None and self.memory_guard['model'] == model
            if guarded:
                peer_wait = self._wait_sleeping(self.memory_guard['peer_props_url'])
            inference_started = self.clock()
            try:
                response = self.http.post(route.rstrip('/')+'/v1/chat/completions', json=body)
                response.raise_for_status()
                data = response.json()
                choice = data['choices'][0]
                if choice['finish_reason'] != 'stop':
                    raise ValueError('TRANSLATION_TRUNCATED')
                output = choice['message']['content']
            finally:
                inference_seconds = round(self.clock() - inference_started, 3)
                if guarded:
                    own_wait = self._wait_sleeping(self.memory_guard['own_props_url'])
            metrics = {'timings':data.get('timings'), 'usage':data.get('usage')}
            if guarded:
                metrics['inference_seconds'] = inference_seconds
                metrics['memory_guard'] = {'peer_wait_seconds': peer_wait, 'own_wait_seconds': own_wait}
        if not isinstance(output, str) or not output.strip():
            raise ValueError('TRANSLATION_EMPTY')
        metrics['seconds'] = round(self.clock()-started, 3)
        return {'model':model,'text':output.strip(),'complete':True,'metrics':metrics}


class Handler(BaseHTTPRequestHandler):
    translator = None

    def log_message(self, *args):
        pass  # Do not log source articles or translated private text.

    def reply(self, code, value):
        raw = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('Content-Length',str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path == '/health':
            return self.reply(200, {'status':'ok','models':[*self.translator.config['gguf'], 'opus-mt-en-ru-int8']})
        self.reply(404, {'error':'NOT_FOUND'})

    def do_POST(self):
        if self.path != '/translate':
            return self.reply(404, {'error':'NOT_FOUND'})
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 24000:
                raise ValueError
            data = json.loads(self.rfile.read(length))
            if (not isinstance(data, dict) or set(data) != {'model','text','source','target'}
                    or data['source'] != 'en' or data['target'] != 'ru'
                    or not isinstance(data['text'],str) or not 1 <= len(data['text'].strip()) <= 1600
                    or not isinstance(data['model'],str)):
                raise ValueError
        except (ValueError, TypeError):
            return self.reply(400, {'error':'TRANSLATION_INPUT_INVALID'})
        if not self.translator.lock.acquire(blocking=False):
            return self.reply(503, {'error':'TRANSLATION_BUSY'})
        try:
            self.reply(200,self.translator.translate(data['model'],data['text']))
        except Exception:
            self.reply(502, {'error':'TRANSLATION_FAILED'})
        finally:
            self.translator.lock.release()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--port', type=int, default=8094)
    args = parser.parse_args()
    Handler.translator = Translator(json.loads(args.config.read_text()))
    ThreadingHTTPServer(('127.0.0.1',args.port),Handler).serve_forever()
