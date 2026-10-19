"""Adapt the model's own template to llama.cpp's string-content init probes.

Run with PYTHONPATH=/opt/llama.cpp/gguf-py; never replace the translation instruction.
This endpoint is deliberately English to Russian only.
"""
import argparse
import hashlib
from pathlib import Path

from gguf import GGUFReader

PREFIX = """{%- set normalized = namespace(messages_list=[]) -%}
{%- for item in messages -%}
  {%- if item['role'] == 'user' and item['content'] is string -%}
    {%- set normalized.messages_list = normalized.messages_list + [{'role':'user', 'content':[
      {'type':'text', 'source_lang_code':'en', 'target_lang_code':'ru', 'text':item['content']}]}] -%}
  {%- elif item['role'] == 'user' and item['content'] | length == 1
      and item['content'][0]['source_lang_code'] is not defined -%}
    {%- set normalized.messages_list = normalized.messages_list + [{'role':'user', 'content':[
      {'type':'text', 'source_lang_code':'en', 'target_lang_code':'ru', 'text':item['content'][0]['text']}]}] -%}
  {%- else -%}
    {%- set normalized.messages_list = normalized.messages_list + [item] -%}
  {%- endif -%}
{%- endfor -%}
{%- set messages = normalized.messages_list -%}
"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('model', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    original = GGUFReader(str(args.model)).fields['tokenizer.chat_template'].contents()
    if 'source_lang_code' not in original or 'target_lang_code' not in original:
        raise ValueError('Expected TranslateGemma language-aware template')
    args.output.write_text(PREFIX + original, encoding='utf-8')
    print('original_template_sha256=' + hashlib.sha256(original.encode()).hexdigest())
    print('adapter_sha256=' + hashlib.sha256(args.output.read_bytes()).hexdigest())


if __name__ == '__main__':
    main()
