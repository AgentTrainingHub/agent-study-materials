增加 parse_ports(text)->list[int]，只修改 validator.py 和 test_validator.py，保留原 parse_port 合同及全部既有测试。
列表接受非空 str，逗号分隔，不去空白、不许空项，每项调用 parse_port。重复项尚未确定：80,443,80 返回 [80,443,80] 或 [80,443]，修改前用 question 询问。标题自行决定。
先 read 两文件（limit=80），用 plan 保存四步：核对原合同、确定列表条件、实现并补测试、运行检查；原合同独立，写清依赖和来源。可同批提出 plan 与 question。
答案只确定重复项策略。优先局部 edit，每批一份候选，逐笔等待批准。新增重复项、空项、单项错误、非 str 测试，shell check 实际文件；按依赖顺序用 plan record 引用当前事实来源。最后给返回示例、修改与检查结论、仍需人工判断的部分。
