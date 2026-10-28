"""Export internal preparation contracts; fixtures never enter runtime generation."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.jobs_v22.review_generation_stage import ReviewGenerationCheckpoint
from app.report_v22.comparison_base import StrictModel
from app.report_v22.review_generation_content import ReviewGenerationContent, build_review_generation_content
from app.report_v22.review_generation_models import ReviewGenerationInput, ReviewGenerationRecipe, ReviewGenerationSelection, recipe_for
from tests.test_v22_review_generation import generation


class Contracts(StrictModel):
    selection: ReviewGenerationSelection
    recipe: ReviewGenerationRecipe
    input: ReviewGenerationInput
    content: ReviewGenerationContent
    checkpoint: ReviewGenerationCheckpoint


def artifacts():
    value = generation()
    return {"generation.schema.json": Contracts.model_json_schema(mode="serialization"),
        "fixture.json": dict(kind="synthetic_review_generation", input=value.model_dump(mode="json"),
            recipe=recipe_for(value).model_dump(mode="json"), content=build_review_generation_content(value).model_dump(mode="json"))}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--frontend-dir", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    roots = [ROOT / "contracts/v2.2-review-generation"]
    if args.frontend_dir:
        if not (args.frontend_dir / "package.json").is_file():
            raise SystemExit("Invalid frontend root")
        roots.append(args.frontend_dir / "src/lib/review-generation/contracts")
    for root in roots:
        for name, value in artifacts().items():
            path = root / name
            data = (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()
            if args.check:
                if not path.is_file() or path.read_bytes() != data:
                    raise SystemExit("Review generation contract drift")
            else:
                root.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
    print(json.dumps(dict(ok=True, check=args.check, destinations=len(roots))))
