"""Loopback-only workbench; bounded CSRF-protected actions, no trading or arbitrary paths."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import secrets
import threading
from urllib.parse import parse_qs

from . import research_product as product
from .publisher import read_current
from .research_product_view import server_page, render
import json
from pathlib import Path


def service_identity(root, output):
    """Bind local service reuse to its workspace and executable UI surface."""
    from .domain import identity, file_hash
    folder=Path(__file__).parent
    files=('research_product_server.py','research_product.py','research_product_view.py',
           'research_journal.py','research_followup.py','observation_workspace.py','observation_capture.py','publisher.py')
    return {'protocol':'stock-data-workspace-v1',
            'workspace_id':identity({'root':str(Path(root).resolve()).casefold(),
                                     'output':str(Path(output).resolve()).casefold()}),
            'surface_sha256':identity({**{name:file_hash(folder/name) for name in files},
                'source_authority.py':file_hash(folder.parent/'source_authority.py'),
                'quote_transport.py':file_hash(folder.parent/'quote_transport.py')})}


def handler(root, output, port, *, shutdown=None):
    root=Path(root);output=Path(output)
    startup_identity=service_identity(root,output)
    token=secrets.token_urlsafe(32)
    state={'running':False,'message':'本地研究服务已就绪。数据日期和历史实验分别展示。'}
    mutex=threading.Lock(); origin=f'http://127.0.0.1:{port}'

    def refresh(mode='daily'):
        try:
            if mode in ('observation','capture_quotes'):
                result=product.observe(output,capture_quotes=mode=='capture_quotes')
                state['message']=f"报价核对完成：{result['qualified']} / {result['securities']} 只通过当前时点校验；本次请求 {result['provider_requests']} 次，失败 {result['capture_failures']} 次，未训练。缺失不使用昨日价格回填。"
            else:
                result=product.update(root,output)
                state['message']=f"更新完成：{result['date']}，{result['predictions']}个非空研究预测。"
        except Exception as exc:
            state['message']='本次更新失败，保留上一成功版本：'+type(exc).__name__+' / '+str(exc)[:200]
        finally:
            state['running']=False

    class Handler(BaseHTTPRequestHandler):
        csrf_token=token
        def log_message(self, *args): pass

        def reply(self, status, body, content_type='text/plain; charset=utf-8'):
            raw=body.encode('utf-8');self.send_response(status)
            self.send_header('Content-Type',content_type);self.send_header('Content-Length',str(len(raw)))
            self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('X-Stock-Research','local-v3')
            self.end_headers();self.wfile.write(raw)

        def valid_host(self): return self.headers.get('Host')==f'127.0.0.1:{port}'

        def do_GET(self):
            if not self.valid_host(): return self.reply(403,'Host refused')
            if self.path=='/health':
                return self.reply(200,json.dumps(dict(startup_identity,
                    source_matches=service_identity(root,output)==startup_identity,
                    update_running=state['running'],execution_ready=False)), 'application/json')
            if self.path.startswith('/review-receipt/'):
                try:
                    from .research_journal import read_review
                    from .research_product_view import review_receipt_page
                    value=read_review(output,self.path.removeprefix('/review-receipt/'))
                    return self.reply(200,review_receipt_page(value),'text/html; charset=utf-8')
                except (ValueError,FileNotFoundError,KeyError):
                    return self.reply(404,'复盘回执不存在或完整性校验失败。')
            if self.path.startswith('/receipt/'):
                try:
                    from .research_journal import read_note
                    note=read_note(output,self.path.removeprefix('/receipt/'))
                    from .research_product_view import receipt_page
                    return self.reply(200,receipt_page(note),'text/html; charset=utf-8')
                except (ValueError,FileNotFoundError,KeyError):
                    return self.reply(404,'判断回执不存在或完整性校验失败；未创建新记录。')
            if self.path!='/': return self.reply(404,'Not found')
            try:
                _,files=read_current(output/'publication')
                # Read the sealed published projection and the small journal only.
                # GET never loads a model, fetches data or changes the publication.
                html=(render(product.journal_projection(output,json.loads(files['desk.json'])))
                      if 'desk.json' in files else files['index.html'].decode('utf-8'))
                html=server_page(html,token,state['message'],state['running'])
                self.reply(200,html,'text/html; charset=utf-8')
            except Exception:
                self.reply(503,'工作台暂时无法读取已发布页面。不要重新训练或重复提交判断；已保存的独立回执仍可查看。')

        def do_POST(self):
            if not self.valid_host() or self.headers.get('Origin')!=origin: return self.reply(403,'Origin refused')
            if self.path not in ('/note','/review','/update','/observe','/capture-quotes','/shutdown'): return self.reply(404,'Not found')
            if self.path!='/shutdown' and service_identity(root,output)!=startup_identity:
                return self.reply(409,'服务代码已变化，请正常停止旧服务并重新启动；本次未执行。')
            try:
                size=int(self.headers.get('Content-Length','0'))
                if not 0<size<=32000: return self.reply(413,'Form size refused')
                if self.headers.get_content_type()!='application/x-www-form-urlencoded': return self.reply(415,'Form required')
                fields=parse_qs(self.rfile.read(size).decode('utf-8'),strict_parsing=True,keep_blank_values=True,max_num_fields=11)
                if any(len(v)!=1 for v in fields.values()) or fields.get('csrf')!=[token]: return self.reply(403,'Token refused')
                values={k:v[0] for k,v in fields.items() if k!='csrf'}
                if self.path=='/shutdown':
                    if shutdown is None or state['running']:return self.reply(409,'Update running or managed shutdown unavailable')
                    self.reply(200,'Local workspace is stopping; data and tasks are unchanged')
                    threading.Thread(target=shutdown,daemon=True).start()
                    return
                if self.path=='/review':
                    from .research_journal import save_review
                    with mutex:review_id=save_review(output,values)
                    self.send_response(303);self.send_header('Location','/review-receipt/'+review_id)
                    self.send_header('Content-Length','0');self.end_headers()
                    return
                if self.path=='/note':
                    if not values.get('request_id'):return self.reply(400,'请刷新工作台后提交带请求编号的判断。')
                    with mutex: note_id=product.save_note(output,values)
                    self.send_response(303);self.send_header('Location','/receipt/'+note_id)
                    self.send_header('Content-Length','0');self.end_headers()
                    return
                else:
                    with mutex:
                        if state['running']: return self.reply(409,'已有更新正在运行')
                        state['running']=True;state['message']='正在更新真实数据；完成前继续展示上一成功版本。'
                        mode={'/observe':'observation','/capture-quotes':'capture_quotes'}.get(self.path,'daily')
                        threading.Thread(target=refresh,args=(mode,),daemon=True).start()
                self.send_response(303);self.send_header('Location','/');self.send_header('Content-Length','0');self.end_headers()
            except (ValueError,KeyError) as exc:
                self.reply(400,'输入未通过校验：'+str(exc)[:200])
            except RuntimeError:
                self.reply(409,'页面版本正在更新。请刷新检查记录是否已保存，再进行操作。')
            except OSError:
                self.reply(503,'存储暂不可用，未确认保存。保留原草稿并用同一请求重试，不要另建重复判断。')
    return Handler


def serve(root, output, port=8766, *, open_browser=False):
    if not 1024<=port<=65535: raise ValueError('unprivileged local port required')
    import socket
    from trade_system.file_lock import FileLock

    class WorkspaceServer(ThreadingHTTPServer):
        allow_reuse_address=False

        def server_bind(self):
            if hasattr(socket,'SO_EXCLUSIVEADDRUSE'):
                self.socket.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
            super().server_bind()

    output=Path(output)
    control=output/('service-'+str(port)+'.json')
    # A crashed process can leave metadata, not ownership. Replace it only after
    # holding our permanent guard AND successfully binding the exclusive port.
    with FileLock(output/('service-'+str(port)+'.guard')):
        server=WorkspaceServer(('127.0.0.1',port),handler(root,output,port))
        published=False
        try:
            server.RequestHandlerClass=handler(root,output,port,shutdown=server.shutdown)
            product.write_pointer(output,control.name,{'scope':'local_workspace_shutdown_only',
                'port':port,'csrf':server.RequestHandlerClass.csrf_token})
            published=True
            print(f'Research desk: http://127.0.0.1:{port}',flush=True)
            if open_browser:
                import webbrowser
                webbrowser.open(f'http://127.0.0.1:{port}')
            server.serve_forever()
        finally:
            # Remove only our control metadata before releasing port/guard.
            try:
                if published: control.unlink(missing_ok=True)
            finally:
                server.server_close()


def stop(output,port):
    """Ask this workspace's own server to stop; never kill a process or touch tasks."""
    from urllib.request import Request,urlopen
    from urllib.parse import urlencode
    from .gap_evidence import read_json
    control=read_json(output/('service-'+str(port)+'.json'))[0]
    if control.get('scope')!='local_workspace_shutdown_only' or control['port']!=port:
        raise ValueError('workspace service ownership unavailable')
    origin='http://127.0.0.1:'+str(port)
    request=Request(origin+'/shutdown',data=urlencode({'csrf':control['csrf']}).encode(),
        headers={'Origin':origin,'Content-Type':'application/x-www-form-urlencoded'})
    with urlopen(request,timeout=10) as response:
        return {'stop_requested':response.status==200,'production_tasks_changed':False}
