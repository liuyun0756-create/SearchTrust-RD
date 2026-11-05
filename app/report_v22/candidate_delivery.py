"""v12 selection policy. Technical omission is confirmable; dependencies are not."""

def valid_dependencies(ids, pool):
    seen = set()
    for key in ids:
        if key not in pool or key in seen or not set(pool[key].dependencies) <= seen:
            return False
        seen.add(key)
    return True


def omission_confirmation(report, ids, client_ids):
    technical = sorted(e.input.action_id for e in report.prioritization.candidates if e.technical_finding_ids)
    if technical and not set(technical).intersection(client_ids):
        return dict(confirmed=True, omitted_action_ids=technical,
                    selected_action_ids=ids, client_action_ids=client_ids)
    return None


def validate_selection(report, ids, client_ids, confirmation):
    pool = {a.id: a for a in report.action_candidates}
    visible = [key for key in ids if key in client_ids]
    if client_ids != visible or not valid_dependencies(ids, pool) or not valid_dependencies(visible, pool):
        raise ValueError("selection dependencies or visible order invalid")
    if confirmation != omission_confirmation(report, ids, visible):
        raise ValueError("technical omission requires current selection confirmation")
