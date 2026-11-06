"""Owner-scoped orchestration. Only lookup may call a paid provider (opt-in).

Workspace, preview and commit load frozen sources from storage. Browser commands
never contain identities, observations, reports or ledger contents.
"""
from datetime import datetime, timezone
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from app.jobs_v22.capability_inputs import generation_recipe
from app.jobs_v22.digest import canonical_json_bytes, verified_request_digest, verified_canonical_json_bytes
from app.jobs_v22.verified_input_resolver import VerifiedRpcClient
from app.preflight_v22.gbp_identity import (IdentityGbpLookup, observations_from_site_selection,
    record_from_saved_mapping)
from app.preflight_v22.gbp_identity_models import GbpIdentityRequest, GbpLookupLedger, SavedGbpRecord
from app.report_v22.models import StrictModel
from app.report_v22.public_reassessment import (IdentitySelection, validated_base, identity_workspace, reassess_public_report)
from app.report_v22.business_identity_v2 import AlignmentIdentityV2
from app.report_v22.site_business_facts import build_site_business_facts
from app.report_v22.site_business_models import SiteBusinessFactsInput
from app.report_v22.site_business_selection import select_site_business_candidates


class IdentityReviewCommand(StrictModel):
    operation: Literal['workspace', 'lookup', 'preview', 'commit']
    user_id: UUID
    case_id: UUID
    report_id: UUID
    request_id: UUID | None = None
    expected_identity_revision: int = Field(default=0, ge=0)
    expected_delivery_revision: int = Field(default=0, ge=0)
    base_checksum: str | None = Field(default=None, pattern=r'^sha256:[a-f0-9]{64}$')
    selection: IdentitySelection | None = None

    @model_validator(mode='after')
    def coherent(self):
        if self.operation in {'preview', 'commit'} and self.request_id is None:
            raise ValueError('request_id required')
        if self.operation == 'preview' and (self.selection is None or self.base_checksum is None):
            raise ValueError('selection and base_checksum required')
        if self.operation != 'preview' and self.selection is not None:
            raise ValueError('selection only accepted for preview')
        return self


class IdentityReviewRepository:
    def __init__(self, **kwargs):
        self.rpc = VerifiedRpcClient(**kwargs, max_response_bytes=30_000_000,
            prefix='V22_IDENTITY_REVIEW', total_timeout_seconds=60)

    async def call(self, operation, command, **values):
        names = {'workspace':'get_v22_identity_workspace', 'ledger':'save_v22_identity_lookup_ledger',
            'lookup':'save_v22_identity_lookup_result', 'preview':'prepare_v22_public_reassessment',
            'commit':'commit_v22_public_reassessment', 'prepared':'get_v22_public_reassessment'}
        if operation not in names:
            raise ValueError('INVALID')
        args = dict(p_user_id=str(command.user_id), p_case_id=str(command.case_id), p_report_id=str(command.report_id))
        return await self.rpc.post(names[operation], {**args, **values})


def lookup_request(base, value, *, saved_records=()):
    identity, automatic, _ = identity_workspace(base, value)
    context = value.evidence_input.context
    source = next((s for s in value.evidence_input.sources if s.kind == 'site'), None)
    facts = build_site_business_facts(SiteBusinessFactsInput(context=dict(case_id=context.case_id,
        report_type='prospect', site_url=context.site_url, evaluated_at=context.evaluated_at),
        source=source, limits=value.site_business_limits), resource_limits=True)
    selected = select_site_business_candidates(facts, AlignmentIdentityV2(identity, context.target_market), value.site_gbp_alignment_limits)
    observations = observations_from_site_selection(facts, selected)
    records = list(saved_records)
    for source in value.evidence_input.sources:
        if source.kind == 'public_gbp':
            fields = source.payload.record.fields
            def observed(name):
                f = getattr(fields, name)
                return f.value if f.state == 'observed' else None
            row = dict(business_name=observed('business_name'), website_url=observed('website_url'),
                phone=observed('phone'), address=observed('address'),
                **{k.kind:k.value for k in source.payload.record.observed_entity_keys})
            records.append(record_from_saved_mapping(row, source_ref=str(source.binding.snapshot_id),
                record_id=source.payload.request_record_id, payload_checksum=source.binding.payload_checksum,
                observed_at=source.binding.fetched_at, expires_at=source.binding.expires_at,
                kind='detail', available=source.payload.collection_status == 'succeeded'))
        elif source.kind == 'serp':
            for run in source.payload.query_runs:
                for row in run.results:
                    if row.result_type != 'maps':
                        continue
                    records.append(record_from_saved_mapping(row.model_dump(mode='json'),
                        source_ref=str(source.binding.snapshot_id), record_id=row.record_id,
                        payload_checksum=source.binding.payload_checksum,
                        observed_at=source.binding.fetched_at, expires_at=source.binding.expires_at, kind='market'))
    # Do not treat the old automatic identity as explicit human branch selection.
    return GbpIdentityRequest(case_ref=str(context.case_id), site_url=context.site_url,
        country_code=identity.country_code, target_market=context.target_market,
        evaluated_at=context.evaluated_at, saved_records=records, market_collected=True,
        phones=observations['phones'], names=observations['names'], gbp_url=str(identity.public_gbp_url) if identity.public_gbp_url else None)


