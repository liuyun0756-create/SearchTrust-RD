# Dify migration: Entity Presence Rules 21–25

The backend now owns Rules 21–25. Dify must consume these values as facts and
must not recalculate them from page content.

## Backend input contract

Create a Dify start variable named `backend_entity_presence` with type **Text**.
The backend sends a JSON string in this shape:

```json
{
  "schema_version": "1",
  "rule_results": {
    "rule_21": false,
    "rule_22": true,
    "rule_23": false,
    "rule_24": false,
    "rule_25": false
  },
  "rule_applicability": {
    "rule_21": true,
    "rule_22": true,
    "rule_23": true,
    "rule_24": true,
    "rule_25": true
  },
  "findings": {
    "rule_25": {
      "field": "opening_hours",
      "triggered": false,
      "condition": "present",
      "page_values": ["Mon-Sat: 24-Hours Emergency Service"],
      "page_observations": [],
      "has_conflict": false
    }
  }
}
```

`true` means the absence rule triggered. `false` means the target page exposed
the field. All five applicability values are `true`.

## Zero-downtime order

1. Add `backend_entity_presence` to the Dify Start node and publish this input-only change.
2. Deploy the backend version that sends the new input.
3. Run one test and confirm the Dify execution input contains all five booleans.
4. Update the rule aggregator and report-copy prompt as described below.
5. Remove the five LLM nodes and their edges.
6. Publish the final Dify workflow and run the regression checks.

Do not deploy the backend before step 1 if the published Dify workflow rejects
undeclared inputs.

## Rule aggregator change

In `rule_aggregator`:

1. Add a Text input variable named `backend_entity_presence`, connected to the
   Start node variable with the same name.
2. Remove the five existing inputs `rule_21`, `rule_22`, `rule_23`, `rule_24`,
   and `rule_25`.
3. Remove those five parameters from `main(...)`.
4. Remove those five entries from the LLM `inputs` dictionary.
5. Parse and validate the backend vector before processing the remaining LLM
   rules.

Add this helper inside the code node:

```python
def parse_backend_presence(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("backend_entity_presence is required")
    try:
        payload = json.loads(value)
    except Exception as exc:
        raise ValueError("backend_entity_presence must be valid JSON") from exc

    results = payload.get("rule_results")
    applicability = payload.get("rule_applicability")
    expected = [f"rule_{rule_id}" for rule_id in range(21, 26)]
    if not isinstance(results, dict) or set(results) != set(expected):
        raise ValueError("backend entity-presence results must contain Rules 21-25")
    if not isinstance(applicability, dict) or set(applicability) != set(expected):
        raise ValueError("backend entity-presence applicability must contain Rules 21-25")
    if any(type(results[key]) is not bool for key in expected):
        raise ValueError("backend entity-presence results must be booleans")
    if any(type(applicability[key]) is not bool for key in expected):
        raise ValueError("backend entity-presence applicability must be booleans")
    return results, applicability
```

Add `backend_entity_presence=None` to `main(...)`, then immediately after
`hit_rules = {}` merge the authoritative values:

```python
presence_results, presence_applicability = parse_backend_presence(
    backend_entity_presence
)
for rule_key, triggered in presence_results.items():
    if presence_applicability[rule_key] and triggered:
        hit_rules[rule_key] = True
```

If the current workflow has a newer node that emits complete `rule_results`
and `rule_applicability` objects, merge all five booleans into those objects
instead of adding only triggered rules:

```python
rule_results.update(presence_results)
rule_applicability.update(presence_applicability)
```

The final rule vector must contain Rules 21–25 exactly once and their values
must come only from `backend_entity_presence`.

## Report-copy prompt change

Connect `backend_entity_presence` to the node that writes the v2.1 report copy.
Add the following instruction:

```text
ENTITY PRESENCE AUTHORITY
- Rules 21-25 are backend-owned facts in backend_entity_presence.
- Copy rule_results and rule_applicability exactly; never recalculate them.
- Use findings.rule_XX.page_values and page_observations only as raw evidence.
- A present but conflicting field is still present in L2. Do not describe it as missing.
- GBP equality is not an L2 question. Do not compare Page and GBP in Entity Presence.
- If an absence rule is false, do not list that field under What Needs Attention.
```

Also insert the variable itself into the prompt:

```text
## Backend Entity Presence
{{ backend_entity_presence }}
```

Use the variable picker in Dify rather than typing an unresolved template name.

## Delete the old nodes

Only after the aggregator and report-copy node no longer reference them, delete:

- `rule21` — missing business identity
- `rule22` — missing physical address
- `rule23` — missing phone
- `rule24` — missing service area
- `rule25` — missing opening hours

Delete their incoming and outgoing edges as well. Do not delete the rule IDs
from the Entity Presence layer mapping; the backend still supplies the five
rules and the report still assesses five signals.

## Regression checks

For every test, verify Dify's final rule vector, the L2 card, attention count,
evidence, and suggested fixes all agree.

1. Rapid Rooter fixture:
   - Rule 25 = `false`
   - L2 must not say business hours were not found
   - Page schedule conflicts may be shown as evidence, but not as L2 absence
2. Complete local-business page:
   - Rules 21–25 all `false`
   - Entity Presence has zero findings requiring attention
3. Empty fixture:
   - Rules 21–25 all `true`
   - Entity Presence has five findings requiring attention
4. Partial-address fixture such as `171 Attorney St`:
   - Rule 22 = `true` because fewer than three address components are present
5. Full-address fixture such as `171 Attorney St, New York, NY 10002`:
   - Rule 22 = `false`

Finally inspect one Dify trace and confirm there are no model calls named
`rule21` through `rule25`.
