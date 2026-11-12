"""禁止网络的本地重放：仅用资料包封存原文重新定位每个引用目标。

重放不重跑抓取，也不做实例校验；它用与收集阶段相同的URI/Pointer/锚语义，
从封存字节重新解析manifest中的每条边，并与收集时记录的目标逐一比对，
证明同一闭包在离线、URI映射下能重新定位到同一目标。
"""
from .archive import OfflineArchive
from .schema_uris import (
    PointerError, UriError, pointer_tokens, resolve, split_fragment,
    valid_anchor, walk_pointer, without_fragment,
)


class OfflineReplayError(Exception):
    pass


class OfflineReplayer:
    def __init__(self, archive_dir):
        self.archive = OfflineArchive(archive_dir)
        self._doc_cache = {}
        self._resource_by_uri = {
            r['resource_uri']: r for r in self.archive.manifest['resources']
        }
        self._anchor = {
            (a['resource_uri'], a['anchor']): a
            for a in self.archive.manifest['anchors']
        }

    # ---- 只读本包，绝不触网 ---------------------------------------------
    def _doc(self, request_uri):
        if request_uri not in self._doc_cache:
            self._doc_cache[request_uri] = self.archive.document_value(request_uri)
        return self._doc_cache[request_uri]

    def _doc_record_for(self, doc_uri):
        """doc_uri可能是文档地址或已登记的嵌套资源URI，返回其文档清单记录。"""
        registered = self._resource_by_uri.get(doc_uri)
        if registered is not None:
            request_uri = registered['request_uri']
        else:
            request_uri = doc_uri
        record = next((d for d in self.archive.manifest['documents']
                       if d['request_uri'] == request_uri), None)
        if record is None:
            raise OfflineReplayError(f'目标文档不在资料包: {doc_uri}')
        return record

    def _root_resource(self, doc_uri, record):
        registered = self._resource_by_uri.get(doc_uri)
        if registered is not None and \
                registered['request_uri'] == record['request_uri'] and \
                registered['document_pointer'] != '':
            return doc_uri
        value = self._doc(record['request_uri'])
        if isinstance(value, bool):
            return record['final_uri']
        root_id = value.get('$id')
        if isinstance(root_id, str) and root_id:
            return without_fragment(resolve(record['final_uri'], root_id))
        return record['final_uri']

    # ---- 单条引用重定位 -------------------------------------------------
    def locate(self, ref, base_uri):
        """把一条原始$ref/$dynamicRef在封存资料中重新解析为目标身份。"""
        absolute = resolve(base_uri, ref)
        doc_uri, fragment = split_fragment(absolute)
        record = self._doc_record_for(doc_uri)
        value = self._doc(record['request_uri'])
        if fragment == '':
            resource = self._root_resource(doc_uri, record)
            res = self._resource_by_uri.get(resource)
            return {
                'resolved_uri': absolute,
                'target_document_uri': record['request_uri'],
                'target_resource_uri': resource,
                'target_pointer': res['document_pointer'] if res else '',
                'resolution': 'empty_fragment',
            }
        from urllib.parse import unquote
        decoded_fragment = unquote(fragment)
        if valid_anchor(decoded_fragment) and not decoded_fragment.startswith('/'):
            resource = self._root_resource(doc_uri, record)
            anchor = self._anchor.get((resource, decoded_fragment))
            if anchor is None:
                raise OfflineReplayError(
                    f'锚在封存资料中缺失: {resource}#{decoded_fragment}')
            return {
                'resolved_uri': absolute,
                'target_document_uri': record['request_uri'],
                'target_resource_uri': resource,
                'target_pointer': anchor['document_pointer'],
                'resolution': 'anchor',
            }
        tokens = pointer_tokens(fragment)
        try:
            _, canonical = walk_pointer(value, tokens)
        except PointerError as exc:
            raise OfflineReplayError(f'{exc.kind}: {exc.detail}') from exc
        owner = self._owner_of(record, canonical)
        return {
            'resolved_uri': absolute,
            'target_document_uri': record['request_uri'],
            'target_resource_uri': owner,
            'target_pointer': canonical,
            'resolution': 'json_pointer',
        }

    def _owner_of(self, record, canonical_pointer):
        from .schema_uris import ARRAY_INDEX, unescape_token
        value = self._doc(record['request_uri'])
        root_id = value.get('$id') if isinstance(value, dict) else None
        if isinstance(root_id, str) and root_id:
            best = without_fragment(resolve(record['final_uri'], root_id))
        else:
            best = record['final_uri']
        node, current_base = value, best
        for raw_token in canonical_pointer.split('/')[1:]:
            token = unescape_token(raw_token)
            if isinstance(node, list):
                if not ARRAY_INDEX.fullmatch(token):
                    break
                node = node[int(token)]
            elif isinstance(node, dict):
                if token not in node:
                    break
                node = node[token]
            else:
                break
            if isinstance(node, dict) and isinstance(node.get('$id'), str) \
                    and node['$id']:
                current_base = without_fragment(
                    resolve(current_base, node['$id']))
                best = current_base
        return best

    # ---- 全闭包重放并与收集阶段比对 -------------------------------------
    def replay_all(self):
        results = []
        mismatches = []
        unresolved = []
        for edge in self.archive.manifest['edges']:
            try:
                replayed = self.locate(edge['ref'], edge['base_uri'])
            except OfflineReplayError as exc:
                # 不完整资料包的未解析目标：记录为重放缺口而不是中断整个重放
                item = {
                    'keyword': edge['keyword'],
                    'ref': edge['ref'],
                    'base_uri': edge['base_uri'],
                    'replayed': None,
                    'matches_manifest': False,
                    'replay_error': str(exc),
                }
                results.append(item)
                unresolved.append(item)
                continue
            match = (
                replayed['target_document_uri'] == edge['target_document_uri']
                and replayed['target_resource_uri'] == edge['target_resource_uri']
                and replayed['target_pointer'] == edge.get('target_pointer')
                and replayed['resolution'] == edge['resolution']
            )
            results.append({
                'keyword': edge['keyword'],
                'ref': edge['ref'],
                'base_uri': edge['base_uri'],
                'replayed': replayed,
                'matches_manifest': match,
            })
            if not match:
                mismatches.append(results[-1])
        return {
            'edges_replayed': len(results),
            'mismatches': mismatches,
            'unresolved': unresolved,
            'complete': not mismatches and not unresolved,
            'results': results,
        }

    def verify_bytes(self):
        """逐一核对封存字节摘要，确保重定位基于未被改写的原文。"""
        import hashlib
        checked = []
        for doc in self.archive.manifest['documents']:
            _, raw, digest, _ = self.archive.locate_document(doc['request_uri'])
            ok = hashlib.sha256(raw).hexdigest() == doc['sha256']
            checked.append({'request_uri': doc['request_uri'],
                            'sha256_match': ok})
            if not ok:
                raise OfflineReplayError(f'字节摘要冲突: {doc["request_uri"]}')
        return checked
