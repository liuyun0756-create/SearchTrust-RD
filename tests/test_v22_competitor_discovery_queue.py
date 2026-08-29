from uuid import UUID

from app.competitors_v22.queue import competitor_discovery_physical_job_id


def test_competitor_discovery_physical_job_id_is_isolated() -> None:
    job_id = UUID("33333333-3333-4333-8333-333333333333")

    assert competitor_discovery_physical_job_id(job_id, 2) == (
        "v22:competitor-discovery:33333333-3333-4333-8333-333333333333:run:2"
    )

