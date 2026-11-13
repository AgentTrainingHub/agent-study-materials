# comma_keep 列表合同

适用条件：仅用于 comma_keep；不用于分号版或去重版。

parse_ports(text) 接受非空 str，以英文逗号 , 分隔。每项按 parse_port 合同校验，保留输入顺序和全部重复项；不去除空白。出现空项或非法项时整次调用抛 ValueError，不返回部分列表。

```python
parse_ports("80,443,80") == [80, 443, 80]
```
