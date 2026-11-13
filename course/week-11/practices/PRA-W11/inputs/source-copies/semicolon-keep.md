# semicolon_keep 列表合同

适用条件：仅用于 semicolon_keep；这是当前资料问答的主合同。

parse_ports(text) 接受非空 str，以英文分号 ; 分隔。每项按 parse_port 合同校验，保留输入顺序和全部重复项；不去除空白。出现空项或非法项时整次调用抛 ValueError，不返回部分列表。逗号输入不属于本合同的合法列表。

```python
parse_ports("80;443;80") == [80, 443, 80]
```
