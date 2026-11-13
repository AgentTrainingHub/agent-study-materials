# comma_unique 列表合同

适用条件：仅用于 comma_unique；不用于保留重复项的合同。

parse_ports(text) 接受非空 str，以英文逗号 , 分隔。每项先按 parse_port 合同校验，随后只保留每个端口第一次出现的位置，输出顺序不变；非法项使整次调用抛 ValueError。

```python
parse_ports("80,443,80") == [80, 443]
```
