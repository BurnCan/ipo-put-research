"""Frozen constants for the first M9A prospective evaluation."""
from dataclasses import dataclass


@dataclass(frozen=True)
class M9AEvaluationSpec:
    evaluation_id: str
    hypothesis_id: str
    default_evaluation_mode: str
    target_group: str
    primary_outcome: str
    bearish_hit_rule: str
    minimum_matured_strict_count: int
    minimum_matured_target_count: int


M9A_PROSPECTIVE_EVAL_V1 = M9AEvaluationSpec(
    evaluation_id="m9a_prospective_eval_v1",
    hypothesis_id="m7_return20_vol20_minus5_post20",
    default_evaluation_mode="strict_prospective",
    target_group="high_high",
    primary_outcome="post_20d_return",
    bearish_hit_rule="post_20d_return < 0",
    minimum_matured_strict_count=20,
    minimum_matured_target_count=5,
)
