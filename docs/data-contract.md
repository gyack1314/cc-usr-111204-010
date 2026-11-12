# JSON Schema资源资料

采用2020-12 URI资源模型，$id不允许非空片段。遍历的Schema位置为$defs/properties/patternProperties/dependentSchemas的值、allOf/anyOf/oneOf/prefixItems的元素，以及additionalProperties、unevaluatedProperties、propertyNames、items、contains、not、if、then、else、contentSchema的Schema值。其余数据原封保留但不当Schema依赖遍历；不支持自定义vocabulary中的新Schema位置时明确范围限制。

片段是URI fragment，先一次percent decode为Unicode，再处理Pointer路径和~1/~0，Pointer数组索引为0或无前导零正整数，-不指向存在元素。锚名遵从2020-12语法，在所属资源根建立索引。资源位置由所在文档的JSON Pointer保存，嵌套$id改变base并开新资源；同文档中指向嵌套资源的Pointer应保留文档位置与资源身份的对应，不能把Pointer目标错当作外层资源根。

记录request URI、最终响应URI、redirect chain及body SHA；获取地址去fragment，资源URI由$id决定。离线URI映射返回原文与原最终URI，使base解析不因存储路径改变。$dynamicRef保留初始静态目标和动态标记，不计算实例相关最终解析。当前domain只列JSON文档，不收集引用闭包。
