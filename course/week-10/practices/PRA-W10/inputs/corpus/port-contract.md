# parse_port 单端口合同

parse_port(text) 只接受非空 str，字符必须全部属于 ASCII 数字 0 到 9。字符串不得有前导零，转换后的整数必须在闭区间 1 到 65535。任一条件不满足均抛 ValueError；不去掉首尾空白，不接受正负号。

```python
parse_port("80") == 80
parse_port("65535") == 65535
# "080"、"１２"、None 均应抛 ValueError
```
