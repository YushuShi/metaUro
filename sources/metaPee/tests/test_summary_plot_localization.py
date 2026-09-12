import csv
import inspect
import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import app as web_app
from scripts import build_summary_plots
from scripts.plot_localization import (
    category_label,
    disease_label,
    exposure_label,
    supported_languages,
    text as plot_text,
    validate_localizations,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


class SummaryPlotLocalizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        manifest_path = REPO_ROOT / "Plot" / "summary_manifest.json"
        cls.manifest = (
            json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest_path.is_file()
            else {"schema_version": 2, "languages": list(supported_languages(REPO_ROOT)), "scopes": {}}
        )
        cls.inventory = build_summary_plots.inventory(REPO_ROOT / "Cached_results")
        cls.exposures = {
            exposure for caches in cls.inventory.values() for _, exposure in caches
        }
        with (REPO_ROOT / "bladder_kidney_cancer_lifetime_incidence_aliases.csv").open(
            encoding="utf-8-sig", newline=""
        ) as handle:
            cls.diseases = [row["cancer_name"] for row in csv.DictReader(handle)]

    def test_every_visible_plot_value_has_a_translation(self):
        categories = {
            build_summary_plots.exposure_category(exposure)
            for exposure in self.exposures
        }
        validate_localizations(
            REPO_ROOT,
            exposures=self.exposures,
            diseases=self.diseases,
            categories=categories,
        )
        for locale in supported_languages(REPO_ROOT):
            self.assertTrue(plot_text("effect_size", locale))
            for disease in self.diseases:
                self.assertTrue(disease_label(disease, locale))
            self.assertTrue(category_label("Other", locale))
            if locale != "en":
                self.assertTrue(exposure_label(REPO_ROOT, "vitamin_d", locale))
                self.assertTrue(exposure_label(REPO_ROOT, "vitamin_k1", locale))

    def test_inventory_omits_zero_inclusion_authoritative_caches(self):
        with tempfile.TemporaryDirectory() as temporary:
            cache_path = (
                Path(temporary)
                / "new_atomic_exposure"
                / "bladder_cancer_incidence_true_all.json"
            )
            cache_path.parent.mkdir(parents=True)
            cache_path.write_text(
                json.dumps(
                    {
                        "cancer": "Bladder cancer",
                        "studies": [],
                        "authoritative_revision": {
                            "zero_inclusion_cache_created": True,
                        },
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(build_summary_plots.inventory(Path(temporary)), {})

    def test_heterogeneity_plot_uses_direct_colored_exposure_labels(self):
        source = inspect.getsource(build_summary_plots.heterogeneity_plot)
        self.assertIn("ax.annotate", source)
        self.assertIn("exposure_label", source)
        self.assertIn('color=label["color"]', source)
        self.assertIn('textcoords="offset points"', source)
        self.assertIn("_place_close_labels", source)
        self.assertNotIn("arrowprops", source)
        self.assertNotIn('textcoords="axes fraction"', source)
        self.assertNotIn('label=f"{number}.', source)

    def test_close_label_placement_keeps_every_label_beside_its_marker(self):
        import math
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from matplotlib.figure import Figure

        marker_size = 81.0
        marker_radius = math.sqrt(marker_size) / 2.0
        candidates = build_summary_plots._close_label_candidates(
            (100.0, 100.0), 60.0, 12.0, marker_size, 100.0
        )
        self.assertTrue(candidates)
        for candidate in candidates:
            self.assertGreaterEqual(math.hypot(candidate[0], candidate[1]), marker_radius)
            self.assertLessEqual(math.hypot(candidate[0], candidate[1]), marker_radius + 3.0)

        figure = Figure(figsize=(4, 3), dpi=100)
        FigureCanvasAgg(figure)
        axis = figure.add_subplot()
        axis.set(xlim=(0, 1), ylim=(0, 1))
        labels = []
        for x, y, name in ((0.5, 0.5, "Vitamin A"), (0.5, 0.5, "Vitamin D"),
                           (0.5, 0.5, "Lycopene"), (0.985, 0.985, "Alpha-linolenic acid")):
            annotation = axis.annotate(name, (x, y), xytext=(0, 0), textcoords="offset points")
            labels.append({"x": x, "y": y, "marker_size": marker_size, "annotation": annotation})
        build_summary_plots._place_close_labels(figure, axis, labels)
        self.assertEqual(len(axis.texts), len(labels))
        for label in labels:
            annotation = label["annotation"]
            self.assertEqual(annotation.xy, (label["x"], label["y"]))
            self.assertTrue(annotation.get_visible())
            self.assertIsNone(annotation.arrow_patch)
            distance = math.hypot(*annotation.get_position())
            self.assertGreaterEqual(distance, marker_radius)
            self.assertLessEqual(distance, marker_radius + 3.0)
        figure.clear()

    def test_new_exposures_have_intentional_summary_categories(self):
        expected = {
            "avocado": "Fruits & Vegetables",
            "sesame": "Fruits & Vegetables",
            "fo_ti": "Herbal & Botanical",
            "oleic_acid": "Fatty Acids & Lipids",
            "omega_9_fatty_acids": "Fatty Acids & Lipids",
        }
        self.assertEqual(
            {slug: build_summary_plots.exposure_category(slug) for slug in expected},
            expected,
        )

    def test_vitamin_k_children_share_the_parent_vitamin_summary_group(self):
        for slug in ("vitamin_k", "vitamin_k1", "vitamin_k2", "vitamin_k3"):
            self.assertEqual(
                build_summary_plots.exposure_category(slug),
                "Vitamins A, C, D, E, K",
            )

    def test_forest_marks_intervals_that_continue_beyond_the_axis(self):
        axis = Mock()
        truncated = build_summary_plots._draw_bounded_interval(
            axis,
            0.01,
            50.0,
            1.0,
            0.05,
            20.0,
            color="#123456",
            linewidth=1.5,
        )
        self.assertTrue(truncated)
        self.assertEqual(axis.plot.call_args.args[0], [0.05, 20.0])
        self.assertEqual(
            {call.kwargs["marker"] for call in axis.scatter.call_args_list},
            {"<", ">"},
        )

    def test_summary_retains_a_valid_estimate_with_fewer_than_50_cases(self):
        payload = {'studies': [{
            'Study': 'Small valid stratum',
            'Effect Type': 'RR',
            'Effect Size': 1.1,
            'Lower CI': 1.01,
            'Upper CI': 1.2,
            'Cases': 46,
            'Sample Size': 100,
            'Quality Score': 'Good',
        }]}
        with tempfile.TemporaryDirectory() as temporary:
            cache_path = Path(temporary) / 'cache.json'
            cache_path.write_text(json.dumps(payload), encoding='utf-8')
            row = build_summary_plots.summarize_cache(
                cache_path, 'Bladder cancer', 'test_exposure', False
            )

        self.assertIsNotNone(row)
        self.assertEqual(row['number estimates'], 1)
        self.assertEqual(row['total Cases'], 46)

    def test_combined_summaries_match_every_saved_cache_headline(self):
        for disease, caches in self.inventory.items():
            for cache_path, exposure in caches:
                with self.subTest(disease=disease, exposure=exposure):
                    payload = json.loads(cache_path.read_text(encoding='utf-8'))
                    row = build_summary_plots.summarize_cache(
                        cache_path, disease, exposure, False
                    )
                    if payload.get("studies") and all(
                        study.get("pool_in_parent_meta_analysis") is False
                        for study in payload["studies"]
                    ):
                        self.assertIsNone(row)
                        self.assertIsNone(payload.get("headline"))
                        continue
                    self.assertIsNotNone(row)
                    headline = payload['headline']
                    for row_key, headline_key in (
                        ('Pooled RR', 'pooled_es'),
                        ('lower CI RR', 'ci_low'),
                        ('upper CI RR', 'ci_upp'),
                    ):
                        self.assertTrue(math.isclose(
                            float(row[row_key]),
                            float(headline[headline_key]),
                            rel_tol=1e-10,
                            abs_tol=1e-12,
                        ))

    def test_zero_i2_is_retained_in_heterogeneity_plot(self):
        rows = [
            {
                'Exposure': 'alcohol',
                'number estimates': 3,
                'Pooled RR': 0.9,
                'I^2 (%)': 0.0,
                'category': 'Other',
            },
            {
                'Exposure': 'tea',
                'number estimates': 4,
                'Pooled RR': 1.1,
                'I^2 (%)': 25.0,
                'category': 'Other',
            },
        ]
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'heterogeneity.pdf'
            count = build_summary_plots.heterogeneity_plot(
                output, rows, 'Bladder cancer', 'effect'
            )
            self.assertTrue(output.is_file())
        self.assertEqual(count, 2)

    def test_log_ticks_use_plain_numbers_for_cjk_font_compatibility(self):
        self.assertEqual(build_summary_plots._plain_log_tick(0.1, 0), "0.1")
        self.assertEqual(build_summary_plots._plain_log_tick(1.0, 0), "1")

    def test_manifest_declares_every_generated_language_variant(self):
        expected_languages = list(supported_languages(REPO_ROOT))
        self.assertEqual(self.manifest["schema_version"], 2)
        self.assertEqual(self.manifest["languages"], expected_languages)
        for scope in self.manifest["scopes"].values():
            for plot in scope["plots"].values():
                if not plot["available"]:
                    continue
                self.assertEqual(set(plot["locales"]), set(expected_languages))
                for relative_path in plot["locales"].values():
                    self.assertTrue((REPO_ROOT / "Plot" / relative_path).is_file())

    def test_summary_route_serves_localized_pdf_and_disables_cache(self):
        with tempfile.TemporaryDirectory() as temporary:
            plot_root = Path(temporary)
            english_path = plot_root / "forest_protective_bladder_cancer.pdf"
            chinese_path = plot_root / "locales" / "zh-CN" / english_path.name
            chinese_path.parent.mkdir(parents=True)
            english_path.write_bytes(b"english-pdf")
            chinese_path.write_bytes(b"chinese-pdf")
            manifest = {
                "scopes": {
                    "bladder_cancer": {
                        "plots": {
                            "forest-protective": {
                                "available": True,
                                "filename": english_path.name,
                                "locales": {"zh-CN": str(chinese_path.relative_to(plot_root))},
                            }
                        }
                    }
                }
            }
            client = web_app.app.test_client()
            with patch.object(web_app, "PLOT_DIR", str(plot_root)), patch.object(
                web_app, "load_summary_manifest", return_value=manifest
            ):
                english = client.get("/summary/plots/bladder_cancer/forest-protective")
                chinese = client.get(
                    "/summary/plots/bladder_cancer/forest-protective?lang=zh-CN"
                )
                self.addCleanup(english.close)
                self.addCleanup(chinese.close)
                self.assertEqual(english.status_code, 200)
                self.assertEqual(chinese.status_code, 200)
                self.assertNotEqual(english.data, chinese.data)
                self.assertIn("no-store", chinese.headers["Cache-Control"])

                manifest["scopes"]["bladder_cancer"]["plots"][
                    "forest-protective"
                ]["locales"].pop("zh-CN")
                stale_locale = client.get(
                    "/summary/plots/bladder_cancer/forest-protective?lang=zh-CN"
                )
                self.addCleanup(stale_locale.close)
                self.assertEqual(english.data, stale_locale.data)

    def test_summary_markup_and_javascript_switch_embedded_pdfs(self):
        page = (REPO_ROOT / "templates" / "summary.html").read_text(encoding="utf-8")
        javascript = (REPO_ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
        self.assertIn("data-localized-plot", page)
        self.assertIn("updateLocalizedPlotUrls", javascript)
        self.assertIn("plot_reload", javascript)

    def test_summary_figure_ui_text_is_translated_in_every_locale(self):
        catalog = json.loads(
            (REPO_ROOT / "static" / "i18n-translations.json").read_text(
                encoding="utf-8"
            )
        )
        locales = set(supported_languages(REPO_ROOT)) - {"en"}
        for source in (
            "Effect size vs heterogeneity",
            "Egger's test vs heterogeneity",
            "Open full-size plot ↗",
            "PDF preview is unavailable.",
        ):
            self.assertEqual(set(catalog[source]), locales)
            self.assertTrue(all(catalog[source].values()))


if __name__ == "__main__":
    unittest.main()
