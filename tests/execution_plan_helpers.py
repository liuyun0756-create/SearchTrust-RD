from uuid import UUID

from app.jobs_v22.digest import request_digest
from app.report_v22.execution_plan_models import ExecutionPlanBuildInput
from app.report_v22.version_diff import build_version_diff
from version_diff_helpers import version_diff_request


VERIFIED_REPORT_ID = UUID("50000000-0000-4000-8000-000000000074")


def execution_plan_request(**verified_kwargs):
    diff_input = version_diff_request(**verified_kwargs)
    diff_result = build_version_diff(diff_input)
    return ExecutionPlanBuildInput(
        case_id=diff_input.case_id,
        parent_report_id=diff_input.parent_report_id,
        verified_report_id=VERIFIED_REPORT_ID,
        evaluated_at=diff_input.evaluated_at,
        planning_date=diff_input.verified_reprioritization_input.planning_date,
        verified_reprioritization_input=diff_input.verified_reprioritization_input,
        verified_reprioritization_input_checksum=request_digest(
            diff_input.verified_reprioritization_input
        ),
        verified_reprioritization_result=diff_input.verified_reprioritization_result,
        verified_reprioritization_result_checksum=request_digest(
            diff_input.verified_reprioritization_result
        ),
        version_diff_input=diff_input,
        version_diff_input_checksum=request_digest(diff_input),
        version_diff_result=diff_result,
        version_diff_result_checksum=request_digest(diff_result),
    )
