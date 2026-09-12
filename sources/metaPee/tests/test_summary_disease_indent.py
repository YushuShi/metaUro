"""Disease hierarchy indentation in the actual summary selector."""
import unittest
from html.parser import HTMLParser
from unittest.mock import patch
import app

class Options(HTMLParser):
    def __init__(self):
        super().__init__();self.options=[];self.current=None
    def handle_starttag(self,tag,attrs):
        if tag=='option':self.current={'attrs':dict(attrs),'text':''};self.options.append(self.current)
    def handle_data(self,data):
        if self.current is not None:self.current['text']+=data
    def handle_endtag(self,tag):
        if tag=='option':self.current=None

class SummaryIndentTests(unittest.TestCase):
    def test_summary_children_and_grandchildren_keep_their_depth(self):
        scopes=app.get_scope_config()
        manifest={'scopes':{key:{} for key in scopes}}
        with patch.object(app,'load_summary_manifest',return_value=manifest):
            response=app.app.test_client().get('/summary')
        self.assertEqual(response.status_code,200)
        options=Options();options.feed(response.get_data(as_text=True))
        by_key={row['attrs']['value']:row for row in options.options if row['attrs']['value'] in scopes}
        self.assertEqual(set(by_key),set(scopes))
        for key,scope in scopes.items():
            depth=scope.get('hierarchy_depth',0);row=by_key[key]
            self.assertEqual(row['text'],'\u00a0\u00a0'*depth+scope['disease'])
            self.assertEqual(row['attrs']['data-hierarchy-depth'],str(depth))
        self.assertEqual(scopes['clear_cell_renal_cell_carcinoma']['hierarchy_depth'],2)

if __name__=='__main__':unittest.main()
