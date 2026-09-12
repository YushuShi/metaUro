import hashlib
import inspect
import json
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
    def setUp(self):
        # These fixtures describe the direct combined manifest, independently
        # of the UI's default subexposure mode.
        for name, value in (
            ('active_summary_manifest', str(REPO_ROOT / 'Plot' / 'summary_manifest.json')),
            ('active_plot_dir', str(REPO_ROOT / 'Plot')),
            ('active_cache_dir', str(REPO_ROOT / 'combined')),
        ):
            patcher = patch.object(web_app, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    @classmethod
    def setUpClass(cls):
        cls.manifest = json.loads(
            (REPO_ROOT / "Plot" / "summary_manifest.json").read_text(encoding="utf-8")
        )
        cls.inventory = build_summary_plots.inventory(REPO_ROOT / "combined")
        cls.exposures = {
            exposure for caches in cls.inventory.values() for _, exposure in caches
        }

    def test_every_visible_plot_value_has_a_translation(self):
        categories = {
            build_summary_plots.exposure_category(exposure)
            for exposure in self.exposures
        }
        validate_localizations(
            REPO_ROOT,
            exposures=self.exposures,
            diseases=self.inventory,
            categories=categories,
        )
        for locale in supported_languages(REPO_ROOT):
            self.assertTrue(plot_text("effect_size", locale))
            self.assertTrue(disease_label("Prostate cancer", locale))
            self.assertTrue(category_label("Other", locale))
            self.assertTrue(exposure_label(REPO_ROOT, "vitamin_d", locale))

    def test_inventory_omits_zero_inclusion_authoritative_caches(self):
        with tempfile.TemporaryDirectory() as temporary:
            cache_path = (
                Path(temporary)
                / "new_atomic_exposure"
                / "prostate_cancer_incidence_true_all.json"
            )
            cache_path.parent.mkdir(parents=True)
            cache_path.write_text(
                json.dumps(
                    {
                        "cancer": "Prostate cancer",
                        "studies": [],
                        "revision_updates": [
                            {"mode": "all_history_authoritative"}
                        ],
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(build_summary_plots.inventory(Path(temporary)), {})

    def test_vitamin_k_children_share_the_vitamin_summary_group(self):
        expected = {"vitamin_k", "vitamin_k1", "vitamin_k2", "vitamin_k3"}
        self.assertTrue(
            expected <= build_summary_plots.CATEGORY_MEMBERS["Vitamins A, C, D, E, K"]
        )

    def test_new_exposure_subtypes_use_specific_summary_groups(self):
        expected = {
            "zeaxanthin": "Carotenoids",
            "alpha_linolenic_acid": "Fatty Acids & Lipids",
            "docosahexaenoic_acid": "Fatty Acids & Lipids",
            "eicosapentaenoic_acid": "Fatty Acids & Lipids",
            "monounsaturated_fat_mufa": "Fatty Acids & Lipids",
            "polyunsaturated_fat_pufa": "Fatty Acids & Lipids",
            "genistein": "Phytoestrogens",
            "ginkgo_biloba": "Herbal & Botanical",
            "animal_protein_intake": "Metabolites & Amino Acids",
            "l_tyrosine": "Metabolites & Amino Acids",
            "l_valine": "Metabolites & Amino Acids",
        }
        self.assertEqual(
            {slug: build_summary_plots.exposure_category(slug) for slug in expected},
            expected,
        )

    def test_english_plot_labels_use_canonical_ui_spelling(self):
        expected = {
            "alpha_linolenic_acid": "Alpha-linolenic acid",
            "l_tyrosine": "L-tyrosine",
            "monounsaturated_fat_mufa": "Monounsaturated Fat (MUFA)",
            "polyunsaturated_fat_pufa": "Polyunsaturated Fat (PUFA)",
        }
        self.assertEqual(
            {
                slug: exposure_label(REPO_ROOT, slug, "en")
                for slug in expected
            },
            expected,
        )

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

    def test_zero_heterogeneity_estimates_remain_in_diagnostic_plot(self):
        rows = [
            {
                "Exposure": "lycopene",
                "number studies": 3,
                "Pooled RR": 0.9,
                "I^2 (%)": 0.0,
                "category": "Carotenoids",
            },
            {
                "Exposure": "vitamin_d",
                "number studies": 4,
                "Pooled RR": 1.1,
                "I^2 (%)": 0.0,
                "category": "Vitamins A, C, D, E, K",
            },
        ]
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "zero-heterogeneity.pdf"
            count = build_summary_plots.heterogeneity_plot(
                output,
                rows,
                "Prostate cancer",
                "effect",
            )
            self.assertEqual(count, 2)
            self.assertTrue(output.is_file())

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

    def test_log_ticks_use_plain_numbers_for_cjk_font_compatibility(self):
        self.assertEqual(build_summary_plots._plain_log_tick(0.1, 0), "0.1")
        self.assertEqual(build_summary_plots._plain_log_tick(1.0, 0), "1")

    def test_manifest_declares_every_generated_language_variant(self):
        expected_languages = list(supported_languages(REPO_ROOT))
        self.assertEqual(self.manifest["schema_version"], 2)
        combined_manifest_path = REPO_ROOT / "combined" / "_merge_manifest.json"
        combined_manifest = json.loads(combined_manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(self.manifest["canonical_track"], "combined")
        self.assertEqual(
            self.manifest["combined_build_id"], combined_manifest["build_id"]
        )
        self.assertEqual(
            self.manifest["combined_manifest_sha256"],
            hashlib.sha256(combined_manifest_path.read_bytes()).hexdigest(),
        )
        self.assertEqual(self.manifest["languages"], expected_languages)
        for scope in self.manifest["scopes"].values():
            for plot in scope["plots"].values():
                if not plot["available"]:
                    continue
                self.assertEqual(set(plot["locales"]), set(expected_languages))
                for relative_path in plot["locales"].values():
                    self.assertTrue((REPO_ROOT / "Plot" / relative_path).is_file())

    def test_summary_route_serves_localized_pdf_and_disables_cache(self):
        client = web_app.app.test_client()
        english = client.get(
            "/summary/plots/prostate_cancer/forest-protective"
        )
        chinese = client.get(
            "/summary/plots/prostate_cancer/forest-protective?lang=zh-CN"
        )
        self.addCleanup(english.close)
        self.addCleanup(chinese.close)
        self.assertEqual(english.status_code, 200)
        self.assertEqual(chinese.status_code, 200)
        self.assertNotEqual(english.data, chinese.data)
        self.assertIn("no-store", chinese.headers["Cache-Control"])

        manifest_without_chinese = json.loads(json.dumps(self.manifest))
        manifest_without_chinese["scopes"]["prostate_cancer"]["plots"][
            "forest-protective"
        ]["locales"].pop("zh-CN")
        with patch.object(
            web_app,
            "load_summary_manifest",
            return_value=manifest_without_chinese,
        ):
            stale_locale = client.get(
                "/summary/plots/prostate_cancer/forest-protective?lang=zh-CN"
            )
        self.addCleanup(stale_locale.close)
        self.assertEqual(english.data, stale_locale.data)

    def test_every_manifest_plot_locale_route_serves_declared_pdf(self):
        client = web_app.app.test_client()
        plot_root = REPO_ROOT / "Plot"
        for scope_key, scope in self.manifest["scopes"].items():
            for plot_name, plot in scope["plots"].items():
                for locale, relative_path in plot["locales"].items():
                    with self.subTest(
                        scope=scope_key,
                        plot=plot_name,
                        locale=locale,
                    ):
                        query = {} if locale == "en" else {"lang": locale}
                        response = client.get(
                            f"/summary/plots/{scope_key}/{plot_name}",
                            query_string=query,
                        )
                        self.addCleanup(response.close)
                        self.assertEqual(response.status_code, 200)
                        self.assertEqual(response.mimetype, "application/pdf")
                        self.assertEqual(
                            response.data,
                            (plot_root / relative_path).read_bytes(),
                        )
                        self.assertIn("no-store", response.headers["Cache-Control"])

    def test_ui_and_plot_builder_support_the_same_languages(self):
        self.assertEqual(
            web_app.SUMMARY_PLOT_LANGUAGES,
            set(supported_languages(REPO_ROOT)) - {"en"},
        )

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
