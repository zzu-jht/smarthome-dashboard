"""
SmartHome 代理服务器
- 公网部署时从环境变量读取端口和登录密码
- 本地运行：python server.py  →  http://localhost:5173
"""
from http.server import HTTPServer, SimpleHTTPRequestHandler
import urllib.request, json, os, hashlib, secrets

IAM_URL   = 'https://iam.cn-north-4.myhuaweicloud.com/v3/auth/tokens'
IOTDA_URL = 'https://f5db3f44e4.st1.iotda-app.cn-north-4.myhuaweicloud.com'

LOGIN_PASSWORD = os.environ.get('LOGIN_PASSWORD', 'admin123')
_sessions: set = set()

def _make_token(password: str) -> str:
    return hashlib.sha256((password + secrets.token_hex(8)).encode()).hexdigest()

def _fetch_sensor_text() -> str:
    """查华为云设备影子，返回可读文本，失败抛异常"""
    iam_body = json.dumps({"auth":{"identity":{"methods":["password"],"password":{"user":{"domain":{"id":"019daebe5a21730fb6f7b308ad52a284"},"name":"esp32test","password":"Test1234"}}},"scope":{"project":{"id":"019daec06b7f75309869716ef4016d9f"}}}}).encode()
    iam_req = urllib.request.Request(IAM_URL, data=iam_body, headers={'Content-Type':'application/json'}, method='POST')
    with urllib.request.urlopen(iam_req, timeout=8) as r:
        hw_token = r.headers.get('X-Subject-Token', '')
    shadow_url = IOTDA_URL + '/v5/iot/019daec06b7f75309869716ef4016d9f/devices/69e77120cbb0cf6bb953468c_esp32_zzujht/shadow'
    shadow_req = urllib.request.Request(shadow_url, headers={'X-Auth-Token': hw_token}, method='GET')
    with urllib.request.urlopen(shadow_req, timeout=8) as r:
        data = json.loads(r.read())
    props = data.get('shadow', [{}])[0].get('reported', {}).get('properties', {})
    parts = []
    if props.get('temperature') is not None: parts.append(f"温度{round(float(props['temperature']), 1)}度")
    if props.get('humidity')    is not None: parts.append(f"湿度{round(float(props['humidity']), 1)}%")
    if props.get('light')       is not None: parts.append(f"光照{props['light']}")
    if props.get('pir')         is not None: parts.append("有人" if props['pir'] else "无人")
    return "，".join(parts) if parts else "暂无数据"

class Handler(SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[{self.address_string()}] {fmt % args}")

    def _cors(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET,POST,OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type,X-Auth-Token,X-Session-Token')

    def _session_ok(self) -> bool:
        return self.headers.get('X-Session-Token', '') in _sessions

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self._cors()
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_POST(self):
        if self.path == '/api/login':
            self._handle_login(); return
        if self.path == '/skill':
            self._handle_skill(); return
        if not self._session_ok():
            self._json(401, {'error': 'unauthorized'}); return
        if self.path == '/proxy/token':
            self._proxy_post(IAM_URL, want_token_header=True)
        elif self.path.startswith('/proxy/iotda'):
            self._proxy_post(IOTDA_URL + self.path[len('/proxy/iotda'):])
        else:
            self.send_response(404); self.end_headers()

    def do_GET(self):
        if self.path == '/query':
            self._handle_query()
        elif self.path.startswith('/proxy/iotda'):
            if not self._session_ok():
                self._json(401, {'error': 'unauthorized'}); return
            self._proxy_get(IOTDA_URL + self.path[len('/proxy/iotda'):])
        else:
            super().do_GET()

    def _handle_query(self):
        try:
            result = f"室内环境：{_fetch_sensor_text()}".encode('utf-8')
        except Exception as e:
            result = f"查询失败：{e}".encode('utf-8')
        self.send_response(200)
        self._cors()
        self.send_header('Content-Type', 'text/plain; charset=utf-8')
        self.end_headers()
        self.wfile.write(result)

    def _handle_skill(self):
        try:
            speak = f"当前室内环境：{_fetch_sensor_text()}"
        except Exception:
            speak = "查询失败，请稍后再试"
        self._json(200, {
            "version": "1.0",
            "response": {
                "toSpeak":  {"type": "T", "text": speak},
                "toDisplay":{"type": "T", "text": speak},
                "shouldEndSession": True
            }
        })

    def _handle_login(self):
        length = int(self.headers.get('Content-Length', 0))
        try:
            data = json.loads(self.rfile.read(length) if length else b'{}')
        except Exception:
            data = {}
        if data.get('password') == LOGIN_PASSWORD:
            tok = _make_token(LOGIN_PASSWORD)
            _sessions.add(tok)
            self._json(200, {'token': tok})
        else:
            self._json(401, {'error': 'wrong password'})

    def _read_body(self):
        length = int(self.headers.get('Content-Length', 0))
        return self.rfile.read(length) if length else b''

    def _proxy_post(self, url, want_token_header=False):
        body  = self._read_body()
        token = self.headers.get('X-Auth-Token', '')
        hdrs  = {'Content-Type': 'application/json'}
        if token: hdrs['X-Auth-Token'] = token
        req = urllib.request.Request(url, data=body, headers=hdrs, method='POST')
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                resp_body = r.read()
                self.send_response(r.status)
                self._cors()
                self.send_header('Content-Type', 'application/json')
                if want_token_header:
                    self.send_header('X-Subject-Token', r.headers.get('X-Subject-Token', ''))
                self.end_headers()
                self.wfile.write(resp_body)
        except urllib.error.HTTPError as e:
            resp_body = e.read()
            self.send_response(e.code)
            self._cors()
            self.send_header('Content-Type', 'application/json')
            if want_token_header:
                self.send_header('X-Subject-Token', e.headers.get('X-Subject-Token', ''))
            self.end_headers()
            self.wfile.write(resp_body)

    def _proxy_get(self, url):
        token = self.headers.get('X-Auth-Token', '')
        hdrs  = {'X-Auth-Token': token} if token else {}
        req = urllib.request.Request(url, headers=hdrs, method='GET')
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                resp_body = r.read()
                self.send_response(r.status)
                self._cors()
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(resp_body)
        except urllib.error.HTTPError as e:
            resp_body = e.read()
            self.send_response(e.code)
            self._cors()
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(resp_body)

os.chdir(os.path.dirname(os.path.abspath(__file__)))
PORT = int(os.environ.get('PORT', 5173))
print(f"Server running at http://localhost:{PORT}")
HTTPServer(('0.0.0.0', PORT), Handler).serve_forever()
