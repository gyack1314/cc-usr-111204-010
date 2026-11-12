# JSON Schema资源资料

采用2020-12 URI资源模型，$id不允许非空片段（违例登记`invalid_id`缺口并按去片段URI容忍）。遍历的Schema位置为$defs/properties/patternProperties/dependentSchemas的值、allOf/anyOf/oneOf/prefixItems的元素，以及additionalProperties、unevaluatedProperties、propertyNames、items、contains、not、if、then、else、contentSchema的Schema值；布尔Schema按叶子处理，不展开内部关键字。examples、default及任何未知自定义字段原封保留为普通JSON原值，其中出现的`$ref`不扫描、不下载、不产生依赖边。不支持自定义vocabulary中的新Schema位置，范围以此处列举为限。

## 资源身份与边界

片段是URI fragment，先一次percent decode为Unicode，再处理Pointer路径和~1/~0，Pointer数组索引为0或无前导零正整数（`01`判为`pointer_bad_index`），越界判为`pointer_out_of_range`，对象属性缺失判为`pointer_missing_property`，穿过非容器标量判为`pointer_through_scalar`，四类严格区分，`-`不指向存在元素。锚名遵从2020-12语法，在所属资源根建立索引；锚片段同样先percent decode再按名称查锚。空片段指向资源根，具名片段定位锚。

资源位置由所在文档的JSON Pointer保存。嵌套$id相对当前base解析并开新资源，登记项同时保存文档request地址、文档内Pointer与资源URI；同文档中指向嵌套资源的Pointer保留文档位置与资源身份的对应，不能把Pointer目标错当作外层资源根。相同绝对资源URI在不同定义位置重复（含不同文档声明同一$id但内容不同）报`duplicate_resource_id`，同资源同名$anchor或$dynamicAnchor在不同位置重复报`duplicate_anchor`，均不按先到者覆盖。

## 三种身份与抓取

记录request URI（去fragment的抓取地址）、最终响应URI（重定向后）、redirect chain及body SHA-256；资源URI由$id决定，无$id的文档（含布尔根）资源URI取最终响应地址。三个身份分别保存，不能合并成一个缓存键：同一request URI只抓取一次，重复抓取直接报错；uri-map同时为request URI和final URI建立到同一封存文件的映射。重定向后的相对引用按最终文档位置及嵌套$id解析。目标文档不在已抓取集合、但其URI恰好是已登记的嵌套资源时，定位到该资源所属文档而不是再次抓取。

## 引用边、动态引用与图

$ref按URI解析：先去片段定位文档，片段再按空片段/锚/JSON Pointer三种形态处理。以资源URI和目标Pointer建立引用边；循环和共享定义保存为resource_graph（邻接表，目标为资源URI+Pointer二元组），不递归内联，共享定义的多条边收敛到同一目标节点。

$dynamicRef先收集静态初始目标：锚形式按锚索引定位，Pointer形式按普通$ref静态解析。边上保留`dynamic=true`标记、原始引用字符串、静态目标，以及同名$dynamicAnchor的资源候选索引；静态初始目标不冒称为所有运行时上下文的最终目标，也不因此执行实例校验，资料包保留后续独立验证器决定动态作用域所需的原始结构。

## 完整性与离线重放

整个闭包任一引用越出允许范围（`origin_not_allowed`）、目标文档缺失（`document_unavailable`/`target_document_missing`）、锚或Pointer目标缺失、文档内容身份冲突，都让资料包`complete=false`，缺口逐条进入manifest的gaps，已收集原文与已解析边仍可查看。

离线重放通过uri-map访问封存原文并核对字节摘要，保持$id及原始$ref不变，不为本地路径改写资源语义；重放用同一套URI/Pointer/锚规则重新定位每条边并与manifest目标比对，全部一致才算重放完整。重放模块仅依赖标准库的解析部分（urllib.parse），不导入任何网络请求模块；测试以socket桩证明重放期间禁止网络。当前domain库存摘要仍只列JSON文档，不解析引用闭包，闭包能力由schema_closure/archive/offline_replay三个模块承担。
