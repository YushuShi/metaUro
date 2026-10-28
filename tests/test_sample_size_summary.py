"""Unified API delegates each scope to its own protected evidence worker."""
import json
from pathlib import Path
import sys
import unittest
import importlib.util
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import app

class Worker:
    def __init__(self): self.calls=[];self.panels=[]
    def request(self,**kwargs):
        self.calls.append(kwargs)
        return 200,[],json.dumps(dict(disease='Prostate cancer',baseline_percent=5,panels=self.panels)).encode()

class SampleSizeSummaryTests(unittest.TestCase):
    def test_scope_is_forwarded_to_matching_native_worker(self):
        urinary,prostate=Worker(),Worker()
        with patch.dict(app.workers,metaPee=urinary,MetaVir=prostate):
            response=app.app.test_client().get('/api/sample-size-summary?disease=prostate_cancer&baseline_percent=7&include_subexposures=no')
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json['disease'],'prostate_cancer')
        self.assertFalse(urinary.calls)
        self.assertIn('disease=Prostate+cancer',prostate.calls[0]['path'])
        self.assertIn('baseline_percent=7',prostate.calls[0]['path'])
        self.assertIn('include_subexposures=no',prostate.calls[0]['path'])
    def test_unknown_scope_is_rejected_without_worker_request(self):
        urinary,prostate=Worker(),Worker()
        with patch.dict(app.workers,metaPee=urinary,MetaVir=prostate):
            response=app.app.test_client().get('/api/sample-size-summary?disease=missing')
        self.assertEqual(response.status_code,400)
        self.assertFalse(urinary.calls or prostate.calls)
    def test_native_colors_match_each_immutable_source_summary(self):
        from runtime_code.sample_size_summary_native import _point_style
        from matplotlib.colors import to_hex
        root=Path(__file__).resolve().parents[1]
        for source in ['MetaVir','metaPee']:
            path=root/'sources'/source/'scripts/diagnostic_style.py'
            spec=importlib.util.spec_from_file_location('summary_style_'+source,path)
            renderer=importlib.util.module_from_spec(spec);spec.loader.exec_module(renderer)
            for exposure in ['Vitamin D','Vitamin B12','Olive oil intake']:
                color=to_hex(renderer.exposure_color(exposure))
                self.assertEqual(_point_style(exposure,root),dict(label_color=color,dot_color=color))
                self.assertEqual(_point_style(exposure,root,True),dict(label_color='#000080',dot_color=color))
    def test_native_color_metadata_is_forwarded_without_changing_calculations(self):
        urinary,prostate=Worker(),Worker()
        point=dict(exposure='Vitamin D',effect_size=.8,total_n=1000,study_count=3,
                   label_color='#000080',dot_color='#763fac')
        prostate.panels=[dict(measurement='all',design='prospective',points=[point])]
        with patch.dict(app.workers,metaPee=urinary,MetaVir=prostate):
            response=app.app.test_client().get('/api/sample-size-summary?disease=prostate_cancer')
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json['panels'],prostate.panels)
