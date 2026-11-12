"""清点远端JSON文档，不解析Schema引用。"""
from .io import unique,asset
import json

def summarize(v,root):
    unique(v['documents'],'request_uri');docs=[]
    for d in v['documents']:
        raw,digest=asset(root,d['file']);value=json.loads(raw)
        if not isinstance(value,(dict,bool)):raise ValueError('文档根不是Schema资料')
        docs.append({'request_uri':d['request_uri'],'final_uri':d['final_uri'],'bytes':len(raw),'sha256':digest})
    return {'entry_uri':v['entry_uri'],'documents':docs}
