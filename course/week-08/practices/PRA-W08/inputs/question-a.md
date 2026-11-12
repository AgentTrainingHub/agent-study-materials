先在同一个工具调用批次读取 validator.py 和 test_validator.py，两个 read 都指定 offset=1、limit=80。当前 parse_port 已限制 ASCII 数字、1～65535 闭区间及无前导零，现在增加要求：只接受 str；None、bool、int、float、bytes、list 等所有非字符串都必须抛 ValueError。原合法字符串及非法字符串的规则保持。

只用小范围 edit 修改 validator.py 和 test_validator.py，补充非字符串回归测试，每笔等待批准。随后调用固定 shell check；失败时根据实际反馈修复并重新检查。检查成功后，单独一批调用 submit_result，text 用一句中文概括实际通过的检查，等待批准。取得回执后简短说明实际修改、测试和提交回执，不再调用工具；没有做过的检查不要写成已完成。
