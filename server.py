"""
SmartHome 代理服务器
- 公网部署时从环境变量读取端口和登录密码
- 本地运行：python server.py  →  http://localhost:5173
"""
from http.server import HTTPServer, SimpleHTTPRequestHandler
import urllib.request, json, os, hashlib, secrets, hmac

IAM_URL   = 'https://iam.cn-north-4.myhuaweicloud.com/v3/auth/tokens'
IOTDA_URL = 'https://f5db3f44e4.st1.iotda-app.cn-north-4.myhuaweicloud.com'
_IAM_BODY = json.dumps({"auth":{"identity":{"methods":["password"],"password":{"user":{"domain":{"id":"019daebe5a21730fb6f7b308ad52a284"},"name":"esp32test","password":"Test1234"}}},"scope":{"project":{"id":"019daec06b7f75309869716ef4016d9f"}}}}).encode()

LOGIN_PASSWORD = os.environ.get('LOGIN_PASSWORD', 'admin123')
# 用 HMAC 签名代替内存 set，重启后 token 仍然有效
SESSION_SECRET = os.environ.get('SESSION_SECRET', LOGIN_PASSWORD + '_secret')

def _make_token(password: str) -> str:
    salt = secrets.token_hex(8)
    sig  = hmac.new(SESSION_SECRET.encode(), (password + salt).encode(), hashlib.sha256).hexdigest()
    return salt + ':' + sig

def _verify_token(token: str) -> bool:
    try:
        salt, sig = token.split(':', 1)
        expected = hmac.new(SESSION_SECRET.encode(), (LOGIN_PASSWORD + salt).encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, sig)
    except Exception:
        return False

