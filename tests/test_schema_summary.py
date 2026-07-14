import unittest

from app.tasks.scraper import extract_schema_summary


class SchemaSummaryTests(unittest.TestCase):
    def test_extracts_types_from_json_ld_graph_and_ignores_invalid_blocks(self):
        html = """
        <script type="application/ld+json">{"@context":"https://schema.org","@graph":[{"@type":"LocalBusiness"},{"@type":["Service","Thing"]}]}</script>
        <script type="application/ld+json">not valid json</script>
        """

        self.assertEqual(extract_schema_summary(html), ["LocalBusiness", "Service", "Thing"])
