"""Whitelisted JSON-LD declarations; no remote contexts, identity selection or IO."""
import json
from urllib.parse import urljoin, urlsplit

from app.jobs_v22.digest import canonical_json_bytes, request_digest
from app.report_v22.site_business_bindings import in_scope
from app.report_v22.site_business_errors import require
from app.report_v22.site_business_html import diagnostic, origin, valid_text
from app.report_v22.site_business_models import CandidateDraft, COMPONENTS

BUSINESS_TYPES = frozenset(("LocalBusiness", "Organization", "Plumber", "HomeAndConstructionBusiness", "ProfessionalService",
                            "Electrician", "HVACBusiness", "RoofingContractor", "GeneralContractor", "Locksmith", "MovingCompany"))
CONTEXTS = frozenset(("http://schema.org", "https://schema.org", "http://schema.org/", "https://schema.org/"))


class JsonIssue(ValueError):
    pass


def decode_json(raw, limits, counter):
    """Bound depth/tokens before the standard decoder allocates nested values."""
    require(len(raw.encode("utf-8")) <= limits.max_jsonld_bytes, "LIMIT_EXCEEDED")
    depth, string, escape, token = 0, False, False, False
    for char in raw:
        if string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                string = False
            continue
        if char.isspace() or char in ",:":
            token = False
        elif char in "[{":
            depth += 1
            require(depth <= limits.max_json_depth, "LIMIT_EXCEEDED")
            counter[0] += 1
            token = False
        elif char in "]}":
            depth -= 1
            token = False
        elif char == '"':
            string = True
            counter[0] += 1
            token = False
        elif not token:
            counter[0] += 1
            token = True
        require(counter[0] <= limits.max_json_nodes_per_page, "LIMIT_EXCEEDED")

    def object_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise JsonIssue("duplicate_json_key")
            result[key] = value
        return result

    def reject_constant(value):
        raise JsonIssue("jsonld_parse_failed")

    try:
        value = json.loads(raw, object_pairs_hook=object_pairs, parse_constant=reject_constant)
        # The decoder may overflow a syntactically valid exponent to infinity.
        from app.report_v22.evidence_models import reject_nonfinite
        reject_nonfinite(value)
        # Invalid Unicode scalars and numbers outside the canonical encoder's
        # domain are local block errors, not failures of the caller's binding.
        canonical_json_bytes(value)
        return value
    except JsonIssue:
        raise
    except (ValueError, TypeError, RecursionError):
        raise JsonIssue("jsonld_parse_failed") from None


def supported_context(value):
    if isinstance(value, str):
        return value in CONTEXTS
    return isinstance(value, list) and all(isinstance(item, str) and item in CONTEXTS for item in value)


def records(value, pointer=""):
    if isinstance(value, list):
        for index, item in enumerate(value):
            if isinstance(item, dict):
                yield from records(item, f"{pointer}/{index}")
    elif isinstance(value, dict):
        yield pointer, value
        graph = value.get("@graph")
        if isinstance(graph, list):
            yield from records(graph, pointer + "/@graph")


def supported_container(value):
    if isinstance(value, list):
        return all(isinstance(item, dict) and supported_container(item) for item in value)
    return isinstance(value, dict) and ("@graph" not in value or value["@graph"] is None
                                       or isinstance(value["@graph"], list) and supported_container(value["@graph"]))


def business(record):
    kinds = record.get("@type", [])
    kinds = [kinds] if isinstance(kinds, str) else kinds
    if not isinstance(kinds, list):
        return False
    accepted = BUSINESS_TYPES | {f"{prefix}{kind}" for prefix in ("https://schema.org/", "http://schema.org/") for kind in BUSINESS_TYPES}
    return any(isinstance(kind, str) and kind in accepted for kind in kinds)


