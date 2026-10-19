"""Loopback-only translation worker. No publishing or arbitrary model downloads."""
import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx


class Translator:
    def __init__(self, config):
        self.config = config
        self.lock = threading.Lock()
        self.http = httpx.Client(timeout=600, trust_env=False, follow_redirects=False)
        self.opus = self.tokenizer = None
        self.prompt = Path(config['prompt_path']).read_text(encoding='utf-8')

    def translate(self, model, text):
        started = time.monotonic()
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
            response = self.http.post(route.rstrip('/')+'/v1/chat/completions', json=body)
            response.raise_for_status()
            data = response.json()
            choice = data['choices'][0]
            if choice['finish_reason'] != 'stop':
                raise ValueError('TRANSLATION_TRUNCATED')
            output = choice['message']['content']
            metrics = {'timings':data.get('timings'), 'usage':data.get('usage')}
        if not isinstance(output, str) or not output.strip():
            raise ValueError('TRANSLATION_EMPTY')
        metrics['seconds'] = round(time.monotonic()-started, 3)
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
