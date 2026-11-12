查询 rules 服务的 port-format/r1 和 checks 服务的 port-checks/r1，两个资料都通过各自的 lookup 取得，并结合实际文件确认新增要求。

将 validator.py 的 parse_port 更新到资料中的表示规则，保留原 ASCII 数字与 1～65535 闭区间规则；补充 test_validator.py 的回归测试。只修改这两个文件，使用小范围 edit，每笔等待批准。读取文件时指定 offset=1、limit=80；无须重复读取未改变的内容。

修改后调用固定 shell check。最后说明资料来源与 revision、实际修改、检查结果及仍未覆盖的输入；只引用实际看到的事实，回答保持简短。
