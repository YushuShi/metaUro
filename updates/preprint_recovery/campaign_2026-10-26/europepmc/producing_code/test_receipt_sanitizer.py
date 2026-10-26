import copy
from receipt_sanitizer import sanitize_receipt

def test_all_nested_header_dictionary_names_and_case_are_sanitized():
    raw={'headers':{'Set-Cookie':'private-session','Content-Type':'application/json'},
         'response':{'Response_Headers':{'AUTHORIZATION':'private-bearer','ETag':'saved'}},
         'request_headers':{'Cookie':'private-session','Proxy-Authorization':'private-proxy'},
         'model_dispatch_authorized':True}
    before=copy.deepcopy(raw);safe=sanitize_receipt(raw)
    assert safe['headers']=={'Content-Type':'application/json'}
    assert safe['response']['Response_Headers']=={'ETag':'saved'}
    assert safe['request_headers']=={} and safe['model_dispatch_authorized'] is True
    assert raw==before

def test_list_pair_and_name_value_header_encodings():
    raw={'httpHeaders':[['Set-Cookie','private'],['Content-Type','xml'],{'name':'Authorization','value':'private'},{'name':'Etag','value':'stable'}]}
    assert sanitize_receipt(raw)=={'httpHeaders':[['Content-Type','xml'],{'name':'Etag','value':'stable'}]}

def test_raw_http_header_blocks_remove_sensitive_folded_lines():
    raw={'headers':'HTTP/1.1 200 OK\nSet-Cookie: private\n continued-private\nETag: stable\nCookie: private\nContent-Type: json'}
    assert sanitize_receipt(raw)['headers']=='HTTP/1.1 200 OK\nETag: stable\nContent-Type: json'
