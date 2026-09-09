"""Unsloth-based fine-tuning pipeline for nl2pbip."""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Dict, Optional, Sequence

from datasets import load_dataset
from transformers import TrainingArguments
from trl import SFTTrainer

try:  # Optional dependency, heavy import
    from unsloth import FastLanguageModel
    from unsloth.chat_templates import get_chat_template
except ImportError as exc:  # pragma: no cover - makes module importable without unsloth
    FastLanguageModel = None  # type: ignore
    get_chat_template = None  # type: ignore
    _UNSLOTH_IMPORT_ERROR = exc
else:
    _UNSLOTH_IMPORT_ERROR = None

MAX_SEQ_LENGTH = 4096
DEFAULT_MODEL_NAME = "unsloth/Qwen2.5-Coder-7B-Instruct"


def _ensure_unsloth() -> None:
    if FastLanguageModel is None or get_chat_template is None:
        raise ImportError("unsloth must be installed to run fine-tuning") from _UNSLOTH_IMPORT_ERROR


def _prepare_model(model_name: str = DEFAULT_MODEL_NAME):
    _ensure_unsloth()
    model, tokenizer = FastLanguageModel.from_pretrained(
        model_name=model_name,
        max_seq_length=MAX_SEQ_LENGTH,
        dtype="bfloat16",
        load_in_4bit=True,
    )
    model = FastLanguageModel.get_peft_model(
        model,
        r=64,
        lora_alpha=16,
        lora_dropout=0.05,
        target_modules=None,
        bias="none",
        use_gradient_checkpointing=True,
    )
    return model, tokenizer


def _format_example(example: Dict, template) -> str:
    messages = example.get("messages")
    if not messages:
        raise ValueError("Each dataset row must contain a 'messages' list in ChatML format.")
    return template.apply_chat_template(messages, tokenize=False, add_generation_prompt=False)


def export_to_gguf(model, tokenizer, output_dir: Path, quantization: str = "q4_k_m") -> Path:
    _ensure_unsloth()
    exporter = getattr(FastLanguageModel, "export_gguf", None)
    if exporter is None:
        raise RuntimeError("Installed unsloth version does not support GGUF export.")
    gguf_dir = output_dir / "gguf"
    gguf_dir.mkdir(parents=True, exist_ok=True)
    exporter(model=model, tokenizer=tokenizer, save_path=str(gguf_dir), quantization=quantization)
    return gguf_dir


def train(
    train_file: Path,
    val_file: Path,
    output_dir: Path,
    model_name: str = DEFAULT_MODEL_NAME,
    learning_rate: float = 2e-4,
    num_train_epochs: float = 2.0,
    per_device_train_batch_size: int = 1,
    gradient_accumulation_steps: int = 4,
    logging_steps: int = 10,
    save_steps: int = 200,
    eval_steps: int = 50,
    max_steps: Optional[int] = None,
    export_gguf_flag: bool = False,
    gguf_quantization: str = "q4_k_m",
) -> Dict[str, Path]:
    _ensure_unsloth()
    output_dir.mkdir(parents=True, exist_ok=True)
    data_files = {"train": str(train_file)}
    if val_file.exists():
        data_files["validation"] = str(val_file)
    dataset_dict = load_dataset("json", data_files=data_files)
    train_dataset = dataset_dict["train"]
    eval_dataset = dataset_dict.get("validation")
    model, tokenizer = _prepare_model(model_name=model_name)
    chat_template = get_chat_template("chatml")

    def formatting_func(example):
        return _format_example(example, chat_template)

    training_args = TrainingArguments(
        output_dir=str(output_dir),
        per_device_train_batch_size=per_device_train_batch_size,
        gradient_accumulation_steps=gradient_accumulation_steps,
        learning_rate=learning_rate,
        num_train_epochs=num_train_epochs,
        logging_steps=logging_steps,
        save_steps=save_steps,
        evaluation_strategy="steps" if eval_dataset is not None else "no",
        eval_steps=eval_steps,
        save_total_limit=2,
        bf16=True,
        max_steps=max_steps if max_steps is not None else -1,
        report_to="none",
    )

    trainer = SFTTrainer(
        model=model,
        tokenizer=tokenizer,
        train_dataset=train_dataset,
        eval_dataset=eval_dataset,
        formatting_func=formatting_func,
        max_seq_length=MAX_SEQ_LENGTH,
        packing=False,
        args=training_args,
        train_on_responses_only=True,
    )
    trainer.train()

    adapter_dir = output_dir / "adapter"
    adapter_dir.mkdir(parents=True, exist_ok=True)
    trainer.model.save_pretrained(adapter_dir)
    tokenizer.save_pretrained(adapter_dir)

    artifacts: Dict[str, Path] = {"adapter_dir": adapter_dir}
    if export_gguf_flag:
        artifacts["gguf_dir"] = export_to_gguf(trainer.model, tokenizer, output_dir, quantization=gguf_quantization)
    return artifacts


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fine-tune nl2pbip using Unsloth + QLoRA.")
    parser.add_argument("--train-file", type=Path, default=Path("finetune/train.jsonl"))
    parser.add_argument("--val-file", type=Path, default=Path("finetune/val.jsonl"))
    parser.add_argument("--output-dir", type=Path, default=Path("finetune/output"))
    parser.add_argument("--model-name", default=DEFAULT_MODEL_NAME)
    parser.add_argument("--learning-rate", type=float, default=2e-4)
    parser.add_argument("--epochs", type=float, default=2.0)
    parser.add_argument("--batch-size", type=int, default=1)
    parser.add_argument("--gradient-accumulation", type=int, default=4)
    parser.add_argument("--logging-steps", type=int, default=10)
    parser.add_argument("--save-steps", type=int, default=200)
    parser.add_argument("--eval-steps", type=int, default=50)
    parser.add_argument("--max-steps", type=int, default=None)
    parser.add_argument("--export-gguf", action="store_true")
    parser.add_argument("--gguf-quantization", default="q4_k_m")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> None:
    args = parse_args(argv)
    train(
        train_file=args.train_file,
        val_file=args.val_file,
        output_dir=args.output_dir,
        model_name=args.model_name,
        learning_rate=args.learning_rate,
        num_train_epochs=args.epochs,
        per_device_train_batch_size=args.batch_size,
        gradient_accumulation_steps=args.gradient_accumulation,
        logging_steps=args.logging_steps,
        save_steps=args.save_steps,
        eval_steps=args.eval_steps,
        max_steps=args.max_steps,
        export_gguf_flag=args.export_gguf,
        gguf_quantization=args.gguf_quantization,
    )


if __name__ == "__main__":  # pragma: no cover - CLI
    main()
