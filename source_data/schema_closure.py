"""收集JSON Schema 2020-12资源闭包，输出保留身份的离线资料包数据结构。

设计边界（与docs/data-contract.md对应）：
- 只在资料约定列出的Schema关键字位置遍历；examples、default、自定义字段作为普通JSON原值，
  其中的“$ref”不会被当成依赖。
- 嵌套$id相对当前base解析，建立独立资源边界；$anchor/$dynamicAnchor按资源登记，
  同资源重复锚或相同资源URI的不同定义位置报歧义，不做先到者覆盖。
- 每个request URI只抓取一次，request URI、final URI、$id资源URI三种身份分别保存。
- $ref/$dynamicRef解析为引用边，循环与共享定义以图保存，不递归内联。
- $dynamicRef只记录静态初始目标、动态标记与同名dynamicAnchor索引，不计算运行时动态作用域。
- 越界、缺失、身份冲突都进入gaps，资料包标记为不完整，但已收集原文完整保留。
"""
from collections import deque

from .schema_uris import (
    PointerError, UriError, escape_token, http_origin, pointer_tokens,
    resolve, split_fragment, valid_anchor, walk_pointer, without_fragment,
)

# 仅这些关键字的位置按2020-12约定被当作Schema（布尔或对象）遍历
OBJECT_SCHEMA_KEYWORDS = (
    '$defs', 'properties', 'patternProperties', 'dependentSchemas',
)
ARRAY_SCHEMA_KEYWORDS = ('allOf', 'anyOf', 'oneOf', 'prefixItems')
SINGLE_SCHEMA_KEYWORDS = (
    'additionalProperties', 'unevaluatedProperties', 'propertyNames', 'items',
    'contains', 'not', 'if', 'then', 'else', 'contentSchema',
)
# 身份与引用关键字单独处理；examples/default及任何其他字段按普通原值保留
ID_KEYWORD = '$id'
REF_KEYWORD = '$ref'
DYNAMIC_REF_KEYWORD = '$dynamicRef'
ANCHOR_KEYWORD = '$anchor'
DYNAMIC_ANCHOR_KEYWORD = '$dynamicAnchor'


class MaterialFetchError(Exception):
    """虚拟HTTP资料里的文档无法提供。"""


class MaterialFetcher:
    """按材料清单模拟HTTP GET：同request URI只抓一次，保留重定向链与字节摘要。

    该实现完全离线，只读取材料file字段指向的本地封存文件；不发起任何真实网络请求。
    """

    def __init__(self, material, material_root):
        self.entry_uri = material['entry_uri']
        self.allowed_origins = list(material['allowed_origins'])
        self._by_request = {}
        for doc in material['documents']:
            request_uri = without_fragment(doc['request_uri'])
            if request_uri in self._by_request:
                raise MaterialFetchError(f'材料中request URI重复: {request_uri}')
            self._by_request[request_uri] = doc
        self._material_root = material_root
        self.fetched = {}

    def allowed(self, uri):
        try:
            origin = http_origin(uri)
        except UriError:
            return False
        return origin in self.allowed_origins

    def get(self, request_uri):
        """返回封存响应记录；同一地址第二次调用直接报错而不是重新抓取。"""
        request_uri = without_fragment(request_uri)
        if request_uri in self.fetched:
            raise MaterialFetchError(f'同一远端文档被重复抓取: {request_uri}')
        doc = self._by_request.get(request_uri)
        if doc is None:
            raise MaterialFetchError(f'材料中没有该文档的封存响应: {request_uri}')
        from .io import asset, loads_strict
        try:
            raw, digest = asset(self._material_root, doc['file'])
        except (OSError, ValueError) as exc:
            raise MaterialFetchError(
                f'封存文件无法读取({doc["file"]}): {exc}') from exc
        final_uri = without_fragment(doc['final_uri'])
        if 'redirect_chain' in doc:
            chain = [without_fragment(x) for x in doc['redirect_chain']]
            if not chain or chain[0] != request_uri or chain[-1] != final_uri:
                raise MaterialFetchError('redirect_chain与request/final URI不一致')
        else:
            chain = [request_uri] if request_uri == final_uri else [request_uri, final_uri]
        try:
            value = loads_strict(raw)
        except ValueError as exc:
            raise MaterialFetchError(f'{doc["file"]}不是严格JSON: {exc}') from exc
        if not isinstance(value, (dict, bool)):
            raise MaterialFetchError(f'{doc["file"]}根节点不是布尔或对象Schema')
        record = {
            'request_uri': request_uri,
            'final_uri': final_uri,
            'redirect_chain': chain,
            'file': doc['file'],
            'raw_bytes': raw,
            'sha256': digest,
            'value': value,
        }
        self.fetched[request_uri] = record
        return record


