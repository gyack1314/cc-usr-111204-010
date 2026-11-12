"""资料库存摘要、Schema资源闭包收集与禁止网络的离线重放。"""
import argparse
import json
from pathlib import Path

from .io import load
from .domain import summarize
from .schema_closure import collect_closure
from .archive import write_archive
from .offline_replay import OfflineReplayer


def _print(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def cmd_inspect(args):
    value, digest = load(args.file)
    _print({'source_sha256': digest,
            'summary': summarize(value, Path(args.file).parent)})


def cmd_collect(args):
    material, digest = load(args.file)
    manifest, fetched = collect_closure(material, Path(args.file).parent)
    written = write_archive(args.out, manifest, fetched)
    _print({
        'material_sha256': digest,
        'complete': manifest['complete'],
        'documents_fetched': len(manifest['documents']),
        'resources': len(manifest['resources']),
        'edges': len(manifest['edges']),
        'gaps': len(manifest['gaps']),
        'archive': written,
    })


def cmd_replay(args):
    replayer = OfflineReplayer(args.archive)
    byte_check = replayer.verify_bytes()
    if args.ref is not None:
        if args.base is None:
            raise SystemExit('replay --ref需要同时提供--base')
        _print({'byte_check': byte_check,
                'located': replayer.locate(args.ref, args.base)})
        return
    report = replayer.replay_all()
    _print({'byte_check': byte_check,
            'edges_replayed': report['edges_replayed'],
            'mismatches': len(report['mismatches']),
            'unresolved': len(report['unresolved']),
            'complete': report['complete'],
            'results': report['results']})


def main():
    parser = argparse.ArgumentParser(
        description='能源合成Schema资料：库存、闭包收集与离线重放')
    sub = parser.add_subparsers(dest='command', required=True)

    p_inspect = sub.add_parser('inspect', help='展示资料库存与原始字段摘要')
    p_inspect.add_argument('file')
    p_inspect.set_defaults(func=cmd_inspect)

    p_collect = sub.add_parser('collect', help='收集Schema 2020-12资源闭包并写离线资料包')
    p_collect.add_argument('file', help='材料清单JSON（含entry_uri/allowed_origins/documents）')
    p_collect.add_argument('-o', '--out', required=True, help='资料包输出目录')
    p_collect.set_defaults(func=cmd_collect)

    p_replay = sub.add_parser('replay', help='禁止网络，从资料包重新定位引用目标')
    p_replay.add_argument('archive', help='collect产出的资料包目录')
    p_replay.add_argument('--ref', help='单条原始引用（如 base.json#/$defs/unit）')
    p_replay.add_argument('--base', help='该引用所在资源的base URI')
    p_replay.set_defaults(func=cmd_replay)

    args = parser.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()
