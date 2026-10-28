"""Unified API delegates each scope to its own protected evidence worker."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import app

class Worker:
    def __init__(self): self.calls=[]
    def request(self,**kwargs):
        self.calls.append(kwargs)
        return 200,[],json.dumps(dict(disease='Prostate cancer',baseline_percent=5,panels=[])).encode()

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