class IdentityReviewService:
    def __init__(self, repository, *, lookup_factory=None, clock=None):
        self.repository = repository
        self.lookup_factory = lookup_factory or IdentityGbpLookup
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    async def run(self, command: IdentityReviewCommand):
        if command.operation == 'commit':
            if not command.request_id:
                raise ValueError('INVALID')
            return await self.repository.call('commit', command, p_request_id=str(command.request_id))
        if command.operation == 'preview' and command.request_id:
            existing = await self.repository.call('prepared', command, p_request_id=str(command.request_id))
            if existing:
                if existing['selection'] != command.selection.model_dump(mode='json'):
                    raise ValueError('CONFLICT')
                return existing
        bundle = await self.repository.call('workspace', command)
        if not bundle:
            raise ValueError('UNAVAILABLE')
        base, value = validated_base(bundle['report'], bundle['request'])
        if (base.id != command.report_id or base.input_manifest.user_id != command.user_id
                or base.input_manifest.case_id != command.case_id):
            raise ValueError('UNAVAILABLE')
        identity, automatic, notes = identity_workspace(base, value)
        records = [SavedGbpRecord.model_validate_json(canonical_json_bytes(r)) for r in bundle.get('saved_records', [])]
        # Only records observed at the saved analysis time can support offline
        # reassessment. New lookup records remain clues for a new collection.
        records = [r for r in records if r.observed_at <= value.evidence_input.context.evaluated_at]
        req = lookup_request(base, value, saved_records=records)
        ledger = GbpLookupLedger.model_validate_json(canonical_json_bytes(bundle['ledger'])) if bundle.get('ledger') else None
        lookup = self.lookup_factory() if command.operation == 'lookup' else IdentityGbpLookup()
        async def persist(current, *, reserve):
            returned = await self.repository.call('ledger', command, p_ledger=current.model_dump(mode='json'), p_reserve=reserve)
            if returned != current.model_dump(mode='json'):
                raise ValueError('LOOKUP_LEDGER_NOT_CONFIRMED')
        lookup.persist_ledger = persist
        if command.operation == 'lookup':
            req.evaluated_at = self.clock()
        result = await lookup.lookup(req, ledger=ledger)
        if command.operation == 'lookup':
            await self.repository.call('lookup', command, p_result=result.model_dump(mode='json'))
        if command.operation in {'workspace','lookup'}:
            options = {}
            reference = value.evidence_input.context.customer_public_gbp
            bound_keys = {f'{k.kind}:{k.value}' for k in reference.entity_keys} if reference else set()
            detail = next((s for s in value.evidence_input.sources if s.kind == 'public_gbp'), None)
            address_field = detail.payload.record.fields.address if detail else None
            bound_address = address_field.value if address_field and address_field.state == 'observed' else None
            for row in result.candidates:
                key = f'cid:{row.record.cid}' if row.record.cid else f'place_id:{row.record.place_id}'
                if row.eligible:
                    options[key] = dict(ref=key, name=row.record.business_name, address=bound_address if key in bound_keys else row.record.address,
                        website=row.record.website_url, phone=row.record.phone, available_for_reassessment=key in bound_keys,
                        domain_match=row.domain_match, phone_match=row.phone_match)
            return dict(schema_version='identity_review_workspace_v1', report_id=str(base.id),
                base_checksum=verified_request_digest(base.model_dump(mode='json')), identity_revision=bundle['identity_revision'],
                delivery_revision=bundle['delivery_revision'], identity=identity.model_dump(mode='json'),
                automatic=automatic.model_dump(mode='json'), notes=notes,
                branches=list(options.values()), lookup_status=result.status, lookup_reason=result.reason,
                lookup_attempts=len(result.ledger.attempts), evidence_as_of=base.input_manifest.evidence_as_of or base.input_manifest.evaluated_at,
                checks=[c.model_dump(mode='json') for c in base.contact_checks or []])
        if (not command.selection or not command.request_id or command.base_checksum != verified_request_digest(base.model_dump(mode='json'))
                or command.expected_identity_revision != bundle['identity_revision']
                or command.expected_delivery_revision != bundle['delivery_revision']):
            raise ValueError('CONFLICT')
        selected = [c for c in result.candidates if c.eligible and c.current and command.selection.branch_ref in
            {f'cid:{c.record.cid}', f'place_id:{c.record.place_id}'}]
        if not selected:
            raise ValueError('BRANCH_NOT_VERIFIED')
        now = self.clock().isoformat().replace('+00:00','Z')
        after, updated, diff = reassess_public_report(base, value, command.selection.model_dump(),
            request_id=command.request_id, evaluated_at=now)
        _, _, recipe = generation_recipe(after.input_manifest, updated)
        proof = dict(selection=command.selection.model_dump(mode='json'), report=after.model_dump(mode='json'),
            report_checksum=verified_request_digest(after.model_dump(mode='json')),
            identity=updated.business_identity.model_dump(mode='json'), recipe=recipe,
            difference={k:[c.model_dump(mode='json') for c in v] if k in {'before','after'} else v for k,v in diff.items()},
            expected_identity_revision=command.expected_identity_revision,
            expected_delivery_revision=command.expected_delivery_revision)
        return await self.repository.call('preview', command, p_request_id=str(command.request_id),
            p_preview=proof, p_canonical=verified_canonical_json_bytes(proof).decode())