def jsonld_candidates(tree, requested_url, final_url, scope):
    drafts, diagnostics, documents = [], [], {}
    counter = [0]
    limits = tree.budget.limits
    for block in tree.json_blocks:
        raw = tree.text[block.content_start:block.content_end]
        require(len(raw.encode("utf-8")) <= limits.max_jsonld_bytes, "LIMIT_EXCEEDED")
        if not block.closed or block.bad:
            diagnostics.append(diagnostic(tree, "jsonld_parse_failed", node=block))
            continue
        try:
            document = decode_json(raw, limits, counter)
        except JsonIssue as exc:
            diagnostics.append(diagnostic(tree, str(exc), node=block))
            continue
        if not supported_container(document):
            diagnostics.append(diagnostic(tree, "unsupported_field_shape", node=block))
            continue
        all_records = list(records(document))
        if any("@context" in record and not supported_context(record["@context"]) for _, record in all_records):
            diagnostics.append(diagnostic(tree, "unsupported_context", node=block))
            continue
        documents[(block.content_start, block.content_end)] = document
        for record_pointer, record in all_records:
            if not business(record):
                continue
            entity_key = request_digest({"requested_url": str(requested_url), "final_url": str(final_url), "record": record})
            hints, owner, notes = {}, "declared_entity", []
            for property_name, output in (("@id", "declared_id"), ("url", "declared_url")):
                value = record.get(property_name)
                if value is None:
                    hints[output] = None
                elif not isinstance(value, str) or not value.strip() or len(value) > 2083:
                    hints[output], owner = None, "unresolved"
                    diagnostics.append(diagnostic(tree, "invalid_field_value", [], block, pointer=record_pointer + "/" + property_name, scope="ownership"))
                else:
                    hints[output] = value
                    try:
                        absolute = urljoin(str(final_url), value)
                        if urlsplit(absolute).scheme not in {"http", "https"} or not in_scope(absolute, scope):
                            owner = "unresolved"
                            notes.append("Website candidate diagnostic: external_entity_hint.")
                            diagnostics.append(diagnostic(tree, "external_entity_hint", [], block, pointer=record_pointer + "/" + property_name, scope="ownership"))
                    except ValueError:
                        owner = "unresolved"
                        diagnostics.append(diagnostic(tree, "ownership_unresolved", [], block, scope="ownership"))

            def diag(field, value, pointer):
                if isinstance(value, list):
                    require(len(value) <= limits.max_field_values, "LIMIT_EXCEEDED")
                code = "invalid_field_value" if isinstance(value, str) else "unsupported_field_shape"
                diagnostics.append(diagnostic(tree, code, [field], block, pointer=pointer))

            def emit(field, value, pointer, *, components=None, component_paths=None):
                if components is None:
                    if not valid_text(value, field):
                        diag(field, value, pointer)
                        return
                    origins = [origin(tree, block, pointer=pointer, record_pointer=record_pointer)]
                else:
                    origins = [origin(tree, block, pointer=component_paths[key], record_pointer=record_pointer, component=key) for key in sorted(components)]
                drafts.append(CandidateDraft(field=field, source_kind="jsonld_property", entity_key=entity_key,
                    ownership_status=owner, scalar_value=value, components=components or {}, origins=origins, limitations=notes, **hints))
                require(len(drafts) <= limits.max_candidates_per_page, "LIMIT_EXCEEDED")

            def values(value, pointer):
                if isinstance(value, list):
                    require(len(value) <= limits.max_field_values, "LIMIT_EXCEEDED")
                    return [(item, f"{pointer}/{i}") for i, item in enumerate(value) if item is not None and item != []]
                return [] if value is None else [(value, pointer)]

            name = record.get("name")
            if isinstance(name, list):
                require(len(name) <= limits.max_field_values, "LIMIT_EXCEEDED")
            if name is not None and name != []:
                emit("business_name", name, record_pointer + "/name")
            phones = values(record.get("telephone"), record_pointer + "/telephone")
            for point, point_path in values(record.get("contactPoint"), record_pointer + "/contactPoint"):
                if isinstance(point, dict):
                    phones.extend(values(point.get("telephone"), point_path + "/telephone"))
                else:
                    diag("phone", point, point_path)
            for value, path in phones:
                emit("phone", value, path)
            for value, path in values(record.get("address"), record_pointer + "/address"):
                if not isinstance(value, dict):
                    emit("address", value, path)
                    continue
                components, pointers = {}, {}
                for key in COMPONENTS:
                    part, pointer = value.get(key), path + "/" + key
                    if key == "addressCountry" and isinstance(part, dict):
                        part, pointer = part.get("name"), pointer + "/name"
                    if part is None or part == []:
                        continue
                    if valid_text(part, "address", key):
                        components[key], pointers[key] = part, pointer
                    else:
                        diag("address", part, pointer)
                if components:
                    emit("address", None, path, components=components, component_paths=pointers)
                else:
                    diag("address", value, path)
            for property_name in ("areaServed", "serviceArea"):
                for value, path in values(record.get(property_name), record_pointer + "/" + property_name):
                    if isinstance(value, dict):
                        if "name" not in value:
                            diag("service_area", value, path)
                            continue
                        value, path = value["name"], path + "/name"
                    if value is not None and value != []:
                        emit("service_area", value, path)
    return drafts, diagnostics, documents
