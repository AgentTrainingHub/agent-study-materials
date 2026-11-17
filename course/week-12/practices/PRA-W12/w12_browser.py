"""真实独立 Chrome、原始采集和传输；许可与业务判据由本地模块提供。"""
from __future__ import annotations
import argparse, hashlib, importlib.util, json, os, signal, sys, time, uuid
from datetime import datetime,timezone
from pathlib import Path
from playwright.sync_api import Error, sync_playwright
from w12_io import BrowserError,append,canonical,decode,save,process_identity

class BrowserOwner:
    def __init__(self,directory,manifest):
        self.directory=Path(directory);self.manifest=manifest;self.page=None;self.browser=None;self.context=None;self.pw=None
        self.authorize=None;self.handles={};self.requests=[];self.responses={};self.errors=[];self.captures=0;self.blocked_sockets=[]
    def start(self):
        self.pw=sync_playwright().start()
        self.browser=self.pw.chromium.launch(channel=self.manifest.get('browser_channel','chrome'),headless=True,
            env={k:v for k,v in os.environ.items() if k in {'HOME','PATH','TMPDIR','LANG','LC_ALL','LC_CTYPE'}})
        cdp=self.browser.new_browser_cdp_session();procs=cdp.send('SystemInfo.getProcessInfo')['processInfo'];cdp.detach()
        pid=next(v['id'] for v in procs if v['type']=='browser')
        self.metadata={'browser_pid':pid,'browser_pgid':os.getpgid(int(pid)),'process_identity':process_identity(pid),'worker_pid':os.getpid(),'browser_version':self.browser.version,
                       'channel':self.manifest.get('browser_channel','chrome'),'persistent_profile':False,'service_workers':'block'}
        save(self.directory/'browser-start.json',self.metadata)
        self.context=self.browser.new_context(service_workers='block',accept_downloads=False,viewport={'width':1000,'height':760})
        self.context.route('**/*',self.route);self.context.route_web_socket('**/*',self.block_ws)
        self.context.on('page',self.extra_page)
        self.page=self.context.new_page();self.page.set_default_timeout(2000);self.page.set_default_navigation_timeout(7000)
        self.page.on('dialog',lambda dialog:dialog.dismiss());self.page.on('download',lambda download:download.cancel())
        self.metadata.update(cookies=self.context.cookies(),storage_state=self.context.storage_state())
        save(self.directory/'browser-start.json',self.metadata);return self.metadata
    def extra_page(self,page):
        if self.page is not None and page!=self.page:
            append(self.directory/'network.jsonl',{'kind':'extra_page','url':page.url,'allowed':False});page.close()
    def block_ws(self,ws):
        fact={'kind':'websocket','url':ws.url,'allowed':False};self.errors.append(fact);append(self.directory/'network.jsonl',fact)
        # close 等待模拟连接完成；路由回调返回后再关闭，始终不 connect_to_server。
        self.blocked_sockets.append(ws)
    def route(self,route):
        request=route.request
        try:main_frame=self.page is not None and request.frame==self.page.main_frame
        except Error:main_frame=False
        facts={'url':request.url,'method':request.method,'body':request.post_data or '',
               'main_frame':main_frame,'resource_type':request.resource_type,'time':time.time()}
        try:
            allowed=self.authorize(facts) if self.authorize else False
            facts['allowed']=bool(allowed)
            if not allowed:
                facts['reason']='request_not_allowed';self.errors.append(facts.copy());route.abort();return
            facts['dispatch_started']=True;self.requests.append(facts.copy())
            response=route.fetch(max_redirects=0,max_retries=0,timeout=3000)
            try:
                body=response.body();facts.update(status=response.status,response_bytes=len(body))
                if 300<=response.status<400:raise BrowserError('redirect_refused','重定向被拒绝。')
                if len(body)>65536:raise BrowserError('response_too_large','响应超过 64 KiB。')
                self.responses[request.url]={'status':response.status,'body':body.decode('utf-8'),'url':request.url,'time':time.time()}
                route.fulfill(response=response,body=body)
            finally:response.dispose()
        except (Error,BrowserError) as exc:
            facts['error']={'class':getattr(exc,'code','transport_error'),'message':str(exc)};self.errors.append(facts.copy())
            try:route.abort()
            except Error:pass
        finally:append(self.directory/'network.jsonl',facts)
    def tick(self):
        self.page.wait_for_timeout(250)
        while self.blocked_sockets:
            ws=self.blocked_sockets.pop(0);ws.close()
            append(self.directory/'network.jsonl',{'kind':'websocket_closed','url':ws.url,'allowed':False})
    def open(self,path):
        self.page.goto(self.manifest['origin']+path,wait_until='domcontentloaded');self.tick()
    def state(self):
        return self.page.evaluate('''()=>({html:document.documentElement.outerHTML,values:Array.from(document.querySelectorAll('input')).map(e=>({name:e.name,value:e.value,disabled:e.disabled})),url:location.href})''')
    def raw_capture(self):
        self.tick();before=self.state();self.captures+=1;directory=self.directory/f'observation-{self.captures:03d}';directory.mkdir()
        handles={};nodes=[]
        for element in self.page.query_selector_all('[data-node]'):
            h='handle_'+uuid.uuid4().hex;handles[h]=element
            info=element.evaluate('''e=>({node_id:e.dataset.node,scheme_id:e.dataset.scheme||null,role:e.getAttribute('role')||(e.tagName==='BUTTON'?'button':e.tagName==='A'?'link':'textbox'),name:e.getAttribute('aria-label')||e.innerText||e.name,value:e.value||'',disabled:!!e.disabled,connected:e.isConnected})''')
            nodes.append({**info,'handle_id':h})
        aria=self.page.locator('body').aria_snapshot();session=self.context.new_cdp_session(self.page)
        ax=session.send('Accessibility.getFullAXTree');session.detach();ax_at=time.time()
        self.page.screenshot(path=str(directory/'screenshot.png'),caret="initial");screen_at=time.time()
        metadata=self.page.locator('#source-meta').text_content();body=self.page.locator('#source-body').text_content()
        after=self.state();connected=all(e.evaluate('e=>e.isConnected') for e in handles.values())
        (directory/'dom.html').write_text(before['html']);(directory/'aria.yml').write_text(aria);(directory/'source.txt').write_text(body)
        save(directory/'ax.json',ax);save(directory/'state.json',after)
        page_facts=self.page.evaluate('({inputEvents:window.inputEvents,clickEvents:window.clickEvents,attackEvents:window.attackEvents||[],serviceWorkers:navigator.serviceWorker?null:"unavailable"})')
        save(directory/'page-events.json',page_facts)
        raw={'snapshot':before,'stable':before==after and connected,'nodes':nodes,'meta':decode(metadata),'text':body,
             'url':self.page.url,'title':self.page.title(),'time':datetime.now(timezone.utc).isoformat(),
             'aria_at':ax_at,'screenshot_at':screen_at,'directory':str(directory)}
        self.handles.update(handles);save(directory/'raw.json',raw);return raw
    def current(self,raw):
        self.tick()
        if self.state()!=raw['snapshot']:return False
        return all(self.handles[n['handle_id']].evaluate('e=>e.isConnected') for n in raw['nodes'])
    def fill_action_id(self,value):
        self.page.locator('input[name="action_id"]').evaluate('(e,v)=>e.value=v',value)
    def raw_act(self,handle_id,action,value=None):
        element=self.handles[handle_id]
        if action=='fill':element.fill(value)
        else:element.click()
        self.tick()
        save(self.directory/'ui-events.json',self.page.evaluate('({input:window.inputEvents,click:window.clickEvents,status:document.querySelector("#status").textContent})'))
    def query_page(self,action_id):
        url=self.manifest['origin']+'/state?action_id='+action_id
        self.page.goto(url,wait_until='domcontentloaded')
        raw=self.responses.get(url);rendered=self.page.locator('#receipt').text_content()
        source=self.page.locator('#receipt-json').text_content()
        directory=self.directory/f'query-{len(list(self.directory.glob("query-*")))+1:02d}';directory.mkdir()
        (directory/'dom.html').write_text(self.page.content());self.page.screenshot(path=str(directory/'screenshot.png'),caret="initial")
        result={'http':raw,'rendered':rendered,'receipt':decode(source),'url':url,'directory':str(directory)}
        save(directory/'response.json',result);return result
    def close(self):
        errors=[]
        for obj in (self.context,self.browser):
            if obj:
                try:obj.close()
                except Error as e:errors.append(str(e))
        if self.pw:
            try:self.pw.stop()
            except Error as e:errors.append(str(e))
        save(self.directory/'owner-close.json',{'errors':errors,'closed_at':time.time()})

