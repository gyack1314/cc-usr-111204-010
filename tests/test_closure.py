"""Schema 2020-12资源闭包收集、身份保留资料包与禁止网络重放的检查。

全部输入为合成Schema与虚拟HTTP封存响应，无真实网络、设备凭据或实例数据。
"""
import json
import socket
import tempfile
import unittest
from pathlib import Path

from source_data.io import load
from source_data.schema_uris import (
    PointerError, pointer_tokens, unescape_token, valid_anchor, walk_pointer,
)
from source_data.schema_closure import collect_closure, MaterialFetcher
from source_data.archive import write_archive, OfflineArchive
from source_data.offline_replay import OfflineReplayer

DATA = Path('data')


def _collect(name):
    material, _ = load(DATA / name)
    return collect_closure(material, DATA)


class UriPointerTests(unittest.TestCase):
    def test_percent_decode_before_pointer_escapes(self):
        # %24先解码为$，再按Pointer定位；与直接写法收敛到同一词元序列
        self.assertEqual(pointer_tokens('/%24defs/thing'), ['$defs', 'thing'])
        self.assertEqual(pointer_tokens('/$defs/a~1b'), ['$defs', 'a/b'])
        self.assertEqual(pointer_tokens('/$defs/a~0b'), ['$defs', 'a~b'])
        self.assertEqual(unescape_token('a~1b~0c'), 'a/b~c')

    def test_array_index_distinguished_from_missing_property(self):
        root = {'prefixItems': [{'type': 'string'}]}
        node, pointer = walk_pointer(root, pointer_tokens('/prefixItems/0'))
        self.assertEqual(node, {'type': 'string'})
        with self.assertRaises(PointerError) as ctx:
            walk_pointer(root, pointer_tokens('/prefixItems/01'))
        self.assertEqual(ctx.exception.kind, 'pointer_bad_index')
        with self.assertRaises(PointerError) as ctx:
            walk_pointer(root, pointer_tokens('/prefixItems/9'))
        self.assertEqual(ctx.exception.kind, 'pointer_out_of_range')
        with self.assertRaises(PointerError) as ctx:
            walk_pointer(root, pointer_tokens('/nope/0'))
        self.assertEqual(ctx.exception.kind, 'pointer_missing_property')
        with self.assertRaises(PointerError) as ctx:
            walk_pointer(root, pointer_tokens('/prefixItems/0/type/x'))
        self.assertEqual(ctx.exception.kind, 'pointer_through_scalar')

    def test_anchor_syntax(self):
        self.assertTrue(valid_anchor('entry'))
        self.assertFalse(valid_anchor('a/b'))
        self.assertFalse(valid_anchor('1bad'))
        self.assertFalse(valid_anchor(''))


class ClosureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest, cls.fetched = _collect('closure.json')
        cls.edges = cls.manifest['edges']

    def test_complete_closure_has_no_gaps(self):
        self.assertTrue(self.manifest['complete'])
        self.assertEqual(self.manifest['gaps'], [])

    def test_three_identities_kept_distinct(self):
        start = next(d for d in self.manifest['documents']
                     if d['request_uri'].endswith('/start'))
        # request URI、final URI、资源URI三者不同，且都被保留
        self.assertEqual(start['final_uri'],
                         'https://dict.example.invalid/schema/root.json')
        self.assertEqual(start['redirect_chain'], [
            'https://dict.example.invalid/start',
            'https://dict.example.invalid/v1/root',
            'https://dict.example.invalid/schema/root.json',
        ])
        root_resource = next(r for r in self.manifest['resources']
                             if r['resource_uri'].endswith('/root.json'))
        self.assertEqual(root_resource['request_uri'],
                         'https://dict.example.invalid/start')
        self.assertEqual(len(start['sha256']), 64)

    def test_nested_id_forms_resource_boundary(self):
        uris = {r['resource_uri'] for r in self.manifest['resources']}
        self.assertIn('https://dict.example.invalid/schema/parts/reading.json',
                      uris)
        reading = next(r for r in self.manifest['resources']
                       if r['resource_uri'].endswith('/reading.json'))
        # 嵌套资源保留文档位置与资源身份的对应，不被错当外层资源根
        self.assertEqual(reading['document_pointer'], '/$defs/reading')
        self.assertTrue(reading['request_uri'].endswith('/start'))

    def test_fetch_once_per_request_uri(self):
        # 同一request URI只允许抓取一次：fetcher对二次调用直接报错
        fetcher = MaterialFetcher(load(DATA / 'closure.json')[0], DATA)
        uri = 'https://dict.example.invalid/schema/base.json'
        fetcher.get(uri)
        with self.assertRaises(Exception):
            fetcher.get(uri)

    def test_pseudo_refs_in_examples_default_custom_are_not_edges(self):
        refs = {e['ref'] for e in self.edges}
        self.assertNotIn(
            'https://forbidden.example.invalid/not-a-schema', refs)
        self.assertNotIn(
            'https://forbidden.example.invalid/also-ignored', refs)
        self.assertNotIn(
            'https://forbidden.example.invalid/custom-keyword-ignored', refs)
        self.assertNotIn(
            'https://forbidden.example.invalid/pseudo-ref-in-examples', refs)
        self.assertNotIn('#/definitely/not/scanned', refs)

    def test_encoded_anchor_and_escaped_slash_edges(self):
        by_ref = {e['ref']: e for e in self.edges}
        encoded = by_ref['#%73haredAnchor']
        self.assertEqual(encoded['resolution'], 'anchor')
        self.assertEqual(encoded['target_pointer'], '/$defs/shared')
        slash = by_ref['base.json#/$defs/a~1b']
        self.assertEqual(slash['target_pointer'], '/$defs/a~1b')
        tilde = next(e for e in self.edges if e['ref'] == '#/$defs/a~0b')
        self.assertEqual(tilde['target_pointer'], '/$defs/a~0b')
        # 百分号片段与未编码写法定位到同一目标
        percent = next(e for e in self.edges
                       if e['ref'] == '#/%24defs/thing')
        plain = next(e for e in self.edges
                     if e['ref'] == '#/$defs/thing')
        self.assertEqual(percent['target_pointer'], plain['target_pointer'])

    def test_shared_definition_converges_to_single_target(self):
        shared = [e for e in self.edges
                  if e['ref'] == 'links.json#/$defs/thing']
        self.assertEqual(len(shared), 2)
        targets = {(e['target_resource_uri'], e['target_pointer'])
                   for e in shared}
        self.assertEqual(len(targets), 1)

    def test_cycles_are_graph_edges_not_inlined(self):
        graph = self.manifest['resource_graph']
        reading = 'https://dict.example.invalid/schema/parts/reading.json'
        cycle = 'https://dict.example.invalid/schema/cycle.json'
        node = 'https://dict.example.invalid/schema/parts/node.json'
        # 跨资源循环：reading -> cycle -> reading
        self.assertIn((cycle, ''),
                      [tuple(t) for t in graph[reading]])
        self.assertIn((reading, '/$defs/reading'),
                      [tuple(t) for t in graph[cycle]])
        # 自环与dynamicRef自指都作为图边保留
        self.assertIn((node, '/$defs/node'),
                      [tuple(t) for t in graph[node]])

    def test_dynamic_ref_keeps_static_target_and_dynamic_marker(self):
        dyn = [e for e in self.edges if e['keyword'] == '$dynamicRef']
        self.assertTrue(dyn)
        for edge in dyn:
            self.assertTrue(edge['dynamic'])
            # 同名dynamicAnchor索引保留全部候选资源，不冒成单一最终目标
            candidates = edge['dynamic_scope_candidates']
            self.assertIn(
                'https://dict.example.invalid/schema/parts/node.json',
                candidates)
            self.assertIn(
                'https://dict.example.invalid/schema/base.json', candidates)
            self.assertEqual(edge['dynamic_anchor_name'], 'node')
        # 原始$dynamicRef字符串仍在边上，未被改写
        self.assertTrue(all(e['ref'] == '#node' for e in dyn))

    def test_boolean_schemas_are_traversed_safely(self):
        # 布尔Schema不展开内部关键字，但作为遍历位置不报错、闭包仍完整
        self.assertTrue(self.manifest['complete'])


class GapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest, cls.fetched = _collect('gaps.json')

    def test_archive_marked_incomplete_but_raw_kept(self):
        self.assertFalse(self.manifest['complete'])
        kinds = {g['kind'] for g in self.manifest['gaps']}
        # 越界、缺失、身份冲突、歧义全覆盖
        self.assertIn('origin_not_allowed', kinds)
        self.assertIn('document_unavailable', kinds)
        self.assertIn('target_document_missing', kinds)
        self.assertIn('duplicate_resource_id', kinds)
        self.assertIn('duplicate_anchor', kinds)
        self.assertIn('pointer_bad_index', kinds)
        self.assertIn('pointer_out_of_range', kinds)
        self.assertIn('pointer_missing_property', kinds)
        self.assertIn('pointer_through_scalar', kinds)
        self.assertIn('anchor_missing', kinds)
        # 已收集原文仍可查看
        self.assertEqual(len(self.manifest['documents']), 9)

    def test_pointer_error_kinds_are_strictly_distinguished(self):
        by_kind = {}
        for gap in self.manifest['gaps']:
            by_kind.setdefault(gap['kind'], gap)
        self.assertEqual(by_kind['pointer_bad_index']['pointer']
                         .split('/')[-1], '01')
        self.assertEqual(by_kind['pointer_out_of_range']['pointer']
                         .split('/')[-1], '9')

    def test_duplicate_anchor_not_overwritten(self):
        dup = [g for g in self.manifest['gaps']
               if g['kind'] == 'duplicate_anchor']
        self.assertEqual(len(dup), 2)  # 普通锚与dynamicAnchor各一
        for gap in dup:
            self.assertIn('first_location', gap)
            self.assertIn('duplicate_location', gap)
            self.assertNotEqual(gap['first_location']['document_pointer'],
                                gap['duplicate_location']['document_pointer'])

    def test_duplicate_resource_id_reports_conflict(self):
        twin = next(g for g in self.manifest['gaps']
                    if g['kind'] == 'duplicate_resource_id')
        self.assertEqual(twin['resource_uri'],
                         'https://dict.example.invalid/schema/twin.json')
        self.assertNotEqual(twin['first_location']['request_uri'],
                            twin['duplicate_location']['request_uri'])


class ArchiveAndReplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.out = Path(cls.tmp.name) / 'pack'
        manifest, fetched = _collect('closure.json')
        cls.manifest = manifest
        write_archive(cls.out, manifest, fetched)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_archive_preserves_raw_bytes_and_identity(self):
        archive = OfflineArchive(self.out)
        # URI映射同时含request与final两种身份，指向同一封存文件
        uri_map = json.loads((self.out / 'uri-map.json').read_text('utf8'))
        self.assertEqual(
            uri_map['https://dict.example.invalid/start'],
            uri_map['https://dict.example.invalid/schema/root.json'])
        rel, raw, digest, record = archive.locate_document(
            'https://dict.example.invalid/start')
        self.assertEqual(digest, record['sha256'])
        value = json.loads(raw)
        # 原文$id与原始$ref均未被改写为本地路径
        self.assertEqual(value['$id'],
                         'https://dict.example.invalid/schema/root.json')
        self.assertIn('parts/reading.json#entry', json.dumps(value))

    def test_offline_replay_relocates_every_edge_identically(self):
        replayer = OfflineReplayer(self.out)
        report = replayer.replay_all()
        self.assertEqual(report['edges_replayed'],
                         len(self.manifest['edges']))
        self.assertEqual(report['mismatches'], [])
        self.assertTrue(report['complete'])

    def test_replay_uses_only_archive_and_blocks_network(self):
        # 证明重放不触网：让任何socket操作立即失败，重放仍全部成功
        def _blocked(*args, **kwargs):
            raise AssertionError('离线重放禁止任何网络socket调用')
        original = socket.socket
        socket.socket = _blocked
        try:
            replayer = OfflineReplayer(self.out)
            report = replayer.replay_all()
            single = replayer.locate(
                'base.json#/$defs/unit',
                'https://dict.example.invalid/schema/root.json')
        finally:
            socket.socket = original
        self.assertTrue(report['complete'])
        self.assertEqual(single['target_pointer'], '/$defs/unit')
        self.assertEqual(single['resolution'], 'json_pointer')

    def test_single_replay_after_redirect_uses_final_base(self):
        # 重定向后的相对引用按最终文档位置解析
        replayer = OfflineReplayer(self.out)
        located = replayer.locate(
            'base.json#/$defs/unit',
            'https://dict.example.invalid/schema/root.json')
        self.assertEqual(located['target_document_uri'],
                         'https://dict.example.invalid/schema/base.json')


class IncompleteArchiveReplayTests(unittest.TestCase):
    def test_replay_reports_unresolved_without_crashing(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'gaps-pack'
            manifest, fetched = _collect('gaps.json')
            self.assertFalse(manifest['complete'])
            write_archive(out, manifest, fetched)
            replayer = OfflineReplayer(out)
            # 已解析边仍能逐条重新定位；未解析目标记为replay_error而非抛异常
            report = replayer.replay_all()
            self.assertEqual(report['edges_replayed'], len(manifest['edges']))
            self.assertFalse(report['complete'])
            self.assertTrue(report['unresolved'])
            matched = [r for r in report['results'] if r['matches_manifest']]
            self.assertTrue(matched)
            # 已收集原文仍可读
            self.assertTrue(replayer.verify_bytes())


if __name__ == '__main__':
    unittest.main()