def _fetch_sensor_text() -> str:
    """查华为云设备影子，返回可读文本，失败抛异常"""
    iam_req = urllib.request.Request(IAM_URL, data=_IAM_BODY, headers={'Content-Type':'application/json'}, method='POST')
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
        return _verify_token(self.headers.get('X-Session-Token', ''))

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
        if self.path == '/api/ai':
            self._handle_ai(); return
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


    def _handle_ai(self):
        """接收文字+传感器状态，调poloAPI Claude，返回AI理解结果"""
        POLO_API_KEY = os.environ.get('CLAUDE_API_KEY', '')
        length = int(self.headers.get('Content-Length', 0))
        try:
            data = json.loads(self.rfile.read(length) if length else b'{}')
        except Exception:
            self._json(400, {'error': '请求格式错误'}); return
        text = data.get('text', '')
        sensor = data.get('sensorState', {})
        if not text:
            self._json(400, {'error': '没有收到文字'}); return
        if not POLO_API_KEY:
            self._json(500, {'error': 'CLAUDE_API_KEY未配置'}); return

        system_prompt = f"""你是智能家居控制助手。只能控制：LED灯(SetLED)、蜂鸣器(SetBuzzer)、门锁(SetLock)。用户说其他设备时忽略并在reply说"暂不支持"。

设备状态：LED={'开' if sensor.get('led') else '关'} 蜂鸣器={'开' if sensor.get('buzzer') else '关'} 温度={sensor.get('temperature','--')}C 湿度={sensor.get('humidity','--')}% 光照={sensor.get('light','--')} 人={('有' if sensor.get('pir') else '无')}

严格规则：
1. 只输出一个JSON对象，不加任何其他文字
2. reply字段必须简短（10字以内），只说操作结果，不解释无关内容
3. 只使用半角双引号，不使用中文引号

格式（三选一）：
{{"type":"command","action":"SetLED","paras":{{"led":1,"manual":1}},"reply":"好的"}}
{{"type":"multi_command","commands":[{{"action":"SetLED","paras":{{"led":1,"manual":1}}}},{{"action":"SetBuzzer","paras":{{"buzzer":1,"manual":1}}}}],"reply":"好的"}}
{{"type":"query","reply":"温度{sensor.get('temperature','--')}C"}}

命令参数：
- SetLED: {{"led":1或0,"manual":1}}
- SetBuzzer: {{"buzzer":1或0,"manual":1}}
- SetLock: {{"lock":1}}"""

        ai_body = json.dumps({
            'model': 'claude-sonnet-4-20250514',
            'max_tokens': 512,
            'system': system_prompt,
            'messages': [{'role': 'user', 'content': text}]
        }).encode('utf-8')
        try:
            import re
            req = urllib.request.Request(
                'https://fast.poloai.top/v1/messages',
                data=ai_body,
                headers={
                    'Content-Type': 'application/json',
                    'x-api-key': POLO_API_KEY,
                    'anthropic-version': '2023-06-01'
                },
                method='POST'
            )
            with urllib.request.urlopen(req, timeout=20) as r:
                result = json.loads(r.read())
            content = result['content'][0]['text'].strip()
            # 替换全角引号为半角，再提取第一个完整 JSON 对象
            content = content.replace('“', '"').replace('”', '"').replace('‘', "'").replace('’', "'")
            start = content.find('{')
            if start == -1:
                self._json(500, {'error': 'AI返回格式异常: ' + content}); return
            try:
                ai_json, _ = json.JSONDecoder().raw_decode(content, start)
            except json.JSONDecodeError:
                # 解析失败：尝试用正则抢救 action 和 reply，降级为单命令执行
                action_m = re.search(r'"action"\s*:\s*"(Set\w+)"', content)
                reply_m  = re.search(r'"reply"\s*:\s*"([^"]*)"', content)
                if action_m:
                    paras_map = {'SetLED': {'led': 1, 'manual': 1}, 'SetBuzzer': {'buzzer': 1, 'manual': 1}, 'SetLock': {'lock': 1}}
                    ai_json = {
                        'type': 'command',
                        'action': action_m.group(1),
                        'paras': paras_map.get(action_m.group(1), {}),
                        'reply': reply_m.group(1) if reply_m else '好的'
                    }
                else:
                    self._json(500, {'error': 'AI返回JSON解析失败 | ' + content[:200]}); return
            # multi_command: 执行每条子命令
            if ai_json.get('type') == 'multi_command':
                token = self._get_hw_token()
                for cmd in ai_json.get('commands', []):
                    try:
                        self._send_hw_cmd(token, cmd['action'], cmd.get('paras', {}))
                    except Exception as e:
                        print(f"[multi_command] {cmd.get('action')} failed: {e}")
                # ai_json['type'] already set, no need to reassign
            reply_text = ai_json.get('reply', '')
            audio_base64 = ''
            if reply_text:
                try:
                    audio_base64 = self._google_tts(reply_text, POLO_API_KEY)
                except Exception:
                    pass
            ai_json['audioBase64'] = audio_base64
            self._json(200, ai_json)
        except Exception as e:
            self._json(500, {'error': str(e)})

    def _get_hw_token(self):
        req = urllib.request.Request(IAM_URL, data=_IAM_BODY, headers={'Content-Type':'application/json'}, method='POST')
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.headers.get('X-Subject-Token', '')

    def _send_hw_cmd(self, token, action, paras):
        cmd_url = IOTDA_URL + '/v5/iot/019daec06b7f75309869716ef4016d9f/devices/69e77120cbb0cf6bb953468c_esp32_zzujht/commands'
        body = json.dumps({'service_id': 'SmartHome', 'command_name': action, 'paras': paras}).encode()
        req = urllib.request.Request(cmd_url, data=body, headers={'Content-Type':'application/json','X-Auth-Token':token}, method='POST')
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read())

    def _google_tts(self, text, api_key):
        """用Google TTS合成语音，返回mp3的base64"""
        import base64, urllib.parse
        encoded = urllib.parse.quote(text)
        tts_url = f'https://translate.google.com/translate_tts?ie=UTF-8&tl=zh-CN&client=tw-ob&q={encoded}'
        tts_req = urllib.request.Request(
            tts_url,
            headers={'User-Agent': 'Mozilla/5.0'}
        )
        with urllib.request.urlopen(tts_req, timeout=15) as r:
            ct = r.headers.get('Content-Type', '')
            audio_bytes = r.read()
            if ct.startswith('audio/') or len(audio_bytes) > 1000:
                return base64.b64encode(audio_bytes).decode('utf-8')
        return ''
    def _handle_login(self):
        length = int(self.headers.get('Content-Length', 0))
        try:
            data = json.loads(self.rfile.read(length) if length else b'{}')
        except Exception:
            data = {}
        if hmac.compare_digest(data.get('password', ''), LOGIN_PASSWORD):
            tok = _make_token(LOGIN_PASSWORD)
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
        except (urllib.error.HTTPError, urllib.error.URLError) as e:
            code = getattr(e, 'code', 502)
            resp_body = e.read() if hasattr(e, 'read') else b'{"error":"network error"}'
            self.send_response(code)
            self._cors()
            self.send_header('Content-Type', 'application/json')
            if want_token_header:
                self.send_header('X-Subject-Token', getattr(getattr(e, 'headers', None), 'get', lambda *a: '')('X-Subject-Token', ''))
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
        except (urllib.error.HTTPError, urllib.error.URLError) as e:
            code = getattr(e, 'code', 502)
            resp_body = e.read() if hasattr(e, 'read') else b'{"error":"network error"}'
            self.send_response(code)
            self._cors()
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(resp_body)

os.chdir(os.path.dirname(os.path.abspath(__file__)))
PORT = int(os.environ.get('PORT', 5173))
print(f"Server running at http://localhost:{PORT}")
HTTPServer(('0.0.0.0', PORT), Handler).serve_forever()

