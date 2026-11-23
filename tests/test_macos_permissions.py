"""Exercise the native capture policy without opening a window or using a mic."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest


SHELL = Path(__file__).resolve().parents[1] / 'desktop/MinDock.swift'


def capture_delegate():
    source = SHELL.read_text(encoding='utf-8')
    method = re.search(
        r'    func webView\(_ webView: WKWebView, requestMediaCapturePermissionFor.*?\n    }',
        source, re.DOTALL)
    assert method is not None, 'WKUIDelegate must explicitly restrict capture permission'
    return method.group()


def test_capture_uses_webkit_prompt_without_automatic_grants():
    delegate = capture_delegate()
    assert 'decisionHandler(.prompt)' in delegate
    assert 'decisionHandler(.deny)' in delegate
    assert '.grant' not in delegate


def test_capture_trust_is_established_only_after_backend_nonce_verification():
    source = SHELL.read_text(encoding='utf-8')
    check = 'guard (payload?["nonce"] as? String) == self.readyNonce else {'
    assert source.index(check) < source.index('self.baseURL = url')
    assert 'baseURL = nil' in source
    assert 'web.uiDelegate = self' in source


@pytest.fixture(scope='module')
def capture_policy(tmp_path_factory):
    delegate = capture_delegate()
    swiftc = shutil.which('swiftc')
    if swiftc is None:
        pytest.skip('Swift compiler is needed to execute the native capture policy')
    directory = tmp_path_factory.mktemp('macos-capture-policy')
    source = directory / 'main.swift'
    executable = directory / 'capture-policy'
    # Only WebKit/process inputs are stand-ins; the decision code is copied verbatim.
    source.write_text('''
import Foundation
enum WKMediaCaptureType: String { case microphone, camera, cameraAndMicrophone, future }
enum WKPermissionDecision: String { case prompt, deny, grant }
struct WKSecurityOrigin {
    let `protocol`: String
    let host: String
    let port: Int
}
struct WKFrameInfo { let securityOrigin: WKSecurityOrigin }
final class WKWebView { var url: URL? }
final class Backend { var isRunning = true }
final class CapturePolicy {
    var web = WKWebView()
    var backend: Backend? = Backend()
    var baseURL: URL? = URL(string: "http://127.0.0.1:54321")
    var loaded = true
    var stopping = false
    var maintaining = false
    var failing = false
''' + delegate + '''
}
let cases = try JSONSerialization.jsonObject(with: FileHandle.standardInput.readDataToEndOfFile()) as! [[String: Any]]
var results: [[String]] = []
for item in cases {
    let policy = CapturePolicy()
    policy.web.url = URL(string: item["page"] as? String ?? "http://127.0.0.1:54321/lectures")
    if let base = item["base"] as? String { policy.baseURL = URL(string: base) }
    policy.loaded = item["loaded"] as? Bool ?? true
    policy.stopping = item["stopping"] as? Bool ?? false
    policy.maintaining = item["maintaining"] as? Bool ?? false
    policy.failing = item["failing"] as? Bool ?? false
    policy.backend?.isRunning = item["running"] as? Bool ?? true
    if item["backendMissing"] as? Bool == true { policy.backend = nil }
    let origin = WKSecurityOrigin(
        protocol: item["scheme"] as? String ?? "http",
        host: item["host"] as? String ?? "127.0.0.1",
        port: item["port"] as? Int ?? 54321)
    let frame = WKFrameInfo(securityOrigin: WKSecurityOrigin(
        protocol: item["frameScheme"] as? String ?? origin.protocol,
        host: item["frameHost"] as? String ?? origin.host,
        port: item["framePort"] as? Int ?? origin.port))
    let view = item["otherWebView"] as? Bool == true ? WKWebView() : policy.web
    let type = WKMediaCaptureType(rawValue: item["type"] as? String ?? "microphone")!
    var decisions: [String] = []
    policy.webView(view, requestMediaCapturePermissionFor: origin,
        initiatedByFrame: frame, type: type) { decisions.append($0.rawValue) }
    results.append(decisions)
}
let output = try JSONSerialization.data(withJSONObject: results)
FileHandle.standardOutput.write(output)
''', encoding='utf-8')
    compiled = subprocess.run([swiftc, str(source), '-o', str(executable)],
                              capture_output=True, text=True, timeout=120)
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr

    def decide(cases):
        completed = subprocess.run([str(executable)], input=json.dumps(cases),
                                   capture_output=True, text=True, timeout=15)
        assert completed.returncode == 0, completed.stdout + completed.stderr
        return json.loads(completed.stdout)

    return decide


def test_verified_local_microphone_prompts_once(capture_policy):
    assert capture_policy([{}, {'page': 'http://127.0.0.1:54321/other/path?q=1'},
                           {'base': 'http://127.0.0.1:23456', 'port': 23456,
                            'page': 'http://127.0.0.1:23456/lectures'}]) == [['prompt']] * 3


@pytest.mark.parametrize('capture_request', [
    {'type': 'camera'}, {'type': 'cameraAndMicrophone'}, {'type': 'future'},
    {'scheme': 'https'}, {'host': 'example.com'}, {'host': 'localhost'},
    {'host': '::1'}, {'host': '127.0.0.1.example.com'}, {'port': 54322}, {'port': 0},
    {'frameScheme': 'https'}, {'frameHost': 'example.com'}, {'framePort': 54322},
    {'base': ''}, {'base': 'http://127.0.0.1'},
    {'base': 'https://127.0.0.1:54321'}, {'base': 'http://example.com:54321'},
    {'base': 'http://127.0.0.1:0', 'port': 0, 'page': 'http://127.0.0.1:0'},
    {'base': 'http://127.0.0.1:65536', 'port': 65536, 'page': 'http://127.0.0.1:65536'},
    {'page': ''}, {'page': 'about:blank'}, {'page': 'http://example.com:54321'},
    {'page': 'https://127.0.0.1:54321'}, {'page': 'http://127.0.0.1:54322'},
    {'loaded': False}, {'running': False}, {'backendMissing': True},
    {'stopping': True}, {'maintaining': True}, {'failing': True}, {'otherWebView': True},
])
def test_untrusted_or_non_microphone_capture_is_denied_once(capture_policy, capture_request):
    assert capture_policy([capture_request]) == [['deny']]