class ClosureCollector:
    def __init__(self, fetcher):
        self.fetcher = fetcher
        self.documents = {}       # request URI -> 文档记录（含解析值）
        self.resources = {}       # 资源URI（无片段） -> 资源登记
        self.anchor_index = {}    # (资源URI, 锚名) -> 位置信息
        self.dynamic_anchors = {}  # 锚名 -> 登记了同名$dynamicAnchor的资源URI集合
        self.raw_refs = []
        self.gaps = []
        self.edges = []

    # ---- 缺口登记 -------------------------------------------------------
    def _gap(self, kind, detail, **where):
        record = {'kind': kind, 'detail': detail}
        record.update(where)
        self.gaps.append(record)
        return record

    # ---- 抓取与文档身份 -------------------------------------------------
    def collect(self, entry_uri):
        entry_request = without_fragment(entry_uri)
        queue = deque([entry_request])
        queued = {entry_request}
        while queue:
            request_uri = queue.popleft()
            if not self.fetcher.allowed(request_uri):
                self._gap('origin_not_allowed',
                          '文档地址越出允许来源范围',
                          request_uri=request_uri)
                continue
            try:
                record = self.fetcher.get(request_uri)
            except MaterialFetchError as exc:
                self._gap('document_unavailable', str(exc),
                          request_uri=request_uri)
                continue
            self.documents[request_uri] = record
            self._index_document(record, queue, queued)
        self._resolve_refs(queue, queued)

    def _index_document(self, record, queue, queued):
        value = record['value']
        final_uri = record['final_uri']
        if isinstance(value, bool):
            # 布尔Schema没有$id，资源URI取最终响应地址
            self._register_resource(final_uri, record['request_uri'], '', value,
                                    record, boolean_root=True)
            return
        root_id = value.get(ID_KEYWORD)
        if isinstance(root_id, str) and root_id:
            try:
                resource_uri = self._valid_abs_id(root_id, final_uri)
            except UriError as exc:
                self._gap('invalid_id', str(exc), id=root_id, base_uri=final_uri)
                resource_uri = final_uri
        else:
            resource_uri = final_uri
        self._register_resource(resource_uri, record['request_uri'], '', value,
                                record)
        self._walk(value, base_uri=resource_uri, pointer='',
                   owner_resource=resource_uri, record=record,
                   queue=queue, queued=queued)

    def _valid_abs_id(self, ref, base):
        """$id按当前base解析；2020-12禁止$id携带非空片段，违例登记缺口并按去片段URI容忍。"""
        resolved_full = resolve(base, ref)
        resolved = without_fragment(resolved_full)
        http_origin(resolved)  # 非http/https或相对身份无法成立时抛UriError
        if split_fragment(resolved_full)[1] != '':
            self._gap('invalid_id', '$id不允许携带非空片段', id=ref, base_uri=base)
        return resolved

    def _register_resource(self, resource_uri, request_uri, pointer, node,
                           record, boolean_root=False):
        existing = self.resources.get(resource_uri)
        location = {
            'request_uri': request_uri,
            'document_pointer': pointer,
            'file': record['file'],
        }
        if existing is not None:
            same_doc = existing['request_uri'] == request_uri
            same_pointer = existing['document_pointer'] == pointer
            if not (same_doc and same_pointer):
                self._gap('duplicate_resource_id',
                          '相同绝对资源URI在不同定义位置重复，拒绝覆盖',
                          resource_uri=resource_uri,
                          first_location=existing['location'],
                          duplicate_location=location)
            return existing
        entry = {
            'resource_uri': resource_uri,
            'request_uri': request_uri,
            'final_uri': record['final_uri'],
            'document_pointer': pointer,
            'file': record['file'],
            'boolean_root': boolean_root,
            'location': location,
            'node': node,
        }
        self.resources[resource_uri] = entry
        return entry

    def _register_anchor(self, resource_uri, name, dynamic, pointer, record):
        if not valid_anchor(name):
            self._gap('invalid_anchor', '锚名不符合2020-12语法',
                      resource_uri=resource_uri, anchor=name,
                      document_pointer=pointer)
            return
        key = (resource_uri, name)
        existing = self.anchor_index.get(key)
        location = {'request_uri': record['request_uri'],
                    'document_pointer': pointer, 'file': record['file']}
        if existing is not None:
            if existing['document_pointer'] != pointer or \
                    existing['request_uri'] != record['request_uri']:
                self._gap('duplicate_anchor',
                          '同资源锚在不同定义位置重复，拒绝覆盖',
                          resource_uri=resource_uri, anchor=name,
                          first_location=existing, duplicate_location=location)
            # 同位置重复登记幂等，动态与普通锚同名冲突也在下面处理
        else:
            self.anchor_index[key] = {**location, 'dynamic': dynamic}
        bucket = self.dynamic_anchors.setdefault(name, set())
        bucket.add(resource_uri)

    # ---- Schema树遍历 ---------------------------------------------------
    def _walk(self, node, base_uri, pointer, owner_resource, record,
              queue, queued):
        if isinstance(node, bool):
            return  # 布尔Schema无内部关键字
        if not isinstance(node, dict):
            return
        if ID_KEYWORD in node and pointer:
            ref = node[ID_KEYWORD]
            if not isinstance(ref, str) or not ref:
                self._gap('invalid_id', '$id必须为非空字符串',
                          base_uri=base_uri, document_pointer=pointer)
            else:
                try:
                    new_base = self._valid_abs_id(ref, base_uri)
                except UriError as exc:
                    self._gap('invalid_id', str(exc), id=ref, base_uri=base_uri)
                else:
                    self._register_resource(new_base, record['request_uri'],
                                            pointer, node, record)
                    owner_resource = new_base
                    base_uri = new_base
        if ANCHOR_KEYWORD in node:
            self._register_anchor(owner_resource, node[ANCHOR_KEYWORD],
                                  False, pointer, record)
        if DYNAMIC_ANCHOR_KEYWORD in node:
            self._register_anchor(owner_resource, node[DYNAMIC_ANCHOR_KEYWORD],
                                  True, pointer, record)
        for keyword in (REF_KEYWORD, DYNAMIC_REF_KEYWORD):
            if keyword in node:
                self._capture_ref(node[keyword], keyword, base_uri,
                                  owner_resource, pointer, record,
                                  queue, queued)
        for keyword in OBJECT_SCHEMA_KEYWORDS:
            container = node.get(keyword)
            if isinstance(container, dict):
                for child_name, child in container.items():
                    if isinstance(child, (dict, bool)):
                        self._walk(child, base_uri,
                                   f'{pointer}/{keyword}/{escape_token(str(child_name))}',
                                   owner_resource, record, queue, queued)
        for keyword in ARRAY_SCHEMA_KEYWORDS:
            container = node.get(keyword)
            if isinstance(container, list):
                for index, child in enumerate(container):
                    if isinstance(child, (dict, bool)):
                        self._walk(child, base_uri,
                                   f'{pointer}/{keyword}/{index}',
                                   owner_resource, record, queue, queued)
        for keyword in SINGLE_SCHEMA_KEYWORDS:
            child = node.get(keyword)
            if isinstance(child, (dict, bool)):
                self._walk(child, base_uri, f'{pointer}/{keyword}',
                           owner_resource, record, queue, queued)
        # examples、default及未知自定义字段：普通JSON原值，不扫描其中的伪$ref

    def _capture_ref(self, ref, keyword, base_uri, owner_resource, pointer,
                     record, queue, queued):
        if not isinstance(ref, str) or not ref:
            self._gap('invalid_ref', f'{keyword}必须为非空字符串',
                      resource_uri=owner_resource, document_pointer=pointer)
            return
        self.raw_refs.append({
            'keyword': keyword,
            'ref': ref,
            'base_uri': base_uri,
            'source_resource_uri': owner_resource,
            'source_pointer': pointer,
            'request_uri': record['request_uri'],
            'file': record['file'],
        })
        # 只把去片段后的同域文档加入抓取队列，片段解析等全部文档就位后统一进行。
        # 已登记的资源URI（含同文档嵌套$id）不是独立抓取地址，不得重复抓文档。
        target_doc = without_fragment(resolve(base_uri, ref))
        if self.fetcher.allowed(target_doc) and target_doc not in queued \
                and target_doc not in self.resources:
            queued.add(target_doc)
            queue.append(target_doc)

    # ---- 引用解析（图，不内联） ------------------------------------------
    def _resolve_refs(self, queue, queued):
        for raw in self.raw_refs:
            self._resolve_one(raw)

    def _resolve_one(self, raw):
        dynamic = raw['keyword'] == DYNAMIC_REF_KEYWORD
        edge_base = {
            'source_resource_uri': raw['source_resource_uri'],
            'source_pointer': raw['source_pointer'],
            'keyword': raw['keyword'],
            'ref': raw['ref'],
            'base_uri': raw['base_uri'],
            'dynamic': dynamic,
        }
        try:
            absolute = resolve(raw['base_uri'], raw['ref'])
        except ValueError as exc:
            self._gap('invalid_ref', f'引用无法解析: {exc}', **edge_base)
            return
        doc_uri, fragment = split_fragment(absolute)
        try:
            http_origin(doc_uri)
        except UriError as exc:
            self._gap('invalid_ref', str(exc), resolved_uri=absolute, **edge_base)
            return
        if not self.fetcher.allowed(doc_uri):
            self._gap('origin_not_allowed',
                      '引用目标越出允许来源范围',
                      resolved_uri=absolute, **edge_base)
            return
        record = self.documents.get(doc_uri)
        if record is None:
            # 可能指向已登记的嵌套$id资源：其URI不是文档地址，用资源所属文档定位
            registered = self.resources.get(doc_uri)
            if registered is not None:
                record = self.documents.get(registered['request_uri'])
        if record is None:
            self._gap('target_document_missing',
                      '引用目标文档未在闭包中抓取成功',
                      target_document_uri=doc_uri, resolved_uri=absolute,
                      **edge_base)
            return

        if fragment == '':
            self._finish_empty_fragment(edge_base, doc_uri, record, absolute)
            return
        # 片段先percent decode一次再判定形态：具名锚按解码名查索引，
        # Pointer仍把原始（带编码）片段交给pointer_tokens统一解码
        from urllib.parse import unquote
        decoded_fragment = unquote(fragment)
        if valid_anchor(decoded_fragment) and not decoded_fragment.startswith('/'):
            self._finish_named_fragment(edge_base, doc_uri, decoded_fragment,
                                        record, absolute)
        else:
            self._finish_pointer_fragment(edge_base, doc_uri, fragment, record,
                                          absolute, dynamic)

    def _root_resource_of(self, doc_uri, record):
        """文档根资源URI；若doc_uri本身是已登记的嵌套资源，直接采用该资源身份。"""
        registered = self.resources.get(doc_uri)
        if registered is not None and registered['request_uri'] == record['request_uri'] \
                and registered['document_pointer'] != '':
            return doc_uri
        root = record['value']
        if isinstance(root, bool):
            return record['final_uri']
        root_id = root.get(ID_KEYWORD)
        if isinstance(root_id, str) and root_id:
            return without_fragment(resolve(record['final_uri'], root_id))
        return record['final_uri']

    def _finish_empty_fragment(self, edge_base, doc_uri, record, absolute):
        target_resource = self._root_resource_of(doc_uri, record)
        resource = self.resources.get(target_resource)
        target_pointer = resource['document_pointer'] if resource is not None else ''
        edge = dict(edge_base)
        edge.update({
            'resolved_uri': absolute,
            'target_document_uri': record['request_uri'],
            'target_resource_uri': target_resource,
            'target_pointer': target_pointer,
            'resolution': 'empty_fragment',
        })
        if resource is None:
            self._gap('target_resource_missing',
                      '空片段指向的根资源未登记（$id身份冲突）',
                      **edge)
        self.edges.append(edge)

    def _finish_named_fragment(self, edge_base, doc_uri, fragment, record,
                               absolute):
        # 具名片段在资源索引中定位；先找引用URI所属资源，再在该资源内查锚
        root_resource = self._root_resource_of(doc_uri, record)
        key = (root_resource, fragment)
        anchor = self.anchor_index.get(key)
        edge = dict(edge_base)
        edge.update({
            'resolved_uri': absolute,
            'target_document_uri': record['request_uri'],
            'target_resource_uri': root_resource,
            'target_fragment': fragment,
            'resolution': 'anchor',
        })
        if anchor is None:
            self._gap('anchor_missing',
                      '目标资源内未登记该锚（重复锚歧义见duplicate_anchor缺口）',
                      **edge)
            self.edges.append(edge)
            return
        edge['target_pointer'] = anchor['document_pointer']
        if edge_base['dynamic']:
            # 静态初始目标如实标注；动态作用域留给后续独立验证器决定
            edge['dynamic_anchor_name'] = fragment
            edge['dynamic_scope_candidates'] = sorted(
                self.dynamic_anchors.get(fragment, set()))
        self.edges.append(edge)

    def _finish_pointer_fragment(self, edge_base, doc_uri, fragment, record,
                                 absolute, dynamic):
        try:
            tokens = pointer_tokens(fragment)
        except UriError as exc:
            self._gap('invalid_pointer', str(exc), resolved_uri=absolute,
                      **edge_base)
            return
        try:
            target, canonical = walk_pointer(record['value'], tokens)
        except PointerError as exc:
            self._gap(exc.kind, exc.detail, target_document_uri=doc_uri,
                      resolved_uri=absolute, pointer=exc.pointer, **edge_base)
            return
        owner_resource = self._owner_of(record, canonical)
        edge = dict(edge_base)
        edge.update({
            'resolved_uri': absolute,
            'target_document_uri': record['request_uri'],
            'target_resource_uri': owner_resource,
            'target_pointer': canonical,
            'resolution': 'json_pointer',
        })
        if dynamic:
            # 非锚形式的$dynamicRef静态行为等同$ref，仅保留动态标记，
            # 不冒称其为运行时动态作用域的最终解析
            edge['dynamic_anchor_name'] = None
            edge['dynamic_scope_candidates'] = []
        self.edges.append(edge)

    def _owner_of(self, record, canonical_pointer):
        """根据文档内资源边界，找Pointer目标所属的嵌套资源URI。

        沿途遇到嵌套$id就切换base与所属资源；Pointer词元按~1/~0还原后再取键。
        """
        from .schema_uris import ARRAY_INDEX, unescape_token
        root = record['value']
        root_id = root.get(ID_KEYWORD) if isinstance(root, dict) else None
        if isinstance(root_id, str) and root_id:
            best = without_fragment(resolve(record['final_uri'], root_id))
        else:
            best = record['final_uri']
        node = root
        current_base = best
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
            if isinstance(node, dict) and isinstance(node.get(ID_KEYWORD), str) \
                    and node[ID_KEYWORD]:
                current_base = without_fragment(
                    resolve(current_base, node[ID_KEYWORD]))
                best = current_base
        return best

    # ---- 结果整理 -------------------------------------------------------
    def manifest(self, entry_uri):
        resource_graph = self._resource_graph()
        return {
            'entry_uri': entry_uri,
            'allowed_origins': list(self.fetcher.allowed_origins),
            'complete': not self.gaps,
            'documents': [self._public_doc(r) for r in self.documents.values()],
            'resources': [self._public_resource(r) for r in self.resources.values()],
            'anchors': [
                {'resource_uri': resource_uri, 'anchor': name, **info}
                for (resource_uri, name), info in sorted(self.anchor_index.items())
            ],
            'dynamic_anchor_index': {
                name: sorted(uris)
                for name, uris in sorted(self.dynamic_anchors.items())
            },
            'edges': self.edges,
            'resource_graph': resource_graph,
            'gaps': self.gaps,
        }

    @staticmethod
    def _public_doc(record):
        return {
            'request_uri': record['request_uri'],
            'final_uri': record['final_uri'],
            'redirect_chain': list(record['redirect_chain']),
            'file': record['file'],
            'bytes': len(record['raw_bytes']),
            'sha256': record['sha256'],
        }

    @staticmethod
    def _public_resource(entry):
        return {
            'resource_uri': entry['resource_uri'],
            'request_uri': entry['request_uri'],
            'final_uri': entry['final_uri'],
            'document_pointer': entry['document_pointer'],
            'file': entry['file'],
            'boolean_root': entry['boolean_root'],
        }

    def _resource_graph(self):
        """以资源URI+目标位置为边的依赖图，循环与共享定义原样保留为图结构。"""
        adjacency = {}
        for edge in self.edges:
            if 'target_resource_uri' not in edge:
                continue
            node = adjacency.setdefault(edge['source_resource_uri'], set())
            node.add((edge['target_resource_uri'], edge.get('target_pointer', '')))
        return {
            source: sorted(targets)
            for source, targets in sorted(adjacency.items())
        }


def collect_closure(material, material_root):
    """材料清单 -> (manifest字典, request URI -> 抓取记录)。"""
    fetcher = MaterialFetcher(material, material_root)
    collector = ClosureCollector(fetcher)
    collector.collect(material['entry_uri'])
    return collector.manifest(material['entry_uri']), fetcher.fetched
