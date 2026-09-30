"""Read-only release checks for the incremental reviewed evidence overlay."""
import json
from pathlib import Path
import unittest
from automation.sync_sources import load_overlay, read, sha, assembled_source
from automation.import_multicancer_review import ROOT, AUDIT, VERSION


def previous_file(source, relative):
    pointer=read(AUDIT/'previous_overlay_index.json')['sources'][source]
    manifest=read(ROOT/'updates'/pointer['manifest'])
    entry=manifest['files'].get(relative)
    return ROOT/'updates'/source/entry['stored_path'] if entry else ROOT/'sources'/source/relative


class MulticancerReleaseTests(unittest.TestCase):
    def test_original_snapshot_hashes_are_unchanged(self):
        for relative,digest in read(AUDIT/'immutable_before.json').items():
            self.assertEqual(sha(ROOT/relative),digest,relative)

    def test_overlay_checksums_and_all_historical_rows_are_preserved(self):
        for source,release in read(AUDIT/'source_release.json').items():
            current=load_overlay(ROOT,source)
            for relative,entry in release['overlay_files'].items():
                self.assertEqual(current['files'][relative],entry)
                path=ROOT/'updates'/source/entry['stored_path']
                self.assertEqual(sha(path),entry['sha256'])
                if not relative.endswith('.json'):continue
                before=read(previous_file(source,relative),{})
                after=read(path)
                for key in ('studies','results'):
                    if isinstance(before.get(key),list):
                        self.assertEqual(after[key][:len(before[key])],before[key],(source,relative))

    def test_reviewed_rows_and_holds_in_both_modes(self):
        for source in ('MetaVir','metaPee'):
            registry=read(AUDIT/source/'registry.json')
            expected={e['id']:e['row'] for e in registry['entries']}
            root=assembled_source(ROOT,source)
            for mode in ('no','yes'):
                cache=root/f'Subexposure_results/{mode}' if source=='MetaVir' else root/f'combined/provisional_ui/subexposure_views/{mode}/Cached_results'
                found=set();selected=set()
                for path in cache.glob('*/*_true_core.json'):
                    for row in read(path).get('studies',[]):
                        rid=row.get('source_review_id')
                        if rid not in expected:continue
                        found.add(rid)
                        for key in ('PMID','Direct Exposure','Effect Size','Lower CI','Upper CI','Sample Size','Cases','Quality Score','Gender','policy_poolable'):
                            self.assertEqual(row.get(key),expected[rid].get(key),(source,mode,rid,key))
                        if row.get('policy_poolable') is True and row.get('subexposure_pooling_selected') is not False and row.get('disease_hierarchy_pooling_selected') is not False:
                            selected.add(rid)
                self.assertEqual(found,set(expected))
                self.assertEqual(len(selected),3 if source=='MetaVir' else 16)

    def test_summaries_current_and_no_legacy_summary_overlay(self):
        import summary_outputs
        from automation.sync_sources import legacy_summary_artifact
        for source in ('MetaVir','metaPee'):
            for path in read(AUDIT/'source_release.json')[source]['overlay_files']:
                self.assertFalse(legacy_summary_artifact(path),path)
            for mode in ('no','yes'):
                folder=ROOT/'summary'/mode
                self.assertTrue(summary_outputs._current(folder,summary_outputs.input_fingerprint(ROOT,source,mode),source))


if __name__=='__main__':unittest.main()
