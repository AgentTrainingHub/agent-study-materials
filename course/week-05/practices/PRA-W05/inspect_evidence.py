"""从原始流、会话、审批、文件与容器核对 W05；保留答案语义人工核对。"""
import argparse
import importlib.util
import json
from pathlib import Path

MATERIALS = Path(__file__).resolve().parent
W04 = MATERIALS.parents[2] / 'week-04/practices/PRA-W04'
LIMITS = {'A': 8, 'B': 4, 'C': 4}


def load_previous():
    spec = importlib.util.spec_from_file_location('w04_inspector', W04 / 'inspect_evidence.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def stream_object(path, old):
    data = path.read_bytes().decode('utf-8').replace('\r\n', '\n')
    old.require(data.endswith('\n\n'), 'SSE 没有完整终帧')
    text, tools, identity, model, finish, usage, done = '', {}, None, None, None, None, False
    for frame in data.split('\n\n'):
        fields = [line[5:].removeprefix(' ') for line in frame.split('\n') if line.startswith('data:')]
        if not fields: continue
        payload = '\n'.join(fields)
        old.require(not done, 'DONE 后仍有数据')
        if payload == '[DONE]': done = True; continue
        chunk = old.decode(payload)
        old.require(isinstance(chunk, dict), 'SSE 数据不是对象')
        for name, current in [('id', identity), ('model', model)]:
            value = chunk.get(name)
            old.require(not value or current is None or value == current, f'{name} 在流中改变')
        identity, model = chunk.get('id') or identity, chunk.get('model') or model
        if chunk.get('usage') is not None: usage = chunk['usage']
        choices = chunk.get('choices', [])
        old.require(isinstance(choices, list), 'choices 不是数组')
        if not choices: continue
        old.require(len(choices) == 1 and isinstance(choices[0], dict)
                    and type(choices[0].get('index')) is int and choices[0]['index'] == 0, '流的 choices 无效')
        choice = choices[0]; delta = choice.get('delta', {})
        old.require(isinstance(delta, dict) and delta.get('role') in (None, 'assistant'), '流的角色或delta无效')
        old.require(delta.get('content') is None or isinstance(delta['content'], str), '文本增量不是字符串')
        text += delta.get('content') or ''
        if choice.get('finish_reason'): finish = choice['finish_reason']
        for part in delta.get('tool_calls') or []:
            old.require(isinstance(part, dict) and type(part.get('index')) is int and part['index'] >= 0
                        and part.get('type') in (None, 'function'), '工具增量索引或类型无效')
            item = tools.setdefault(part['index'], {'id': None, 'type': 'function',
                                                   'function': {'name': '', 'arguments': ''}})
            if part.get('id'):
                old.require(item['id'] in (None, part['id']), '工具 id 改变')
                item['id'] = part['id']
            for name in ('name', 'arguments'):
                item['function'][name] += part.get('function', {}).get(name) or ''
    old.require(done and finish in {'stop', 'tool_calls'}, '真实流未正常完整结束')
    old.require(isinstance(identity, str) and identity.strip() and isinstance(model, str) and model.strip(), '实际响应身份缺失')
    message = {'role': 'assistant', 'content': text}
    if tools:
        old.require(sorted(tools) == list(range(len(tools))), '工具索引不连续')
        message['tool_calls'] = [tools[i] for i in sorted(tools)]
        old.require(len({item['id'] for item in tools.values()}) == len(tools)
                    and all(isinstance(item['id'], str) and item['id'].strip() for item in tools.values()), '工具调用ID缺失或重复')
    return {'id': identity, 'model': model, 'message': message, 'finish_reason': finish, 'usage': usage}


def inspect(batch):
    old = load_previous(); w03 = old.load_w03_inspector(); require = old.require
    experiment = old.read_json(batch / 'experiment.json')
    require(experiment.get('limits') == LIMITS and experiment.get('maximum_model_requests') == 16
            and experiment.get('automatic_retries') == 0
            and experiment.get('evidence_source') == 'live-external-api', '真实采集身份或请求预算不同')
    rows = old.read_json(batch / 'summary.json')
    require([r['phase'] for r in rows] == list(LIMITS), '缺少实验阶段')
    results, total = [], 0
    previous = None
    for row in rows:
        phase = row['phase']; calls = row.get('model_calls')
        require(type(calls) is int and 0 <= calls <= LIMITS[phase], '请求计数无效或超限')
        total += calls
        expected_status = 'cancelled' if phase == 'C' else 'stop'
        if row.get('status') != expected_status or row.get('supervisor_timeout'):
            results.append({'phase': phase, 'coverage': 'blocked' if row.get('status') == 'blocked'
                            else 'not_covered', 'status': row.get('status'), 'error': row.get('error')})
            continue
        directory = batch / phase
        result = old.read_json(directory / 'stdout.json')
        invocation = old.read_json(directory / 'invocation.json')
        args = invocation['argv']
        require(result['pid'] == invocation['pid'] == row['pid']
                and invocation['phase'] == phase and invocation['model_request_limit'] == LIMITS[phase]
                and args.count('--max-turns') == 1
                and args[args.index('--max-turns') + 1] == str(LIMITS[phase]), '实际进程或启动预算不同')
        require(result['evidence_source'] == 'live-external-api', '运行来源不是本组真实调用')
        require(all(row.get(k) == v for k, v in result.items()), '采集摘要与 CLI 输出不同')
        require(result['exit_code'] == int((directory / 'exit-code.txt').read_text()), '实际退出码不同')
        require(result['exit_code'] == (1 if phase == 'C' else 0), '阶段终态与退出码不同')
        run = directory / 'runs' / result['run_id']
        require(Path(result['output_directory']).resolve() == run and old.read_json(run / 'result.json') == result,
                '运行结果目录或内容不同')
        events = old.lines(run / 'events.jsonl')
        require([x['seq'] for x in events] == list(range(1, len(events) + 1)), '事件序号不连续')
        require(all(x['run_id'] == result['run_id'] and x['session_id'] == result['session_id']
                    and x['evidence_source'] == 'live-external-api' for x in events), '事件归属或来源不同')
        require(events[0]['event'] == 'run_started' and events[-1]['event'] == 'run_finished'
                and events[-1]['status'] == result['status'], '事件没有匹配的起止')
        require([x['elapsed_ms'] for x in events] == sorted(x['elapsed_ms'] for x in events), '单调时间倒退')
        for event in events:
            if event['event'] in {'model_requested','text_delta','tool_arguments_delta','model_returned'}:
                require(event['span_id'] == f"model:{event['turn']}" and event['parent_span_id'] == 'run',
                        '模型事件的 span 归属错误')
            elif event.get('call_id'):
                require(event['span_id'] == f"tool:{event['turn']}:{event['call_id']}", '工具事件归属错误')
        turns = sorted(run.glob('turn-*/request.json'))
        require(len(turns) == calls == len([e for e in events if e['event'] == 'model_requested']), '请求计数不同')
        segmented = False
        for request_path in turns:
            request = old.read_json(request_path)
            require(request.get('stream') is True and request.get('stream_options') == {'include_usage': True}
                    and request.get('thinking') == {'type': 'disabled'}, '实际请求参数不同')
            receipt = old.read_json(request_path.parent / 'receipt.json')
            arrivals = old.read_json(request_path.parent / 'arrivals.json')
            require([a['seq'] for a in arrivals] == list(range(1, len(arrivals) + 1)), '接收序号不连续')
            require(all(type(a['byte_count']) is int and a['byte_count'] > 0 for a in arrivals), '接收字节计数无效')
            require(all(type(a['elapsed_ms']) is int and a['elapsed_ms'] >= 0 for a in arrivals)
                    and [a['elapsed_ms'] for a in arrivals] == sorted(a['elapsed_ms'] for a in arrivals), '接收时序无效')
            segmented |= len({a['elapsed_ms'] for a in arrivals}) >= 2
            rebuilt = stream_object(request_path.parent / 'stream.raw', old)
            raw = old.read_json(request_path.parent / 'raw-response.json')
            require(rebuilt['id'] == raw['id'] and rebuilt['model'] == raw['model']
                    and rebuilt['message'] == raw['choices'][0]['message']
                    and rebuilt['finish_reason'] == raw['choices'][0]['finish_reason']
                    and rebuilt['usage'] == raw.get('usage'), '独立汇总原始流与保存对象不同')
            require(receipt['done_seen'] is True and receipt['http_status'] == 200
                    and receipt['error'] is None and receipt['arrival_count'] == len(arrivals), '回执与实际流不同')
            usage = rebuilt['usage'] if isinstance(rebuilt['usage'], dict) else {}
            values = {name: usage[name] if type(usage.get(name)) is int and usage[name] >= 0 else None
                      for name in ('prompt_tokens', 'completion_tokens', 'total_tokens')}
            known = (all(value is not None for value in values.values())
                     and values['prompt_tokens'] + values['completion_tokens'] == values['total_tokens'])
            require(receipt['usage'] == {'usage_status': 'known' if known else 'usage_unknown', **values}, 'usage回执与原始流不同')
        before, after = old.snapshot(batch / f'before-{phase}', w03), old.snapshot(batch / f'after-{phase}', w03)
        finished = {(e['turn'], e['call_id']): e for e in events if e['event'] == 'tool_finished'}
        require(len(finished) == len([e for e in events if e['event'] == 'tool_finished']), '工具结束事件重复')
        returned = {e['turn']: e['seq'] for e in events if e['event'] == 'model_returned'}
        require(all(e['turn'] in returned and e['seq'] > returned[e['turn']]
                    for e in events if e['event'] == 'tool_started'), '完整模型响应之前已经派发工具')
        if phase in {'A','B'}:
            index, _ = old.journal_index(old.lines(directory / 'after-session.jsonl'))
            finished, visible, sources = old.inspect_requests(run, result, events, index, 'W05_NEVER_IN_INPUT_941c')
            started, completed, approvals = w03.inspect_proposals(directory, run, events, finished, before)
            if phase == 'A':
                require({(x['tool_name'], x['path']) for x in completed}
                        >= {('edit','validator.py'),('write','test_validator.py')}, '未完成两种修改')
                latest = {x['path']: x['after'] for x in completed}
                require(all(after[k] == v for k, v in latest.items()), '实际文件不等于批准候选')
                shells = [w03.inspect_shell(e,run) for e in finished.values() if e['tool_name'] == 'shell' and e['executed']]
                last = shells[-1] if shells else None
                checked_matches = bool(last and set(last['inputs']) == w03.FILES
                                       and all(last['inputs'][name] == after[name] for name in w03.FILES))
                derived = {'visible_read': bool(visible), 'confirmed_mutation': bool(completed),
                           'last_check_succeeded': bool(last and last['check_passed']),
                           'new_tests_ran': bool(last and last['new_tests_ran']),
                           'fixed_checks_passed': bool(last and last['fixed_checks_passed']),
                           'checked_version_matches': checked_matches}
                require(result['process_gates'] == derived and all(derived.values()), '过程声明与真实文件、输入版本或检查结果不同')
                require(result['mutation_successes'] == len(completed)
                        and row['approval_count'] == len(approvals), '批准或提交计数与独立证据不同')
                observations = old.read_json(directory / 'viewer-observations.json')
                require([x['action'] for x in observations] == ['closed','reopened']
                        and all(x['runner_alive'] for x in observations), '未在运行期间关闭并重开查看者')
                cut = observations[0]['cursor']['last_seq']
                shown1, shown2 = old.lines(directory/'viewer-1/stdout.jsonl'), old.lines(directory/'viewer-2/stdout.jsonl')
                require(shown1 and shown2 and shown1[-1]['seq'] == cut and shown2[0]['seq'] == cut+1
                        and shown1+shown2 == events, '查看显示有遗漏、重复或内容不同')
                require(all(int((directory/f'viewer-{i}/exit-code.txt').read_text()) == 0 for i in (1,2)), '查看者未正常退出')
                require(segmented, '未命中不同时间的真实字节接收')
                previous = result
            else:
                require(before == after and not approvals and not started
                        and all(e['tool_name'] == 'read' for e in finished.values()), '只读续聊出现其他工具效果')
                require(previous and result['session_id'] == previous['session_id']
                        and result['run_id'] != previous['run_id'] and result['pid'] != previous['pid'], '续聊身份或进程不正确')
                require(previous['head_id'] in index, '续聊没有历史父链')
                prior_ids = {e['entry_id'] for e in old.lines(batch/'A/after-session.jsonl') if e['type'] in {'header','message'}}
                require(prior_ids <= sources[0], '续聊请求缺少先前完整消息')
        else:
            require(before == after, '取消组修改了主工作区')
            trigger = old.read_json(directory/'cancel-trigger.json')
            cancels = [e for e in events if e['event'] == 'cancel_requested']
            require(len(cancels) == 1 and cancels[0]['seq'] > trigger['events_seq_before_signal'], '取消未在真实慢工具开始后记录')
            require((directory/'slow-marker-before-signal.txt').read_bytes() and row['cancel_signal_sent'], '缺少真实触发证据')
            require(not any(e['seq'] > cancels[0]['seq'] and e['event'] in {'model_requested','tool_started','commit_started'}
                            for e in events), '取消后有新派发')
            shells = [e for e in finished.values() if e['tool_name'] == 'shell' and e['executed']]
            require(len(shells) == 1, '取消组没有唯一实际检查')
            shell = shells[0]['result']; lifecycle = old.read_json(Path(shell['artifact_dir'])/'container.json')
            independent_shell = w03.inspect_shell(shells[0], run)
            require(independent_shell['cleanup_confirmed'] and shell['cancelled']
                    and shell['original_status'] == 'cancelled' and lifecycle['profile'] == 'slow'
                    and lifecycle['stopped_confirmed']
                    and lifecycle['removed_confirmed'], '容器收尾未确认')
            manifest = old.read_json(Path(shell['artifact_dir'])/'manifest.json')
            require(manifest['main_workspace_published'] is False and manifest['effect_scope'] == 'container_copy', '容器结果被发布到主工作区')
            entries = old.lines(directory/'after-session.jsonl')
            require(entries[-1]['type'] == 'run_end' and entries[-1]['status'] == 'cancelled', '取消会话未保留阻断终态')
            # 用正常终态的临时结构副本复用父链校验，原始取消状态已经单独核对。
            structural = [dict(entry) for entry in entries]
            structural[-1]['status'] = 'stop'
            index, head = old.journal_index(structural)
            require(head == result['head_id'] and entries[-1]['run_id'] == result['run_id'], '取消终态归属不同')
            for turn, request_path in enumerate(turns, 1):
                projection = old.read_json(run / f'projection-{turn:03d}.json')
                request = old.read_json(request_path)
                sources = projection['source_ids']
                require(sources and sources[-1] in index, '取消组请求缺少消息来源')
                messages, expected_sources = old.project(index, sources[-1])
                require(projection['messages'] == request['messages'] == messages and sources == expected_sources
                        and projection['session_id'] == result['session_id']
                        and projection['run_id'] == result['run_id'], '取消组请求不等于完整父链投影')
                old.check_pairs(messages)
            marker = (directory/'slow-marker-before-signal.txt').read_bytes()
            artifact = Path(shell['artifact_dir'])
            require(Path(trigger['marker_path']).resolve() == artifact/'copy/marker.txt'
                    and trigger['runner_pid'] == result['pid'] and trigger['bytes_observed'] == len(marker)
                    and marker.startswith(b'heartbeat pid=')
                    and (artifact/'output/marker.txt').read_bytes().startswith(marker), '取消触发与实际慢工具导出不一致')
        results.append({'phase': phase, 'coverage': 'hit', 'model_calls': calls,
                        'usage': result['usage'], 'semantic_verdict': 'unevaluated'})
    require(total <= 16, '超出全组请求预算')
    return {'coverage_complete': all(x['coverage'] == 'hit' for x in results),
            'actual_model_calls': total, 'phases': results,
            'semantic_verdict': 'requires-independent-review'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--batch', type=Path, required=True)
    args = parser.parse_args()
    result = inspect(args.batch.resolve(strict=True))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['coverage_complete'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
