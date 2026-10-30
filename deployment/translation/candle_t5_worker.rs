//! Изолированный CPU JSONL worker для MADLAD-400: модель загружается один раз.
//!
//! Основа: официальный Candle quantized-t5, upstream commit
//! 12bb5d1f6350f4ea9827d9086f213feec1f0af6a (workspace version 0.11.0):
//! https://github.com/huggingface/candle/blob/12bb5d1f6350f4ea9827d9086f213feec1f0af6a/candle-examples/examples/quantized-t5/main.rs
//!
//! Зависимости существующего candle-examples: candle, candle-transformers,
//! tokenizers, serde (derive), serde_json, anyhow, clap (derive).
//! В ИЗОЛИРОВАННОМ checkout Candle добавьте в candle-examples/Cargo.toml:
//! [[example]]
//! name = "max-postiz-candle-t5"
//! path = "/absolute/path/to/candle_t5_worker.rs"
//! Сборка: cargo build --locked --release -p candle-examples
//!          --example max-postiz-candle-t5 --no-default-features
//! Для полностью offline сборки добавьте --offline при заполненном Cargo cache.
//!
//! CLI: --model /local/model-q4k.gguf --tokenizer /local/tokenizer.json
//!      --config /local/config.json
//! stdin:  {"text":"English source", "max_new_tokens":1024}
//! stdout: ровно один JSON-ответ на строку; диагностические события — stderr.
//! source_tokens включает языковой префикс и специальные токены tokenizer.
//! token_ids/output_tokens включают sampled EOS, но не decoder_start_token.

use std::io::{self, BufRead, Write};
use std::path::PathBuf;
use std::time::Instant;

use anyhow::Result;
use candle::{Device, Tensor};
use candle_transformers::generation::LogitsProcessor;
use candle_transformers::models::quantized_t5 as t5;
use clap::Parser;
use serde::{Deserialize, Serialize};
use tokenizers::Tokenizer;

const MAX_INPUT_BYTES: usize = 128 * 1024;
const MAX_NEW_TOKENS: usize = 4096;

#[derive(Parser)]
struct Args {
    #[arg(long)]
    model: PathBuf,
    #[arg(long)]
    tokenizer: PathBuf,
    #[arg(long)]
    config: PathBuf,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Request {
    text: String,
    max_new_tokens: usize,
}

#[derive(Serialize)]
struct Response {
    text: String,
    complete: bool,
    finish_reason: &'static str,
    source_tokens: usize,
    output_tokens: usize,
    token_ids: Vec<u32>,
    load_seconds: f64,
    inference_seconds: f64,
    cold_start: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    error: Option<&'static str>,
}

impl Response {
    fn new(load_seconds: f64) -> Self {
        Self {
            text: String::new(),
            complete: false,
            finish_reason: "error",
            source_tokens: 0,
            output_tokens: 0,
            token_ids: Vec::new(),
            load_seconds,
            inference_seconds: 0.0,
            cold_start: false,
            error: None,
        }
    }
}

struct Worker {
    model: t5::T5ForConditionalGeneration,
    tokenizer: Tokenizer,
    config: t5::Config,
    load_seconds: f64,
    used: bool,
}

impl Worker {
    fn load(args: Args) -> Result<Self> {
        let started = Instant::now();
        let mut config: t5::Config = serde_json::from_slice(&std::fs::read(args.config)?)?;
        config.use_cache = true;
        let mut tokenizer = Tokenizer::from_file(args.tokenizer).map_err(anyhow::Error::msg)?;
        tokenizer.with_padding(None);
        tokenizer.with_truncation(None).map_err(anyhow::Error::msg)?;
        // Только локальные файлы и CPU; Hub/API и загрузчиков здесь нет.
        let weights = t5::VarBuilder::from_gguf(args.model, &Device::Cpu)?;
        let model = t5::T5ForConditionalGeneration::load(weights, &config)?;
        Ok(Self {
            model,
            tokenizer,
            config,
            load_seconds: started.elapsed().as_secs_f64(),
            used: false,
        })
    }

