"""Loopback-only workbench; bounded CSRF-protected actions, no trading or arbitrary paths."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import secrets
import threading
from urllib.parse import parse_qs

from trade_system.file_lock import FileLock
from . import research_product as product
from .publisher import read_current
from .research_product_view import server_page


def handler(root, output, port):
    token=secrets.token_urlsafe(32)
    state={'running':False,'message':'本地研究服务已就绪。数据日期和历史实验分别展示。'}
    mutex=threading.Lock(); origin=f'http://127.0.0.1:{port}'

    def refresh():
        try:
            with FileLock(output/'update.guard'):
                result=product.update(root,output)
            state['message']=f"更新完成：{result['date']}，{result['predictions']}个非空研究预测。"
        except Exception as exc:
            state['message']='本次更新失败，保留上一成功版本：'+type(exc).__name__+' / '+str(exc)[:200]
        finally:
            state['running']=False

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass

        def reply(self, status, body, content_type='text/plain; charset=utf-8'):
            raw=body.encode('utf-8');self.send_response(status)
            self.send_header('Content-Type',content_type);self.send_header('Content-Length',str(len(raw)))
            self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('X-Stock-Research','local-v1')
            self.end_headers();self.wfile.write(raw)

        def valid_host(self): return self.headers.get('Host')==f'127.0.0.1:{port}'

        def do_GET(self):
            if not self.valid_host(): return self.reply(403,'Host refused')
            if self.path!='/': return self.reply(404,'Not found')
            try:
                _,files=read_current(output/'publication')
                html=server_page(files['index.html'].decode('utf-8'),token,state['message'],state['running'])
                self.reply(200,html,'text/html; charset=utf-8')
            except Exception:
                self.reply(503,'研究页面尚未就绪，请先运行构建入口。')

        def do_POST(self):
            if not self.valid_host() or self.headers.get('Origin')!=origin: return self.reply(403,'Origin refused')
            if self.path not in ('/note','/update'): return self.reply(404,'Not found')
            try:
                size=int(self.headers.get('Content-Length','0'))
                if not 0<size<=32000: return self.reply(413,'Form size refused')
                if self.headers.get_content_type()!='application/x-www-form-urlencoded': return self.reply(415,'Form required')
                fields=parse_qs(self.rfile.read(size).decode('utf-8'),strict_parsing=True,keep_blank_values=True,max_num_fields=10)
                if any(len(v)!=1 for v in fields.values()) or fields.get('csrf')!=[token]: return self.reply(403,'Token refused')
                values={k:v[0] for k,v in fields.items() if k!='csrf'}
                if self.path=='/note':
                    with mutex: product.save_note(output,values)
                    state['message']='判断已保存；原记录和接收时间保留，不产生委托。'
                else:
                    with mutex:
                        if state['running']: return self.reply(409,'已有更新正在运行')
                        state['running']=True;state['message']='正在更新真实数据；完成前继续展示上一成功版本。'
                        threading.Thread(target=refresh,daemon=True).start()
                self.send_response(303);self.send_header('Location','/');self.send_header('Content-Length','0');self.end_headers()
            except (ValueError,KeyError) as exc:
                self.reply(400,'输入未通过校验：'+str(exc)[:200])
            except RuntimeError:
                self.reply(409,'页面版本正在更新。请刷新检查记录是否已保存，再进行操作。')
    return Handler


def serve(root, output, port=8766, *, open_browser=False):
    if not 1024<=port<=65535: raise ValueError('unprivileged local port required')
    server=ThreadingHTTPServer(('127.0.0.1',port),handler(root,output,port))
    print(f'Research desk: http://127.0.0.1:{port}',flush=True)
    if open_browser:
        import webbrowser
        webbrowser.open(f'http://127.0.0.1:{port}')
    try: server.serve_forever()
    finally: server.server_close()
