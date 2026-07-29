from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict, Field


class ContextBudgets(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    maximum_manifest_entries: int = Field(default=2000, ge=1, le=10000)
    maximum_manifest_bytes: int = Field(default=262144, ge=1024, le=4194304)
    maximum_model_steps: int = Field(default=8, ge=1, le=50)
    maximum_model_calls: int = Field(default=8, ge=1, le=50)
    maximum_repository_tool_calls: int = Field(default=6, ge=1, le=50)
    maximum_files_per_read: int = Field(default=5, ge=1, le=20)
    maximum_distinct_files: int = Field(default=16, ge=1, le=100)
    maximum_bytes_per_file: int = Field(default=65536, ge=1, le=1048576)
    maximum_total_source_bytes: int = Field(default=393216, ge=1024, le=8388608)
    maximum_issue_description_bytes: int = Field(default=32768, ge=1, le=1048576)
    maximum_prompt_bytes: int = Field(default=524288, ge=1024, le=8388608)
    maximum_output_tokens: int = Field(default=8192, ge=1, le=65536)
    maximum_output_bytes: int = Field(default=262144, ge=1024, le=1048576)
    maximum_proposal_bytes: int = Field(default=131072, ge=1024, le=1048576)
    maximum_changed_files: int = Field(default=8, ge=1, le=50)
    maximum_diff_bytes: int = Field(default=65536, ge=1, le=1048576)

    def constrained_by_policy(self, policy_context_bytes: int) -> ContextBudgets:
        return self.model_copy(
            update={
                "maximum_total_source_bytes": min(
                    self.maximum_total_source_bytes,
                    policy_context_bytes,
                ),
                "maximum_prompt_bytes": min(
                    self.maximum_prompt_bytes,
                    policy_context_bytes,
                ),
                "maximum_issue_description_bytes": min(
                    self.maximum_issue_description_bytes,
                    policy_context_bytes,
                ),
            }
        )


class BudgetExceededError(ValueError):
    pass


@dataclass(slots=True)
class BudgetLedger:
    budgets: ContextBudgets
    model_calls: int = 0
    logical_steps: int = 0
    repository_tool_calls: int = 0
    prompt_bytes: int = 0
    output_bytes: int = 0
    output_tokens: int = 0
    issue_description_bytes: int = 0
    _blob_sizes: dict[str, int] = field(default_factory=dict)

    @property
    def distinct_files(self) -> int:
        return len(self._blob_sizes)

    @property
    def total_source_bytes(self) -> int:
        return sum(self._blob_sizes.values())

    def record_model_call(self) -> None:
        self.model_calls += 1
        self._require(self.model_calls <= self.budgets.maximum_model_calls)

    def record_step(self) -> None:
        self.logical_steps += 1
        self._require(self.logical_steps <= self.budgets.maximum_model_steps)

    def record_tool_call(self) -> None:
        self.repository_tool_calls += 1
        self._require(self.repository_tool_calls <= self.budgets.maximum_repository_tool_calls)

    def record_file_read_call(self, file_count: int) -> None:
        if file_count < 1 or file_count > self.budgets.maximum_files_per_read:
            raise BudgetExceededError("file-read batch exceeds configured limit")
        self.record_tool_call()

    def record_blob(self, blob_sha: str, byte_size: int) -> bool:
        if byte_size > self.budgets.maximum_bytes_per_file:
            raise BudgetExceededError("file exceeds the configured byte limit")
        if blob_sha in self._blob_sizes:
            return False
        self._blob_sizes[blob_sha] = byte_size
        self._require(self.distinct_files <= self.budgets.maximum_distinct_files)
        self._require(self.total_source_bytes <= self.budgets.maximum_total_source_bytes)
        return True

    def record_prompt(self, byte_size: int) -> None:
        self.prompt_bytes = byte_size
        self._require(byte_size <= self.budgets.maximum_prompt_bytes)

    def record_issue_description(self, byte_size: int) -> None:
        self.issue_description_bytes = byte_size
        self._require(byte_size <= self.budgets.maximum_issue_description_bytes)

    def record_output(self, *, byte_size: int, tokens: int) -> None:
        self.output_bytes += byte_size
        self.output_tokens += tokens
        self._require(self.output_bytes <= self.budgets.maximum_output_bytes)
        self._require(self.output_tokens <= self.budgets.maximum_output_tokens)

    @staticmethod
    def _require(condition: bool) -> None:
        if not condition:
            raise BudgetExceededError("context budget exhausted")
