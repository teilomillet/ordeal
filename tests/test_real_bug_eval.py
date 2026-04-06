"""Evaluation cases for real-bug mode precision and harness reach."""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from pathlib import Path

import hypothesis.strategies as st

import ordeal.auto as auto_mod
from ordeal.auto import scan_module


@dataclass(frozen=True)
class EvalOutcome:
    """One compact evaluation outcome."""

    name: str
    verdict: str
    promoted: bool


def _write_real_bug_eval_project(tmp_path: Path) -> None:
    pkg = tmp_path / "evalpkg"
    tests_dir = tmp_path / "tests"
    pkg.mkdir()
    tests_dir.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "numeric.py").write_text(
        "def divide_total(total: int, count: int) -> float:\n"
        "    return total / count\n",
        encoding="utf-8",
    )
    (pkg / "shellish.py").write_text(
        "def build_copy_command(path: str) -> str:\n"
        "    return f'cp {path} /tmp'\n",
        encoding="utf-8",
    )
    (pkg / "robust.py").write_text(
        "from typing import Any\n"
        "\n"
        "def normalize_message(message: Any) -> int:\n"
        "    payload = dict(message)\n"
        "    return len(str(payload['text']))\n",
        encoding="utf-8",
    )
    (pkg / "preconditions.py").write_text(
        "def score_responses(prompts: list[str], responses: list[str]) -> list[int]:\n"
        '    """Raises ValueError when prompts and responses have different lengths."""\n'
        "    if len(prompts) != len(responses):\n"
        "        raise ValueError('prompts and responses must have the same length')\n"
        "    return [\n"
        "        len(prompt) + len(response)\n"
        "        for prompt, response in zip(prompts, responses)\n"
        "    ]\n",
        encoding="utf-8",
    )
    (pkg / "typed_api.py").write_text(
        "from dataclasses import dataclass\n"
        "from typing import Literal\n"
        "\n"
        "@dataclass\n"
        "class PolicyConfig:\n"
        "    mode: Literal['strict', 'relaxed']\n"
        "    threshold: int\n"
        "\n"
        "def score_policy(config: PolicyConfig) -> int:\n"
        "    return len(config.mode) + config.threshold\n",
        encoding="utf-8",
    )
    (pkg / "lifecycle.py").write_text(
        "class Env:\n"
        "    def __init__(self) -> None:\n"
        "        self.events: list[str] = []\n"
        "\n"
        "    async def cleanup(self) -> None:\n"
        "        self.events.append('cleanup')\n"
        "\n"
        "    async def teardown(self) -> None:\n"
        "        self.events.append('teardown')\n"
        "\n"
        "    async def rollout(self, state: dict[str, str], prompt: str) -> str:\n"
        "        self.events.append('rollout')\n"
        "        return f\"{state['seed']}:{prompt}\"\n",
        encoding="utf-8",
    )
    (tests_dir / "test_evalpkg.py").write_text(
        "from evalpkg.numeric import divide_total\n"
        "from evalpkg.preconditions import score_responses\n"
        "from evalpkg.robust import normalize_message\n"
        "from evalpkg.shellish import build_copy_command\n"
        "from evalpkg.typed_api import PolicyConfig, score_policy\n"
        "from evalpkg.lifecycle import Env\n"
        "\n"
        "def test_eval_seeds() -> None:\n"
        "    assert divide_total(10, 2) == 5\n"
        "    assert build_copy_command('demo files/input.txt').startswith('cp ')\n"
        "    assert normalize_message({'text': 'hello'}) == 5\n"
        "    assert score_responses(['a'], ['b']) == [2]\n"
        "    assert score_policy(PolicyConfig(mode='strict', threshold=2)) == 8\n",
        encoding="utf-8",
    )
    (tests_dir / "support_factories.py").write_text(
        "from evalpkg.lifecycle import Env\n"
        "\n"
        "def make_env() -> Env:\n"
        "    return Env()\n"
        "\n"
        "def make_env_state(instance: Env) -> dict[str, str]:\n"
        "    return {'seed': 'cached'}\n",
        encoding="utf-8",
    )


def test_real_bug_eval_suite_tracks_precision_and_harness_reach(tmp_path, monkeypatch):
    _write_real_bug_eval_project(tmp_path)
    auto_mod._mine_object_harness_hints.cache_clear()
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()

    numeric = next(
        item
        for item in scan_module("evalpkg.numeric", max_examples=10, mode="real_bug").functions
        if item.name == "divide_total"
    )
    shellish = next(
        item
        for item in scan_module("evalpkg.shellish", max_examples=5, mode="real_bug").functions
        if item.name == "build_copy_command"
    )
    robust = next(
        item
        for item in scan_module(
            "evalpkg.robust",
            max_examples=12,
            mode="real_bug",
            fixtures={"message": st.sampled_from([None, 1, {"text": "hello"}])},
        ).functions
        if item.name == "normalize_message"
    )
    preconditions = next(
        item
        for item in scan_module("evalpkg.preconditions", max_examples=8, mode="real_bug").functions
        if item.name == "score_responses"
    )
    typed_api = next(
        item
        for item in scan_module("evalpkg.typed_api", max_examples=8, mode="real_bug").functions
        if item.name == "score_policy"
    )

    lifecycle_mod = importlib.import_module("evalpkg.lifecycle")
    lifecycle_scan = scan_module(
        "evalpkg.lifecycle",
        max_examples=5,
        mode="real_bug",
        targets=["Env.rollout"],
        object_factories={"evalpkg.lifecycle:Env": lifecycle_mod.Env},
        object_state_factories={"evalpkg.lifecycle:Env": lambda _instance: {"seed": "cached"}},
        object_harnesses={"evalpkg.lifecycle:Env": "stateful"},
    )
    lifecycle = next(item for item in lifecycle_scan.functions if item.name == "Env.rollout")

    outcomes = [
        EvalOutcome("divide_total", numeric.verdict, numeric.promoted),
        EvalOutcome("build_copy_command", shellish.verdict, shellish.promoted),
        EvalOutcome("normalize_message", robust.verdict, robust.promoted),
        EvalOutcome("score_responses", preconditions.verdict, preconditions.promoted),
        EvalOutcome("score_policy", typed_api.verdict, typed_api.promoted),
        EvalOutcome("Env.rollout", lifecycle.verdict, lifecycle.promoted),
    ]

    promoted = {item.name for item in outcomes if item.promoted}
    expected_real_bugs = {"divide_total", "build_copy_command"}
    precision = len(promoted & expected_real_bugs) / max(1, len(promoted))

    assert promoted == expected_real_bugs
    assert precision == 1.0
    assert numeric.verdict == "promoted_real_bug"
    assert shellish.verdict == "semantic_contract"
    assert robust.verdict == "invalid_input_crash"
    assert robust.promoted is False
    assert preconditions.verdict == "expected_precondition_failure"
    assert preconditions.passed is True
    assert typed_api.verdict == "clean"
    assert typed_api.contract_violations == []
    assert lifecycle_scan.skipped == []
    assert lifecycle.verdict == "clean"
