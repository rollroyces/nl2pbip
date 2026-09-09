from __future__ import annotations

import json
from pathlib import Path
from typing import List
from unittest.mock import MagicMock, patch

from nl2pbip.finetune import train as train_module
from nl2pbip.finetune.dataset_generator import synthesize_dataset
from nl2pbip.providers.local_finetuned import LocalFineTunedProvider


def _build_dummy_client() -> object:
    class _Responses:
        def __init__(self) -> None:
            self.requests = []

        def create(self, *, model, input, response_model):
            self.requests.append({"model": model, "input": input})
            return response_model(tmdl_schema={"tables": []}, pbir_layout={"pages": []})

    class _Client:
        def __init__(self) -> None:
            self.responses = _Responses()

    return _Client()


def test_synthesize_dataset_persists_chatml(tmp_path: Path) -> None:
    client = _build_dummy_client()
    prompts = ["Design a revenue model", "Build an inventory dashboard"]
    train_path = tmp_path / "train.jsonl"
    val_path = tmp_path / "val.jsonl"

    train_records, val_records = synthesize_dataset(
        prompts=prompts,
        client=client,
        model="dummy",
        train_output=train_path,
        val_output=val_path,
        val_ratio=0.5,
        seed=1,
    )

    assert train_records
    assert val_records
    with train_path.open() as handle:
        rows = [json.loads(line) for line in handle]
    assert rows[0]["messages"][0]["role"] == "system"
    assert json.loads(rows[0]["messages"][2]["content"]) == {
        "tmdl_schema": {"tables": []},
        "pbir_layout": {"pages": []},
    }


def test_local_finetuned_provider_parses_payload() -> None:
    provider = LocalFineTunedProvider(
        model="local-model", base_url="http://localhost:9999"
    )
    fake_response = MagicMock()
    fake_response.json.return_value = {
        "choices": [{"message": {"content": '{"plan": []}'}}]
    }
    fake_response.raise_for_status.return_value = None
    provider._session.post = MagicMock(return_value=fake_response)  # type: ignore[attr-defined]

    content = provider.generate([{"role": "user", "content": "Hi"}])

    provider._session.post.assert_called_once()
    assert content == '{"plan": []}'


@patch("nl2pbip.finetune.train.export_to_gguf")
@patch("nl2pbip.finetune.train.SFTTrainer")
@patch("nl2pbip.finetune.train.get_chat_template")
@patch("nl2pbip.finetune.train.FastLanguageModel")
@patch("nl2pbip.finetune.train.TrainingArguments")
@patch("nl2pbip.finetune.train.load_dataset")
def test_train_invokes_unsloth_pipeline(
    mock_load_dataset,
    mock_training_args,
    mock_fast_lm,
    mock_get_template,
    mock_trainer_cls,
    mock_export,
    tmp_path,
) -> None:
    mock_dataset = {
        "train": [{"messages": [{"role": "user", "content": "hi"}]}],
        "validation": [{"messages": [{"role": "user", "content": "hi"}]}],
    }
    mock_load_dataset.return_value = mock_dataset

    mock_model = MagicMock(name="model")
    mock_tokenizer = MagicMock(name="tokenizer")
    mock_fast_lm.from_pretrained.return_value = (mock_model, mock_tokenizer)
    mock_fast_lm.get_peft_model.return_value = mock_model

    mock_template = MagicMock()
    mock_template.apply_chat_template.return_value = "<formatted>"
    mock_get_template.return_value = mock_template

    trainer_instance = MagicMock()
    trainer_instance.model = mock_model
    mock_trainer_cls.return_value = trainer_instance

    mock_export.return_value = Path("/tmp/gguf")

    output_dir = tmp_path / "artifacts"
    artifacts = train_module.train(
        train_file=Path("train.jsonl"),
        val_file=Path("val.jsonl"),
        output_dir=output_dir,
        export_gguf_flag=True,
    )

    mock_fast_lm.from_pretrained.assert_called_once()
    # TrainingArguments must have been constructed with bf16 enabled
    # (the train module sets bf16=True in the call).
    mock_training_args.assert_called_once()
    train_kwargs = mock_training_args.call_args.kwargs
    assert train_kwargs.get("bf16") is True
    assert train_kwargs.get("eval_strategy") == "steps"
    # The trainer should have been constructed with train_on_responses_only
    # and the mocked TrainingArguments instance.
    mock_trainer_cls.assert_called_once()
    trainer_kwargs = mock_trainer_cls.call_args.kwargs
    assert trainer_kwargs["train_on_responses_only"] is True
    assert trainer_kwargs["args"] is mock_training_args.return_value
    trainer_instance.train.assert_called_once()
    mock_model.save_pretrained.assert_called()
    mock_tokenizer.save_pretrained.assert_called()
    mock_export.assert_called_once()
    assert "adapter_dir" in artifacts
    assert "gguf_dir" in artifacts
