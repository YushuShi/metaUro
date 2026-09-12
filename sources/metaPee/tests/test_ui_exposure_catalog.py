import json
import unittest
from pathlib import Path

import app as web_app


ROOT = Path(__file__).resolve().parents[1]


def hierarchy_parents(packet):
    return {
        str(parent).strip()
        for parents in packet.values()
        for parent in (parents if isinstance(parents, list) else [parents])
        if str(parent).strip()
    }


class UiExposureCatalogTests(unittest.TestCase):
    def setUp(self):
        web_app.app.config["TESTING"] = True
        self.client = web_app.app.test_client()
        self.current = json.loads(
            (ROOT / "static" / "exposures.json").read_text(encoding="utf-8")
        )
        self.additional = json.loads(
            (ROOT / "data" / "additionalExposure.json").read_text(encoding="utf-8")
        )
        self.additional_v2 = json.loads(
            (ROOT / "data" / "additionalExposureV2.json").read_text(encoding="utf-8")
        )
        self.hierarchy = json.loads(
            (ROOT / "data" / "exposure_hierarchy.json").read_text(encoding="utf-8")
        )

    def test_api_exposes_collision_deduped_current_additional_union(self):
        response = self.client.get("/api/exposures")
        self.assertEqual(response.status_code, 200)
        catalog = response.get_json()
        normalized = [name.strip().casefold() for name in catalog]

        self.assertEqual(len(normalized), len(set(normalized)))
        self.assertTrue(
            {name.casefold() for name in self.current}.issubset(set(normalized))
        )
        self.assertTrue(
            {name.casefold() for name in self.additional}.issubset(set(normalized))
        )
        self.assertTrue(
            {name.casefold() for name in self.additional_v2}.issubset(set(normalized))
        )
        self.assertTrue(
            {name.casefold() for name in hierarchy_parents(self.hierarchy)}.issubset(
                set(normalized)
            )
        )
        self.assertIn("PQQ", catalog)
        self.assertIn("DASH diet", catalog)
        self.assertNotIn("Diet pills", catalog)

        script = (ROOT / "static" / "script.js").read_text(encoding="utf-8")
        self.assertIn("fetch('/api/exposures')", script)
        self.assertNotIn("fetch('/static/exposures.json')", script)

    def test_additional_aliases_are_displayable_without_collision_inflation(self):
        response = self.client.get("/api/synonyms")
        self.assertEqual(response.status_code, 200)
        synonyms = response.get_json()
        self.assertIsInstance(synonyms["PQQ"]["core"], str)
        self.assertIn("methoxatin", synonyms["PQQ"]["core"].casefold())
        self.assertIn(
            "dietary approaches to stop hypertension",
            synonyms["DASH diet"]["core"].casefold(),
        )
        self.assertIsInstance(synonyms["Vitamin K3"]["core"], str)
        self.assertIn(
            "2-methyl-1,4-naphthoquinone",
            synonyms["Vitamin K3"]["core"],
        )

        current_synonyms = json.loads(
            (ROOT / "data" / "synonyms_cache.json").read_text(encoding="utf-8")
        )
        current_by_key = {name.casefold(): name for name in self.current}
        synonym_by_key = {name.casefold(): name for name in synonyms}
        for additional_name in (*self.additional, *self.additional_v2):
            key = additional_name.casefold()
            if key not in current_by_key:
                continue
            current_name = current_by_key[key]
            current_synonym_key = next(
                name for name in current_synonyms if name.casefold() == key
            )
            self.assertEqual(
                synonyms[synonym_by_key[key]],
                current_synonyms[current_synonym_key],
                current_name,
            )


if __name__ == "__main__":
    unittest.main()
