"""Remove credential/session HTTP headers without touching private receipts."""
import re
SENSITIVE_HEADERS=frozenset({'authorization','proxy-authorization','cookie','set-cookie'})

def sanitize_receipt(value,*,headers=False):
    if isinstance(value,dict):
        if headers and str(value.get('name',value.get('key',''))).strip().casefold() in SENSITIVE_HEADERS:
            return None
        result={}
        for key,item in value.items():
            normalized=str(key).strip().casefold()
            if normalized in SENSITIVE_HEADERS:continue
            child=sanitize_receipt(item,headers=headers or 'header' in normalized)
            if child is not None or item is None:result[key]=child
        return result
    if isinstance(value,(list,tuple)):
        result=[]
        for item in value:
            if headers and isinstance(item,(list,tuple)) and item and str(item[0]).strip().casefold() in SENSITIVE_HEADERS:continue
            child=sanitize_receipt(item,headers=headers)
            if child is not None or item is None:result.append(child)
        return result
    if headers and isinstance(value,str):
        lines=[];redacting=False
        for line in value.splitlines():
            match=re.match(r'^([^:\s]+)\s*:',line)
            if match:redacting=match.group(1).casefold() in SENSITIVE_HEADERS
            elif line and not line[0].isspace():redacting=False
            if not redacting:lines.append(line)
        return '\n'.join(lines)
    return value