    fn generate(&mut self, request: Request, response: &mut Response) -> Result<()> {
        self.model.clear_kv_cache();
        let input = self
            .tokenizer
            .encode(format!("<2ru> {}", request.text), true)
            .map_err(anyhow::Error::msg)?;
        response.source_tokens = input.len();
        let input_ids = Tensor::new(input.get_ids(), &Device::Cpu)?.unsqueeze(0)?;
        let encoder = self.model.encode(&input_ids)?;
        let mut next_input = self
            .config
            .decoder_start_token_id
            .unwrap_or(self.config.pad_token_id) as u32;
        // None temperature — greedy. При penalty=1.0 менять logits не нужно.
        let mut logits = LogitsProcessor::new(42, None, None);
        response.finish_reason = "length";
        for _ in 0..request.max_new_tokens {
            let decoder_ids = Tensor::new(&[next_input], &Device::Cpu)?.unsqueeze(0)?;
            let prediction = self.model.decode(&decoder_ids, &encoder)?.squeeze(0)?;
            let token = logits.sample(&prediction)?;
            response.token_ids.push(token);
            if token as usize == self.config.eos_token_id {
                response.complete = true;
                response.finish_reason = "EOS";
                break;
            }
            next_input = token;
        }
        Ok(())
    }

    fn process(&mut self, line: &str) -> Response {
        let mut response = Response::new(self.load_seconds);
        let request = serde_json::from_str::<Request>(line);
        let request = match request {
            Ok(value)
                if !value.text.trim().is_empty()
                    && value.text.len() <= MAX_INPUT_BYTES
                    && (1..=MAX_NEW_TOKENS).contains(&value.max_new_tokens) => value,
            _ => {
                response.error = Some("INVALID_REQUEST");
                return response;
            }
        };
        // Прогрев отделён от загрузки: первая реальная попытка использует холодную модель.
        response.cold_start = !self.used;
        self.used = true;
        let started = Instant::now();
        if self.generate(request, &mut response).is_err() {
            response.complete = false;
            response.finish_reason = "error";
            response.error = Some("INFERENCE_FAILED");
        }
        // Полный decode сохраняет Unicode/byte fallback. У MADLAD EOS в JSON
        // tokenizer не отмечен special: управляющий финальный ID не является
        // текстом. Полный sampled ряд, включая EOS, остаётся в evidence.
        let visible_ids = if response.complete {
            &response.token_ids[..response.token_ids.len() - 1]
        } else {
            response.token_ids.as_slice()
        };
        match self.tokenizer.decode(visible_ids, true) {
            Ok(text) => response.text = text,
            Err(_) => {
                response.complete = false;
                response.finish_reason = "error";
                response.error = Some("DECODE_FAILED");
            }
        }
        response.output_tokens = response.token_ids.len();
        response.inference_seconds = started.elapsed().as_secs_f64();
        self.model.clear_kv_cache();
        response
    }
}

fn main() {
    let args = Args::parse();
    let mut worker = match Worker::load(args) {
        Ok(worker) => worker,
        Err(_) => {
            // Не печатаем OS/model ошибки: они могут содержать локальные пути.
            eprintln!("{{\"event\":\"error\",\"error\":\"MODEL_LOAD_FAILED\"}}");
            std::process::exit(1);
        }
    };
    eprintln!(
        "{{\"event\":\"loaded\",\"load_seconds\":{}}}",
        worker.load_seconds
    );
    let input = io::stdin();
    let output = io::stdout();
    let mut output = output.lock();
    for line in input.lock().lines() {
        let line = match line {
            Ok(line) => line,
            Err(_) => {
                eprintln!("{{\"event\":\"error\",\"error\":\"INPUT_IO_FAILED\"}}");
                std::process::exit(1);
            }
        };
        let response = worker.process(&line);
        if serde_json::to_writer(&mut output, &response).is_err()
            || output.write_all(b"\n").is_err()
            || output.flush().is_err()
        {
            eprintln!("{{\"event\":\"error\",\"error\":\"OUTPUT_IO_FAILED\"}}");
            std::process::exit(1);
        }
    }
}
