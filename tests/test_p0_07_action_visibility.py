import json
import pytest
from app.report_v22.capability import DeliveryRevision
from test_p0_06_finding_status import labelled_public_fixture
from test_v22_capability_contract import revision


@pytest.mark.parametrize('selection',['empty','all','missing','null','duplicate','unknown','old_revision','old_report'])
def test_revision_visibility_is_versioned_and_cannot_edit_frozen_actions(selection):
    report=labelled_public_fixture(search_gap=True)[2]
    before=report.model_dump(mode='json')
    raw=revision(report)
    raw.update(schema_version='v22_delivery_revision_v2',client_action_ids=[])
    ids=[a.id for a in report.actions]
    if selection=='all':raw['client_action_ids']=ids
    if selection=='missing':raw.pop('client_action_ids')
    if selection=='null':raw['client_action_ids']=None
    if selection=='duplicate':raw['client_action_ids']=[ids[0],ids[0]]
    if selection=='unknown':raw['client_action_ids']=['invented']
    if selection=='old_revision':raw['schema_version']='v22_delivery_revision_v1'
    if selection=='old_report':
        from app.report_v22.capability import CapabilityReport
        from app.report_v22.capability_examples import example_report
        report=CapabilityReport.model_validate_json(json.dumps(example_report()))
        raw={**revision(report),'schema_version':'v22_delivery_revision_v2','client_action_ids':[]}
    if selection in {'empty','all'}:
        saved=DeliveryRevision.model_validate_json(json.dumps(raw)).validate_report(report)
        assert saved.client_action_ids==raw['client_action_ids']
        assert report.model_dump(mode='json')==before
    else:
        with pytest.raises(ValueError): DeliveryRevision.model_validate_json(json.dumps(raw)).validate_report(report)
