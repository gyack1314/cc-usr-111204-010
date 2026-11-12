"""把闭包清单与封存原文写入离线资料包，并提供只读本包的加载器。

资料包布局：
  <out>/manifest.json   闭包清单（身份、引用边、图、缺口）
  <out>/uri-map.json    request URI/final URI -> documents/<file> 的重定位映射
  <out>/documents/...   原始字节原样封存，$id与原始$ref不做任何语义改写
"""
import hashlib
import json
from pathlib import Path

from .io import loads_strict

MANIFEST_NAME = 'manifest.json'
URI_MAP_NAME = 'uri-map.json'
DOC_DIR = 'documents'


def write_archive(out_dir, manifest, fetched):
    """写资料包。fetched为MaterialFetcher.fetched：request URI -> 抓取记录。"""
    out = Path(out_dir)
    docs_dir = out / DOC_DIR
    docs_dir.mkdir(parents=True, exist_ok=True)
    uri_map = {}
    written = set()
    for request_uri, record in sorted(fetched.items()):
        target = docs_dir / record['file']
        # 不同request URI可能指向同一封存文件，避免同路径写入冲突
        if str(target) not in written:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(record['raw_bytes'])
            written.add(str(target))
            if hashlib.sha256(target.read_bytes()).hexdigest() != record['sha256']:
                raise IOError(f'封存字节摘要不一致: {record["file"]}')
        uri_map[request_uri] = f'{DOC_DIR}/{record["file"]}'
        uri_map[record['final_uri']] = f'{DOC_DIR}/{record["file"]}'
    manifest_path = out / MANIFEST_NAME
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=False),
        encoding='utf8')
    (out / URI_MAP_NAME).write_text(
        json.dumps(uri_map, ensure_ascii=False, indent=2, sort_keys=True),
        encoding='utf8')
    return {
        'dir': str(out),
        'manifest': str(manifest_path),
        'uri_map': str(out / URI_MAP_NAME),
        'documents': len(written),
    }


class OfflineArchive:
    """禁止网络的资料包读取器：仅解析本包文件，任何import或调用都不触网。"""

    def __init__(self, archive_dir):
        self.dir = Path(archive_dir)
        self.manifest = json.loads((self.dir / MANIFEST_NAME).read_text('utf8'))
        self.uri_map = json.loads((self.dir / URI_MAP_NAME).read_text('utf8'))

    def locate_document(self, uri):
        """URI（request或final身份）-> (相对路径, 原始字节, 摘要, 文档清单记录)。"""
        rel = self.uri_map.get(uri)
        if rel is None:
            raise KeyError(f'资料包URI映射中不存在: {uri}')
        path = self.dir / rel
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        record = next((d for d in self.manifest['documents']
                       if f'{DOC_DIR}/{d["file"]}' == rel), None)
        if record is not None and record['sha256'] != digest:
            raise IOError(f'封存文件字节摘要冲突: {rel}')
        return rel, raw, digest, record

    def document_value(self, uri):
        """返回重定位到同一目标所需的解析值；原文$id/$ref保持不变。"""
        _, raw, _, _ = self.locate_document(uri)
        return loads_strict(raw)

    def resource_uris(self):
        return [r['resource_uri'] for r in self.manifest['resources']]
