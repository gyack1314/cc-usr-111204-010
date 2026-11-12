"""JSON Schema 2020-12的URI、片段与JSON Pointer处理，仅使用标准库。

只处理资源身份与定位语法，不读取实例数据，也不做Schema校验。
"""
import re
from urllib.parse import urldefrag, urljoin, urlsplit, unquote

# 2020-12锚名：ALPHA或下划线开头，其后仅允许字母数字、-、.、_
ANCHOR_NAME = re.compile(r'^[A-Za-z_][A-Za-z0-9_.-]*$')
# RFC 6901数组下标：0或无前导零正整数，“-”不指向存在元素
ARRAY_INDEX = re.compile(r'^(0|[1-9][0-9]*)$')
HTTP_SCHEMES = ('http', 'https')


class UriError(ValueError):
    """URI身份或片段语法不合法。"""


class PointerError(Exception):
    """JSON Pointer无法定位，kind严格区分下标语法、越界、属性缺失与标量中断。"""

    def __init__(self, kind, pointer, detail):
        super().__init__(detail)
        self.kind = kind
        self.pointer = pointer
        self.detail = detail


def http_origin(uri):
    """返回scheme://host[:port]形式的来源，非http/https绝对URI报错。"""
    parts = urlsplit(uri)
    if parts.scheme not in HTTP_SCHEMES or not parts.netloc:
        raise UriError(f'仅支持http/https绝对URI: {uri}')
    return f'{parts.scheme}://{parts.netloc}'


def without_fragment(uri):
    return urldefrag(uri).url


def split_fragment(uri):
    """返回(去片段URI, 原始片段)，片段保留百分号编码交由调用方解码。"""
    url, fragment = urldefrag(uri)
    return url, fragment


def resolve(base, ref):
    """按RFC 3986相对解析；base自带片段会被自动去掉。"""
    return urljoin(base, ref)


def valid_anchor(name):
    return isinstance(name, str) and bool(ANCHOR_NAME.fullmatch(name))


def escape_token(token):
    """把属性名渲染为JSON Pointer词元：先~0后~1的逆操作。"""
    return token.replace('~', '~0').replace('/', '~1')


def pointer_tokens(fragment):
    """片段先percent decode一次成为Unicode，再按~1、~0规则还原Pointer词元。"""
    decoded = unquote(fragment)
    if not decoded.startswith('/'):
        raise UriError(f'JSON Pointer片段必须以/开头: {fragment}')
    return [unescape_token(raw) for raw in decoded.split('/')[1:]]


def unescape_token(token):
    """Pointer词元逆转义：~1还原为/，~0还原为~（顺序不可颠倒）。"""
    return token.replace('~1', '/').replace('~0', '~')


def walk_pointer(root, tokens):
    """沿Pointer严格定位，返回(目标节点, 规范Pointer字符串)。

    数组下标语法错误、下标越界、对象属性缺失、穿过标量分别抛出不同kind，
    调用方不能把它们合并成同一种“目标不存在”。
    """
    node = root
    location = ''
    for token in tokens:
        location += '/' + escape_token(token)
        if isinstance(node, list):
            if not ARRAY_INDEX.fullmatch(token):
                raise PointerError('pointer_bad_index', location,
                                   f'非法数组下标: {token!r}')
            index = int(token)
            if index >= len(node):
                raise PointerError('pointer_out_of_range', location,
                                   f'下标{index}越界，数组长度{len(node)}')
            node = node[index]
        elif isinstance(node, dict):
            if token not in node:
                raise PointerError('pointer_missing_property', location,
                                   f'对象缺少属性: {token!r}')
            node = node[token]
        else:
            raise PointerError('pointer_through_scalar', location,
                               '标量节点无法继续定位')
    return node, location
