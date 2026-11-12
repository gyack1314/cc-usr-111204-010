# 设备数据字典的Schema资源闭包收集

此Python项目管理独立的能源领域合成源资料，`data/`为原始样本及访问配置。`source_data.io`负责严格读取和字节身份，`source_data.domain`提供现有库存摘要，`source_data.replay`按声明顺序返回不解释的源响应。`source_data.schema_closure`按JSON Schema 2020-12 URI资源模型收集引用闭包，`source_data.archive`写保留身份的离线资料包，`source_data.offline_replay`在禁止网络的条件下重放同一闭包。字段和支持的来源口径见[资料约定](docs/data-contract.md)。所有机构、设备、对象与数值均为虚构。

Python 3.11及以上即可使用，无第三方Python依赖。项目根目录运行：

```sh
python -m unittest discover -s tests -v
python -m compileall -q source_data tests
python -m source_data.cli inspect data/normal.json
```

## 闭包收集与离线资料包

`collect`从材料清单的入口URI和允许来源出发，只模拟对封存响应的HTTP GET（不发起真实网络请求），按资料约定的Schema关键字位置遍历布尔或对象Schema，把`$ref`/`$dynamicRef`解析为引用边并产出资料包：

```sh
python -m source_data.cli collect data/closure.json -o out/closure-pack
python -m source_data.cli collect data/gaps.json -o out/gaps-pack
```

资料包布局（原文`$id`与原始`$ref`一律不改写）：

- `manifest.json`：入口、允许来源、完整标志、文档/资源/锚登记、引用边、资源图、缺口清单。
- `uri-map.json`：request URI与final URI两种身份到`documents/<file>`的重定位映射。
- `documents/`：按原始字节封存的Schema文档，附SHA-256摘要与重定向链。

三种身份分别保存，不合并为一个缓存键：抓取地址（request URI，去fragment）、最终响应地址（final URI，重定向后）、Schema资源URI（由`$id`决定，嵌套`$id`相对当前base解析并建立独立资源边界）。重定向后的相对引用按最终文档位置与嵌套`$id`解析。

### 引用边与缺口（合成夹具的演示点）

- 嵌套`$id`：`root.json`的`$defs/reading`以相对`$id`成为独立资源`parts/reading.json`，文档Pointer与资源身份同时保留。
- 循环：`reading -> cycle -> reading`跨资源循环、`parts/node.json`的`$dynamicRef #node`自环，均保存为`resource_graph`的边，不递归内联。
- 百分号片段：`#/%24defs/thing`先percent decode再按Pointer定位，与`#/$defs/thing`收敛到同一目标；锚片段`#%73haredAnchor`解码后查锚索引。
- 转义斜杠/波浪号：`a~1b`还原为属性名`a/b`，`a~0b`还原为`a~b`。
- 重复锚与重复资源URI：`dup-anchor.json`同名锚、`twin-one/two.json`相同`$id`报歧义缺口，不按先到者覆盖。
- examples/default/自定义字段伪引用：其中的`$ref`作为普通JSON原值保留，不产生边、不触发下载（见`root.json`、`links*.json`）。
- 缺口严格区分：越出允许来源（`origin_not_allowed`）、文档缺失（`document_unavailable`/`target_document_missing`）、Pointer下标语法错误、下标越界、属性缺失、穿过标量、锚缺失、身份冲突。任一缺口都会让资料包`complete=false`，已收集原文仍可查看。

`$dynamicRef`只记录静态初始目标、动态标记与同名`$dynamicAnchor`的资源候选索引，不计算实例相关的运行时动态作用域，也不执行Schema校验；资料包保留后续独立验证器所需的原始结构。

## 禁止网络的本地重放

`replay`只读取资料包，用与收集阶段相同的URI/Pointer/锚语义从封存字节重新定位每个目标，并与manifest逐条比对：

```sh
# 全闭包重放：核对字节摘要后重定位全部边
python -m source_data.cli replay out/closure-pack
# 单条原始引用，按其所在资源base重定位
python -m source_data.cli replay out/closure-pack \
  --ref 'base.json#/$defs/a~1b' \
  --base 'https://dict.example.invalid/schema/root.json'
```

测试通过把`socket.socket`替换为立即失败的桩，证明重放路径不发生任何网络调用；重定位经`uri-map.json`访问封存原文，资源语义保持不变。

`inspect`展示已有的原始库存，不修改资料。`invalid-`前缀文件为不合法输入，其他变体保留不同业务边界；源协议与二进制格式使用资料约定中的有限范围，未解释部分仍以原字节保存。

固定基础镜像见Dockerfile，可执行`docker build -t energy-source .`，再用`docker run --rm energy-source`显示摘要；`docker run --rm energy-source python -m unittest discover -s tests -v`执行已有检查。资料和回放不依赖真实设备、账户或实时网络服务。
