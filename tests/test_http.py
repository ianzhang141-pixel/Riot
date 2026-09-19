import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from http.server import ThreadingHTTPServer
from lolab import server, research
from lolab.archive import Warehouse, dumps
from tests.fixtures import game


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name);server.CONSOLE=server.Console(root)
        self.http=ThreadingHTTPServer(("127.0.0.1",0),server.DebugHandler)
        thread=threading.Thread(target=self.http.serve_forever,daemon=True);thread.start()
        self.addCleanup(self.http.server_close);self.addCleanup(self.http.shutdown)
        self.port=self.http.server_port
        wh=Warehouse(root);m,t,c=game()
        refs=[wh.capture(dumps(o).encode(),source="test",kind=k,source_id=m["metadata"]["matchId"],endpoint="test") for o,k in ((m,"match"),(t,"timeline"))]
        research.import_game(wh,*refs,c)
    def request(self,path,method="GET",body=None,headers=None):
        client=http.client.HTTPConnection("127.0.0.1",self.port)
        try:
            client.request(method,path,body,headers or {})
            response=client.getresponse();return response.status,response.read()
        finally:client.close()
    def test_page_and_json_routes(self):
        self.assertEqual(self.request('/research')[0],200)
        code,raw=self.request('/api/research/games?league=LPL')
        self.assertEqual(code,200);self.assertEqual(len(json.loads(raw)["games"]),1)
        self.assertEqual(self.request('/api/research/game?id=test%3ATEST_000001')[0],200)
        self.assertEqual(self.request('/api/research/stats')[0],200)
        self.assertEqual(self.request('/api/research/status')[0],200)
    def test_localhost_csrf_and_rebinding_blocked(self):
        self.assertEqual(self.request('/api/research/status',headers={'Host':'evil.example'})[0],403)
        self.assertEqual(self.request('/api/research/action','POST','{}',{'Content-Type':'application/json','Origin':'https://evil.example'})[0],403)
        self.assertEqual(self.request('/api/research/action','POST','{}',{'Content-Type':'text/plain'})[0],403)
    def test_invalid_json_bad_request(self):
        self.assertEqual(self.request('/api/research/action','POST','invalid',{'Content-Type':'application/json'})[0],400)
        self.assertEqual(self.request('/api/research/action','POST','[]',{'Content-Type':'application/json'})[0],400)

if __name__=='__main__':unittest.main()
