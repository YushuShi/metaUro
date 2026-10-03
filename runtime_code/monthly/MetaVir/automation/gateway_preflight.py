"""Check exact required model availability without leaking credential values."""
import json
import os
from pathlib import Path
from openai import OpenAI
MODELS=('gpt-5.6-luna','gpt-5.6-sol','claude-opus-5','gemini-3.7-flash','gpt-6-astra')
def required_models(config_path=None):
    path=Path(config_path or 'automation/monthly_config.json')
    config=json.loads(path.read_text()) if path.is_file() else {}
    models=config.get('gateway_models',list(MODELS))
    if (not isinstance(models,list) or not models or
            not all(isinstance(name,str) and name.strip()==name and name for name in models) or
            len(models)!=len(set(models))):
        raise RuntimeError('gateway_models must be a nonempty list of exact, unique model IDs.')
    return tuple(dict.fromkeys([*models, 'claude-opus-5', 'gemini-3.7-flash']))
def check():
    models=required_models()
    key=(os.environ.get('CORNELL_API_KEY') or '').strip()
    base=(os.environ.get('CORNELL_API_BASE_URL') or os.environ.get('API_BASE_URI') or '').strip()
    if not key or not base:raise RuntimeError('Cornell gateway credentials are missing.')
    try:
        client=OpenAI(api_key=key,base_url=base,timeout=45,max_retries=0)
        available={item.id for item in client.models.list()}
    except Exception as error:
        budget=any(word in str(error).lower() for word in ('budget','quota','credit'))
        reason='Cornell gateway budget or quota is exhausted.' if budget else 'Cornell gateway availability check failed.'
        raise RuntimeError(f'{reason} HTTP {getattr(error,"status_code","unavailable")}.') from None
    missing=[name for name in models if name not in available]
    if missing:raise RuntimeError('Requested models unavailable: '+', '.join(missing))
    print(json.dumps({'models':{name:'available' for name in models}}))
if __name__=='__main__':
    try:check()
    except RuntimeError as error:
        print(str(error));raise SystemExit(1)
