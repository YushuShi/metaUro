"""Unified planning API over the two protected native source workers."""
import json
from urllib.parse import urlencode
from flask import jsonify, render_template, request
from sample_size_planning import DEFAULTS


def install_app(app, namespace):
    def call(source, path, values):
        status,headers,body=namespace['workers'][source].request(
            path=path+'?'+urlencode(values),method='GET',data=b'',headers={})
        payload=json.loads(body)
        if status>=400: raise ValueError(payload.get('error','The selected evidence view is unavailable.'))
        return payload

    def selected_scope():
        choices=namespace['scope_catalog']()
        wanted=request.args.get('disease') or request.args.get('scope') or choices[0]['id']
        selected=next((item for item in choices if wanted in {item['id'],item['label']}),None)
        if selected is None: raise ValueError('Choose an available disease.')
        return selected

    @app.get('/sample-size-summary')
    def sample_size_summary():
        try: selected=selected_scope()
        except ValueError: selected=namespace['scope_catalog']()[0]
        context=namespace['context'](selected['source'],'sample-size-summary')
        context['current']=selected
        return render_template('sample_size_summary.html',sample_size_title='MetaUro',
            sample_size_styles=['/static/theme.css','/static/metauro.css'],
            sample_size_header='_site_header.html',**context)

    @app.get('/api/sample-size-summary/catalog')
    def sample_size_summary_catalog():
        try:
            choices=namespace['scope_catalog']();diseases=[]
            for source in namespace['SOURCES']:
                payload=call(source,'/api/sample-size-summary/catalog',dict(request.args))
                for item in payload['diseases']:
                    selected=next((scope for scope in choices if scope['source']==source and scope['label'].casefold()==item['label'].casefold()),None)
                    if selected is not None:
                        diseases.append(dict(value=selected['id'],label=selected['label'],source=selected['source'],baseline_percent=item['baseline_percent']))
            return jsonify(diseases=diseases,defaults=DEFAULTS)
        except (ValueError,RuntimeError,TimeoutError,OSError) as exc:
            return jsonify(error=str(exc)),503

    @app.get('/api/sample-size-summary')
    def sample_size_summary_data():
        try:
            selected=selected_scope()
            values=dict(request.args);values['disease']=selected['label']
            values.pop('scope',None)
            payload=call(selected['source'],'/api/sample-size-summary',values)
            payload['disease']=selected['id']
            return jsonify(payload)
        except ValueError as exc: return jsonify(error=str(exc)),400
        except (RuntimeError,TimeoutError,OSError) as exc: return jsonify(error=str(exc)),503