def main():
    p=argparse.ArgumentParser();p.add_argument('--entry',type=Path,required=True);p.add_argument('--directory',type=Path,required=True);a=p.parse_args()
    sys.path.insert(0,str(a.entry.parent));spec=importlib.util.spec_from_file_location('student_browser',a.entry.parent/'browser.py');module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    owner=None;session=None
    signal.signal(signal.SIGTERM,lambda *_:(_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        for line in sys.stdin:
            message=decode(line);method=message['method'];args=message['args'];identifier=message['id']
            if method=='close':break
            try:
                if method=='init':
                    owner=BrowserOwner(a.directory,args['manifest']);session=module.BrowserSession(args['manifest'],args['task'],owner,session_id=args['session_id'],run_id=args['run_id'])
                    owner.authorize=session.authorize_request;result=owner.start()
                elif method=='tool':result=session.tool(args['name'],args['arguments'])
                elif method=='commit':result=session.commit_act(args['candidate_id'],args['digest'],args['approved'],actor=args['actor'])
                elif method=='validate':result=session.validate_final(args)
                elif method=='stats':result=session.stats()
                else:raise BrowserError('worker_protocol','未知 worker 方法。')
                reply={'id':identifier,'ok':True,'result':result}
            except Exception as e:reply={'id':identifier,'ok':False,'error':{'class':getattr(e,'code','browser_error'),'message':str(e)}}
            print(canonical(reply),flush=True)
    except KeyboardInterrupt:pass
    finally:
        if owner:owner.close()
if __name__=='__main__':main()
