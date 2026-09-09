"""Synthetic dataset generator for Power BI fine-tuning."""

from __future__ import annotations

import argparse
import json
import os
import random
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence

from pydantic import BaseModel, Field

try:  # Optional dependency – only needed when actually calling OpenAI
    from openai import OpenAI
except ImportError:  # pragma: no cover - optional import
    OpenAI = None  # type: ignore

try:  # instructor enforces structured outputs
    import instructor
    from instructor import Mode
except ImportError:  # pragma: no cover - optional import
    instructor = None  # type: ignore
    Mode = None  # type: ignore

DEFAULT_SYSTEM_PROMPT = (
    "You are a senior Power BI architect. Given a natural language requirement, "
    "produce a JSON object with two keys: tmdl_schema (Power BI TMDL for the semantic model) "
    "and pbir_layout (visual layout JSON compliant with PBIR)."
)


class SyntheticArtifact(BaseModel):
    """Validated schema for each synthetic example."""

    tmdl_schema: dict = Field(..., description="Valid Power BI TMDL definition JSON")
    pbir_layout: dict = Field(..., description="Power BI PBIR visual layout JSON")


@dataclass
class DatasetRecord:
    prompt: str
    artifact: SyntheticArtifact

    def to_chatml(self, system_prompt: str) -> dict:
        assistant_content = json.dumps(
            self.artifact.model_dump(), separators=(",", ":")
        )
        return {
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": self.prompt},
                {"role": "assistant", "content": assistant_content},
            ]
        }


class DatasetGenerator:
    """Thin wrapper around an instructor client to generate structured samples."""

    def __init__(
        self, client: object, model: str, system_prompt: str = DEFAULT_SYSTEM_PROMPT
    ) -> None:
        self._client = client
        self._model = model
        self._system_prompt = system_prompt

    def generate(self, prompt: str) -> DatasetRecord:
        response = self._client.responses.create(  # type: ignore[attr-defined]
            model=self._model,
            input=_build_messages(prompt, self._system_prompt),
            response_model=SyntheticArtifact,
        )
        artifact = self._to_artifact(response)
        return DatasetRecord(prompt=prompt, artifact=artifact)

    @staticmethod
    def _to_artifact(response: object) -> SyntheticArtifact:
        if isinstance(response, SyntheticArtifact):
            return response
        if isinstance(response, dict):
            return SyntheticArtifact.model_validate(response)
        raise TypeError("Instructor client returned an unsupported payload type.")


def _build_messages(prompt: str, system_prompt: str) -> List[dict]:
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": prompt},
    ]


def synthesize_dataset(
    prompts: Sequence[str],
    client: object,
    model: str,
    train_output: Path,
    val_output: Path,
    system_prompt: str = DEFAULT_SYSTEM_PROMPT,
    val_ratio: float = 0.1,
    seed: int = 13,
) -> tuple[List[DatasetRecord], List[DatasetRecord]]:
    if not 0 < val_ratio < 1:
        raise ValueError("val_ratio must be between 0 and 1")
    generator = DatasetGenerator(
        client=client, model=model, system_prompt=system_prompt
    )
    rng = random.Random(seed)
    prompts_list = list(prompts)
    if not prompts_list:
        raise ValueError("At least one prompt is required to synthesize a dataset.")
    records: List[DatasetRecord] = []
    for prompt in prompts_list:
        record = generator.generate(prompt)
        records.append(record)
    rng.shuffle(records)
    split_idx = max(1, int(len(records) * (1 - val_ratio)))
    train_records = records[:split_idx]
    val_records = records[split_idx:]
    if not val_records:
        val_records = [train_records[-1]]
    _write_chatml_jsonl(train_records, train_output, system_prompt)
    _write_chatml_jsonl(val_records, val_output, system_prompt)
    return train_records, val_records


def _write_chatml_jsonl(
    records: Sequence[DatasetRecord], output_path: Path, system_prompt: str
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as handle:
        for record in records:
            json.dump(record.to_chatml(system_prompt), handle, ensure_ascii=False)
            handle.write("\n")


def _load_prompts(path: Optional[Path]) -> List[str]:
    if path is None:
        return _default_prompts()
    if not path.exists():
        raise FileNotFoundError(f"Prompt file '{path}' does not exist.")
    with path.open("r", encoding="utf-8") as handle:
        return [line.strip() for line in handle if line.strip()]


def _default_prompts() -> List[str]:
    return [
        "Create a sales star schema with measures for YoY growth and region filters.",
        "Model marketing campaign attribution with factCampaign, dimensionChannel, and ROI calculations.",
        "Design a finance model for profit and loss statements with departmental security roles.",
    ]


def _build_instructor_client(api_key: Optional[str], base_url: Optional[str]) -> object:
    if OpenAI is None:
        raise ImportError(
            "openai package is required to call the dataset generator client."
        )
    if instructor is None:
        raise ImportError(
            "instructor package is required to enforce JSON schema outputs."
        )
    client_kwargs = {}
    if api_key:
        client_kwargs["api_key"] = api_key
    if base_url:
        client_kwargs["base_url"] = base_url
    openai_client = OpenAI(**client_kwargs)
    return instructor.from_openai(openai_client, mode=Mode.JSON)


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate synthetic ChatML datasets for nl2pbip fine-tuning."
    )
    parser.add_argument(
        "--prompt-file",
        type=Path,
        default=None,
        help="Optional newline-delimited prompt file.",
    )
    parser.add_argument(
        "--train-output", type=Path, default=Path("finetune/train.jsonl")
    )
    parser.add_argument("--val-output", type=Path, default=Path("finetune/val.jsonl"))
    parser.add_argument("--model", default="gpt-4o-mini")
    parser.add_argument("--val-ratio", type=float, default=0.1)
    parser.add_argument("--seed", type=int, default=13)
    parser.add_argument("--api-key", default=os.getenv("OPENAI_API_KEY"))
    parser.add_argument("--base-url", default=os.getenv("OPENAI_BASE_URL"))
    parser.add_argument("--system-prompt", default=DEFAULT_SYSTEM_PROMPT)
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> None:
    args = parse_args(argv)
    prompts = _load_prompts(args.prompt_file)
    client = _build_instructor_client(api_key=args.api_key, base_url=args.base_url)
    synthesize_dataset(
        prompts=prompts,
        client=client,
        model=args.model,
        train_output=args.train_output,
        val_output=args.val_output,
        system_prompt=args.system_prompt,
        val_ratio=args.val_ratio,
        seed=args.seed,
    )


if __name__ == "__main__":  # pragma: no cover - CLI
    main()
